"""Fixtures reproducibles de frames sintéticos para el benchmark (PR-015 · FR-015).

Frames generados con Pillow + numpy (sin ffmpeg, rápidos y deterministas):
cada "vídeo" (video_ref) tiene un patrón base distinto y cada frame desplaza
un elemento móvil, de forma que los frames de un vídeo son distinguibles
entre sí y los vídeos son visualmente diferentes (necesario para que las
etiquetas del benchmark tengan sentido). Todo deriva de un seed fijo.

Layout: <root>/<video_ref>/frame_%04d.png

El pool por defecto (4 vídeos x 10 frames = 40) cubre el recuento por
defecto del generador (~30 por variante, FR-015 · Decisión D3).
"""

from __future__ import annotations

import colorsys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from xtrace_spike.benchmark import SourceFrame

DEFAULT_VIDEOS: int = 4
DEFAULT_FRAMES_PER_VIDEO: int = 10
DEFAULT_SIZE: tuple[int, int] = (320, 240)
DEFAULT_SEED: int = 42


def make_benchmark_frames(
    root: str | Path,
    *,
    videos: int = DEFAULT_VIDEOS,
    frames_per_video: int = DEFAULT_FRAMES_PER_VIDEO,
    size: tuple[int, int] = DEFAULT_SIZE,
    seed: int = DEFAULT_SEED,
) -> tuple[SourceFrame, ...]:
    """Genera frames sintéticos deterministas y devuelve sus SourceFrame.

    Escribe <root>/<video_ref>/frame_%04d.png por frame. Reproducible: mismo
    seed -> mismos bytes de imagen. El pool (videos x frames_per_video) debe
    tener >= cases_per_variant frames para el recuento pedido.
    """
    root_path = Path(root)
    rng = np.random.default_rng(seed)
    frames: list[SourceFrame] = []
    width, height = size
    for video_index in range(videos):
        video_ref = f"video_{video_index:03d}"
        video_dir = root_path / video_ref
        video_dir.mkdir(parents=True, exist_ok=True)
        for frame_index in range(frames_per_video):
            image = _frame_image(video_index, frame_index, width, height, rng)
            path = video_dir / f"frame_{frame_index:04d}.png"
            image.save(path, "PNG")
            frames.append(SourceFrame(video_ref=video_ref, path=path.absolute()))
    return tuple(frames)


def _hsv_rgb(hue_deg: float, saturation: float, value: float) -> tuple[int, int, int]:
    """Convierte HSV (grados) a RGB entero (colorsys, stdlib, determinista)."""
    red, green, blue = colorsys.hsv_to_rgb((hue_deg % 360) / 360.0, saturation, value)
    return round(red * 255), round(green * 255), round(blue * 255)


def _vertical_gradient(
    width: int, height: int, top_rgb: tuple[int, int, int], bottom_rgb: tuple[int, int, int]
) -> np.ndarray[Any, Any]:
    """Array (height, width, 3) con gradiente vertical entre dos colores."""
    t = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    top = np.array(top_rgb, dtype=np.float32)[None, None, :]
    bottom = np.array(bottom_rgb, dtype=np.float32)[None, None, :]
    return (top * (1.0 - t) + bottom * t).astype(np.uint8)


def _horizontal_gradient(
    width: int, height: int, left_rgb: tuple[int, int, int], right_rgb: tuple[int, int, int]
) -> np.ndarray[Any, Any]:
    """Array (height, width, 3) con gradiente horizontal entre dos colores."""
    t = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
    left = np.array(left_rgb, dtype=np.float32)[None, None, :]
    right = np.array(right_rgb, dtype=np.float32)[None, None, :]
    return (left * (1.0 - t) + right * t).astype(np.uint8)


def _diagonal_gradient(
    width: int, height: int, start_rgb: tuple[int, int, int], end_rgb: tuple[int, int, int]
) -> np.ndarray[Any, Any]:
    """Array (height, width, 3) con gradiente diagonal entre dos colores."""
    tx = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
    ty = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    t = (tx + ty) / 2.0
    start = np.array(start_rgb, dtype=np.float32)[None, None, :]
    end = np.array(end_rgb, dtype=np.float32)[None, None, :]
    return (start * (1.0 - t) + end * t).astype(np.uint8)


def _frame_image(
    video_index: int, frame_index: int, width: int, height: int, rng: np.random.Generator
) -> Image.Image:
    """Imagen sintética del frame (video_index, frame_index), determinista.

    Cuatro familias de patrón (rotando por vídeo) con un elemento móvil por
    frame: los frames de un mismo vídeo difieren y los vídeos son distintos.
    """
    pattern = video_index % 4
    hue = (video_index * 90 + frame_index * 6) % 360
    if pattern == 0:
        arr = _vertical_gradient(
            width, height, _hsv_rgb(hue, 0.6, 0.9), _hsv_rgb((hue + 45) % 360, 0.7, 0.4)
        )
        size = 40
        x = (frame_index * 37) % (width - size)
        y = (frame_index * 53) % (height - size)
        arr[y : y + size, x : x + size] = (
            255 - arr[y : y + size, x : x + size].astype(np.int16)
        ).astype(np.uint8)
        return Image.fromarray(arr, mode="RGB")
    if pattern == 1:
        arr = _horizontal_gradient(
            width, height, _hsv_rgb(hue, 0.5, 0.8), _hsv_rgb((hue + 120) % 360, 0.8, 0.6)
        )
        image = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(image)
        x = (frame_index * 41) % (width - 60)
        y = (frame_index * 29) % (height - 60)
        draw.ellipse([x, y, x + 60, y + 60], fill=_hsv_rgb((hue + 180) % 360, 0.3, 1.0))
        return image
    if pattern == 2:
        arr = _diagonal_gradient(
            width, height, _hsv_rgb(hue, 0.7, 0.5), _hsv_rgb((hue + 90) % 360, 0.4, 0.9)
        )
        band = 80
        x = (frame_index * 31) % (width - band)
        arr[:, x : x + band] = np.clip(arr[:, x : x + band].astype(np.int16) - 60, 0, 255).astype(
            np.uint8
        )
        return Image.fromarray(arr, mode="RGB")
    arr = np.full((height, width, 3), _hsv_rgb(hue, 0.55, 0.45), dtype=np.uint8)
    image = Image.fromarray(arr, mode="RGB")
    draw = ImageDraw.Draw(image)
    cx = ((frame_index * 47 + 40) % (width - 80)) + 40
    cy = ((frame_index * 61 + 40) % (height - 80)) + 40
    draw.ellipse([cx - 30, cy - 30, cx + 30, cy + 30], fill=_hsv_rgb((hue + 200) % 360, 0.6, 1.0))
    return image
