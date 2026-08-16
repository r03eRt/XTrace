"""Generador determinista de previews cortos con FFmpeg (PR-029 · lavfi · sin red).

Contrato PR-029: si ffmpeg está disponible se genera un mp4 corto con `lavfi`
(`testsrc`); si no, los tests de preview se marcan skip con motivo documentado
(véase `ffmpeg_available()`). Genera un vídeo sintético, nunca contenido real
(SEC-004).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FFMPEG = "ffmpeg"
_FFMPEG_TIMEOUT_S = 60.0


def ffmpeg_available() -> bool:
    """True si `ffmpeg` está disponible en el PATH (para el skip de tests)."""
    return shutil.which(FFMPEG) is not None


def make_preview_mp4(
    path: Path,
    *,
    duration_s: float = 2.0,
    size: tuple[int, int] = (160, 90),
    rate: int = 15,
) -> Path:
    """Genera un mp4 sintético de `duration_s` segundos en `path` (testsrc lavfi).

    Requiere `ffmpeg` (con libx264). Sin red y determinista: mismo input →
    mismo archivo. Lanza `RuntimeError` si ffmpeg falla o no está disponible.
    """
    if not ffmpeg_available():
        raise RuntimeError(
            f"ffmpeg no está disponible en el PATH: no se puede generar el preview en {path}"
        )
    width, height = size
    cmd = [
        FFMPEG,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration_s}:size={width}x{height}:rate={rate}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=_FFMPEG_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al generar el preview en {path}: {result.stderr.strip()}")
    return path
