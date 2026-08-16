"""Extracción de frames de previews CORTOS con FFmpeg (PR-029 · FR-005 · SC-006 · contracts §7).

**Nunca se procesa un vídeo completo** (SC-006): antes de extraer frames se
sondea la duración con `ffprobe`; si supera `max_duration_s` (default **120 s**,
configurable) se lanza `PreviewTooLongError`; si la duración **no puede
determinarse** se rehúsa el procesado (`PreviewExtractionError`): un input que
no puede probarse corto se trata como potencial vídeo completo. El propio tipo
`AssetKind` de los assets ya excluye "video" (SC-006, contracts §7).

La extracción usa el filtro `fps=1/<interval_s>` (un frame cada `interval_s`
segundos) y produce archivos JPEG con su `timestamp_ms` aproximado
(`round(index * interval_s * 1000)`), coherente con el estilo de
`assets/storyboard.py`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_PREVIEW_SECONDS = 120.0  # límite que separa preview corto de vídeo completo (SC-006)
_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"
_PROBE_TIMEOUT_S = 30.0
_EXTRACT_TIMEOUT_S = 120.0


class PreviewTooLongError(ValueError):
    """El asset supera la duración máxima permitida: parece vídeo completo (SC-006)."""


class PreviewExtractionError(RuntimeError):
    """FFmpeg/ffprobe falló, no está disponible o la duración no pudo determinarse.

    Un input cuya duración no puede verificarse se rehúsa (SC-006: solo se
    procesan previews cortos verificables).
    """


@dataclass(frozen=True)
class PreviewFrame:
    """Frame extraído de un preview: archivo JPEG + timestamp aproximado (ms)."""

    path: Path
    timestamp_ms: int


class PreviewFrameExtractor:
    """Extrae frames de previews cortos con intervalo configurable (FR-005).

    Args:
        max_duration_s: duración máxima aceptada para un preview; cualquier
            input con duración mayor se rehúsa como vídeo completo (SC-006).
    """

    def __init__(self, *, max_duration_s: float = DEFAULT_MAX_PREVIEW_SECONDS) -> None:
        if max_duration_s <= 0:
            raise ValueError(f"max_duration_s debe ser > 0, got {max_duration_s}")
        self._max_duration_s = max_duration_s

    def probe_duration(self, source: Path) -> float | None:
        """Devuelve la duración en segundos, o `None` si no puede determinarse.

        Usa `ffprobe` (formato contenedor). Cualquier fallo (binario ausente,
        timeout, archivo no multimedia) devuelve `None`: el llamador decide
        rehusar el procesado (SC-006).
        """
        cmd = [
            _FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=_PROBE_TIMEOUT_S
            )
        except FileNotFoundError:
            raise PreviewExtractionError(
                "ffprobe no está disponible; no se puede verificar la duración del asset"
            ) from None
        except subprocess.TimeoutExpired:
            raise PreviewExtractionError(
                "ffprobe excedió el timeout al sondear la duración del asset"
            ) from None
        if result.returncode != 0:
            return None
        try:
            return float(result.stdout.strip())
        except ValueError:
            return None

    def extract_frames(
        self, source: Path, *, interval_s: float, out_dir: Path
    ) -> list[PreviewFrame]:
        """Extrae un frame cada `interval_s` segundos del preview (FR-005).

        Rechaza inputs que parezcan vídeo completo (SC-006): duración >
        `max_duration_s` (error tipado `PreviewTooLongError`) o duración
        desconocida (`PreviewExtractionError`). Los frames se escriben como
        `frame_%05d.jpg` en `out_dir` (creado si falta); los timestamps son
        aproximados y monotónicos desde 0.

        Args:
            source: archivo del preview (mp4 corto ya descargado).
            interval_s: intervalo entre frames en segundos (> 0).
            out_dir: directorio de salida de los frames.

        Returns:
            Lista de `PreviewFrame` ordenada por timestamp.

        Raises:
            PreviewTooLongError: duración conocida y mayor que `max_duration_s`.
            PreviewExtractionError: duración desconocida o fallo de ffmpeg/ffprobe.
        """
        if interval_s <= 0:
            raise ValueError(f"interval_s debe ser > 0, got {interval_s}")
        if not source.is_file():
            raise PreviewExtractionError(f"el preview no existe: {source}")

        duration = self.probe_duration(source)
        if duration is None:
            raise PreviewExtractionError(
                f"no se pudo determinar la duración de {source}: se rehúsa procesar "
                "(SC-006: solo previews cortos verificables)"
            )
        if duration > self._max_duration_s:
            raise PreviewTooLongError(
                f"el asset dura {duration:.1f}s y excede el límite de preview "
                f"{self._max_duration_s:.0f}s: se rehúsa (SC-006, nunca vídeo completo)"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = out_dir / "frame_%05d.jpg"
        cmd = [
            _FFMPEG,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{interval_s}",
            "-q:v",
            "2",
            str(pattern),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=_EXTRACT_TIMEOUT_S
            )
        except FileNotFoundError:
            raise PreviewExtractionError(
                "ffmpeg no está disponible; no se pueden extraer frames del preview"
            ) from None
        except subprocess.TimeoutExpired:
            raise PreviewExtractionError(
                "ffmpeg excedió el timeout al extraer frames del preview"
            ) from None
        if result.returncode != 0:
            raise PreviewExtractionError(
                f"ffmpeg falló al extraer frames de {source}: {result.stderr.strip()}"
            )

        frames = [
            PreviewFrame(path=path, timestamp_ms=round(index * interval_s * 1000))
            for index, path in enumerate(sorted(out_dir.glob("frame_*.jpg")))
        ]
        return frames
