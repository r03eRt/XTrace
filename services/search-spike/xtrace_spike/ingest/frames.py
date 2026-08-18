"""Extracción de frames representativos con FFprobe + FFmpeg (FR-002 · ADR-0006).

Estrategia de muestreo configurable (FR-002): "frames_per_video" frames por
vídeo, distribuidos uniformemente en el tiempo (filtro fps de FFmpeg sobre la
duración medida por FFprobe). El tamaño de los frames es configurable vía
"scale_width" (escala que preserva la proporción; None = sin escalado).

Temporales seguros (FR-009 · ADR-0006): la extracción escribe los PNG en un
directorio temporal único por vídeo y el context manager "extract_frames"
garantiza el borrado en try/finally, incluso si falla FFprobe/FFmpeg. Los
paths de los temporales quedan listados en "ExtractionResult.temporary_paths"
para el cleanup del llamador (pipeline, PR-010).

Errores controlados: fichero corrupto o sin stream de vídeo → ProbeError;
fallo de FFmpeg → FramesExtractionError. Ambos heredan de IngestError, de
forma que el pipeline puede marcar el vídeo como "failed" y continuar con el
resto del dataset (FR-001, acceptance scenario 3 de US1).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xtrace_spike.ingest import IngestError
from xtrace_spike.ingest.dataset import DatasetVideo
from xtrace_spike.sampling import AdaptiveSamplingPolicy

DEFAULT_FRAMES_PER_VIDEO: int = 30
DEFAULT_SCALE_WIDTH: int = 256

_FRAME_PATTERN = "frame_%06d.png"


class ProbeError(IngestError):
    """FFprobe no pudo leer el vídeo (corrupto, sin stream de vídeo, etc.)."""


class FramesExtractionError(IngestError):
    """FFmpeg falló al extraer frames (o no produjo ningún frame)."""


@dataclass(frozen=True)
class VideoProbe:
    """Metadatos del vídeo obtenidos con FFprobe (FR-002)."""

    duration_ms: int
    width: int
    height: int
    avg_fps: float | None
    codec: str | None


@dataclass(frozen=True)
class ExtractedFrame:
    """Un frame representativo extraído (temporal) con su timestamp (FR-002)."""

    path: Path
    timestamp_ms: int
    width: int
    height: int


@dataclass(frozen=True)
class ExtractionResult:
    """Resultado de una extracción: probe + frames temporales + dir de salida.

    Los paths de los frames temporales se listan en "temporary_paths" para el
    cleanup del llamador; el context manager "extract_frames" ya los elimina
    al salir (try/finally, FR-009 · ADR-0006 · SC-006).
    """

    probe: VideoProbe
    out_dir: Path
    frames: tuple[ExtractedFrame, ...]

    @property
    def temporary_paths(self) -> tuple[Path, ...]:
        """Paths de todos los temporales creados (listados para cleanup)."""
        return tuple(frame.path for frame in self.frames)


def probe_video(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> VideoProbe:
    """Inspecciona el vídeo con FFprobe y devuelve sus metadatos (FR-002).

    Raises:
        ProbeError: si FFprobe falla (fichero corrupto/no soportado), no hay
            stream de vídeo, o no se puede determinar duración/dimensiones.
    """
    video_path = Path(path)
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise ProbeError(f"ffprobe no pudo leer '{video_path}': {detail[:300]}")

    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"respuesta de ffprobe inválida para '{video_path}'") from exc

    video_stream: Any = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            video_stream = stream
            break
    if video_stream is None:
        raise ProbeError(f"'{video_path}' no contiene un stream de vídeo")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError(f"'{video_path}': dimensiones de vídeo no disponibles")

    duration_raw: Any = video_stream.get("duration") or (data.get("format") or {}).get("duration")
    if duration_raw is None:
        raise ProbeError(f"'{video_path}': duración no disponible")
    try:
        duration_s = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise ProbeError(f"'{video_path}': duración inválida ({duration_raw!r})") from exc
    if duration_s <= 0:
        raise ProbeError(f"'{video_path}': duración no positiva ({duration_s})")

    return VideoProbe(
        duration_ms=round(duration_s * 1000),
        width=width,
        height=height,
        avg_fps=_parse_frame_rate(video_stream.get("avg_frame_rate")),
        codec=str(video_stream["codec_name"]) if video_stream.get("codec_name") else None,
    )


@contextlib.contextmanager
def extract_frames(
    video: DatasetVideo,
    *,
    work_root: str | Path,
    frames_per_video: int | None = DEFAULT_FRAMES_PER_VIDEO,
    scale_width: int | None = DEFAULT_SCALE_WIDTH,
    sampling_policy: AdaptiveSamplingPolicy | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> Iterator[ExtractionResult]:
    """Extrae frames representativos del vídeo en un dir temporal (FR-002).

    Muestreo uniforme: "frames_per_video" frames espaciados duration/N. El
    directorio temporal (único por vídeo) se elimina en finally (FR-009 ·
    ADR-0006 · SC-006), también si FFprobe/FFmpeg fallan (errores controlados).

    Args:
        video: vídeo del dataset (de "scan_dataset" o construido por el test).
        work_root: directorio base para los temporales (se crea si falta).
        frames_per_video: nº de frames a extraer por vídeo (> 0) en modo
            histórico. Se ignora cuando se proporciona ``sampling_policy``.
        scale_width: anchura de salida (preserva proporción); None = sin escala.
        sampling_policy: política adaptativa explícita. Calcula el objetivo tras
            obtener la duración y usa centros de intervalo.

    Raises:
        ValueError: configuración inválida (frames_per_video <= 0, scale_width <= 0).
        ProbeError: fichero corrupto o sin stream de vídeo (FR-001 US1 esc. 3).
        FramesExtractionError: fallo de FFmpeg o cero frames extraídos.
    """
    if frames_per_video is not None and frames_per_video <= 0:
        raise ValueError(f"frames_per_video debe ser > 0 (recibido {frames_per_video})")
    if scale_width is not None and scale_width <= 0:
        raise ValueError(f"scale_width debe ser > 0 o None (recibido {scale_width})")

    work_dir = Path(work_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    slug = _sanitize_slug(video.local_ref)
    out_dir = Path(tempfile.mkdtemp(prefix=f"xtrace-frames-{slug}-", dir=work_dir))
    try:
        probe = probe_video(video.path)
        effective_frames = (
            sampling_policy.target_count(probe.duration_ms)
            if sampling_policy is not None
            else frames_per_video
        )
        if effective_frames is None or effective_frames <= 0:
            raise ValueError("frames_per_video debe ser > 0 o proporcionar sampling_policy")
        frames = _run_ffmpeg_extraction(
            video=video,
            probe=probe,
            out_dir=out_dir,
            frames_per_video=effective_frames,
            scale_width=scale_width,
            centered=sampling_policy is not None,
            ffmpeg_bin=ffmpeg_bin,
        )
        yield ExtractionResult(probe=probe, out_dir=out_dir, frames=frames)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _run_ffmpeg_extraction(
    *,
    video: DatasetVideo,
    probe: VideoProbe,
    out_dir: Path,
    frames_per_video: int,
    scale_width: int | None,
    centered: bool = False,
    ffmpeg_bin: str,
) -> tuple[ExtractedFrame, ...]:
    """Ejecuta FFmpeg y devuelve los frames extraídos con su timestamp."""
    duration_s = probe.duration_ms / 1000.0
    interval_s = duration_s / frames_per_video
    interval_ms = probe.duration_ms / frames_per_video

    if centered:
        return _run_centered_extraction(
            video=video,
            probe=probe,
            out_dir=out_dir,
            frames_per_video=frames_per_video,
            scale_width=scale_width,
            interval_ms=interval_ms,
            ffmpeg_bin=ffmpeg_bin,
        )

    filters = [f"fps=1/{interval_s:.9f}"]
    if scale_width is not None:
        filters.append(f"scale={scale_width}:-2")

    cmd = [ffmpeg_bin, "-y", "-v", "error"]
    cmd.extend(["-i", str(video.path)])
    if filters:
        cmd.extend(["-vf", ",".join(filters)])
    cmd.extend(
        [
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(frames_per_video),
            "-f",
            "image2",
            str(out_dir / _FRAME_PATTERN),
        ]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise FramesExtractionError(
            f"ffmpeg falló al extraer frames de '{video.path}': {detail[:300]}"
        )

    paths = sorted(out_dir.glob("frame_*.png"))
    if not paths:
        raise FramesExtractionError(f"no se extrajo ningún frame de '{video.path}'")

    width, height = _scaled_dimensions(probe, scale_width)
    return tuple(
        ExtractedFrame(
            path=path,
            timestamp_ms=round(index * interval_ms),
            width=width,
            height=height,
        )
        for index, path in enumerate(paths)
    )


def _run_centered_extraction(
    *,
    video: DatasetVideo,
    probe: VideoProbe,
    out_dir: Path,
    frames_per_video: int,
    scale_width: int | None,
    interval_ms: float,
    ffmpeg_bin: str,
) -> tuple[ExtractedFrame, ...]:
    """Extrae cada centro adaptativo para materializar siempre 1..8 frames.

    Una única cadena ``fps=1/interval`` después de un seek centrado puede
    emitir un frame menos en ficheros long-GOP. El modo adaptativo está
    limitado a ocho puntos, por lo que un seek por punto es determinista y
    acotado; el camino legacy sigue usando una única invocación de 30 frames.
    """
    filters = [f"scale={scale_width}:-2"] if scale_width is not None else []
    frames: list[ExtractedFrame] = []
    width, height = _scaled_dimensions(probe, scale_width)
    for index in range(frames_per_video):
        timestamp_ms = round((index + 0.5) * interval_ms)
        output_path = out_dir / f"frame_{index + 1:06d}.png"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-ss",
            f"{timestamp_ms / 1000.0:.9f}",
            "-i",
            str(video.path),
        ]
        if filters:
            cmd.extend(["-vf", ",".join(filters)])
        cmd.extend(["-frames:v", "1", "-f", "image2", str(output_path)])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or f"exit {proc.returncode}"
            raise FramesExtractionError(
                f"ffmpeg falló al extraer frame {index + 1} de '{video.path}': {detail[:300]}"
            )
        if not output_path.exists():
            raise FramesExtractionError(f"no se extrajo el frame {index + 1} de '{video.path}'")
        frames.append(
            ExtractedFrame(
                path=output_path,
                timestamp_ms=timestamp_ms,
                width=width,
                height=height,
            )
        )
    return tuple(frames)


def _scaled_dimensions(probe: VideoProbe, scale_width: int | None) -> tuple[int, int]:
    """Dimensiones de salida: originales o escaladas preservando proporción (par)."""
    if scale_width is None:
        return probe.width, probe.height
    height = 2 * round(probe.height * scale_width / probe.width / 2)
    return scale_width, max(2, height)


def _parse_frame_rate(value: Any) -> float | None:
    """Parsea 'num/den' de FFprobe a float; None si no es un rate válido."""
    if not value:
        return None
    num_str, sep, den_str = str(value).partition("/")
    try:
        num = float(num_str)
        den = float(den_str) if sep else 1.0
    except ValueError:
        return None
    if den == 0:
        return None
    return num / den


def _sanitize_slug(local_ref: str) -> str:
    """Convierte el local_ref en un prefijo de directorio seguro."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", local_ref).strip("._-")
    return slug or "video"
