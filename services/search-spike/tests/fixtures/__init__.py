"""Fixtures reproducibles de vídeo para los tests (PR-008 · FR-001/FR-002).

Los vídeos se generan con FFmpeg en el momento del test (no se comprometen
binarios) y de forma determinista: el generador sintetico testsrc2 de FFmpeg
no usa aleatoriedad, asi que mismos parametros -> mismo video -> mismos frames
(reproducible, SC-007 mindset):

    ffmpeg -v error -y -f lavfi \
      -i "testsrc2=duration=2:size=320x240:rate=25" \
      -c:v libx264 -pix_fmt yuv420p fixture.mp4

(Nota: la opcion seed de testsrc2 no existe en FFmpeg 7.1.1; no es necesaria
por el determinismo intrinseco del filtro.)

Requisito: ffmpeg y ffprobe en el PATH. Los tests que los usan hacen skip si
no estan disponibles (p. ej. un CI sin FFmpeg instalado).
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

FFMPEG: str | None = shutil.which("ffmpeg")
FFPROBE: str | None = shutil.which("ffprobe")


@functools.lru_cache(maxsize=1)
def _has_libx264() -> bool:
    """True si el binario ffmpeg disponible incluye el encoder libx264."""
    if FFMPEG is None:
        return False
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "libx264" in proc.stdout


def _require_tools() -> None:
    if FFMPEG is None or FFPROBE is None:
        raise RuntimeError("ffmpeg/ffprobe no estan disponibles en el PATH")


def _run_checked(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"comando fallo (exit {proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )


def make_test_video(
    path: str | Path,
    *,
    duration_s: float = 2.0,
    size: str = "320x240",
    rate: int = 25,
) -> Path:
    """Genera un video pequeno y determinista (testsrc2) en path.

    Documentado en el handoff PR-008: comando FFmpeg exacto arriba (docstring).
    """
    _require_tools()
    assert FFMPEG is not None
    encoder = "libx264" if _has_libx264() else "mpeg4"
    _run_checked(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={duration_s}:size={size}:rate={rate}",
            "-c:v",
            encoder,
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return Path(path)


def make_audio_video(path: str | Path) -> Path:
    """Genera un .mp4 solo-audio (sin stream de video) en path."""
    _require_tools()
    assert FFMPEG is not None
    _run_checked(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    return Path(path)


def make_corrupt_video(path: str | Path) -> Path:
    """Escribe bytes no-video en path simulando un fichero corrupto."""
    Path(path).write_bytes(b"XTrace fake video - not a real media file" + bytes([0, 1, 2]))
    return Path(path)
