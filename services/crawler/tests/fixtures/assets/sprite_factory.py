"""Generador determinista de sprites sintéticos (PR-029 · tests sin red · NFR-003).

Cada tile recibe un **color único** derivado de su `(row, col)` más un borde
blanco de 1 px, de modo que los tests pueden verificar el recorte exacto del
grid (crop) y la distinguibilidad entre tiles sin depender de ningún recurso
externo ni de la red. Sin RNG: la misma llamada produce exactamente la misma
imagen (determinismo, NFR-003).
"""

from __future__ import annotations

from PIL import Image, ImageDraw

BORDER_COLOR: tuple[int, int, int] = (255, 255, 255)


def tile_color(row: int, col: int) -> tuple[int, int, int]:
    """Color único y estable para la tile `(row, col)` del sprite."""
    return (
        (row * 53 + col * 29) % 256,
        (row * 17 + col * 71) % 256,
        (row * 91 + col * 13) % 256,
    )


def make_sprite(*, cols: int, rows: int, tile_w: int, tile_h: int) -> Image.Image:
    """Crea un sprite de `cols`×`rows` tiles de `tile_w`×`tile_h` px (RGB).

    Cada tile es un rectángulo relleno de `tile_color(row, col)` con borde
    blanco; el píxel central queda siempre con el color de relleno (los tests
    lo usan para verificar el recorte).
    """
    sprite = Image.new("RGB", (cols * tile_w, rows * tile_h))
    draw = ImageDraw.Draw(sprite)
    for row in range(rows):
        for col in range(cols):
            x0, y0 = col * tile_w, row * tile_h
            draw.rectangle(
                [x0, y0, x0 + tile_w - 1, y0 + tile_h - 1],
                fill=tile_color(row, col),
                outline=BORDER_COLOR,
                width=1,
            )
    return sprite
