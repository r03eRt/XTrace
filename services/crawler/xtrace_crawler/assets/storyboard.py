"""Recorte de tiles de un sprite storyboard con Pillow (PR-029 · FR-005 · contracts §7).

Convierte un sprite (imagen PIL) en sus tiles individuales usando el grid
declarado `cols`×`rows` y el tamaño de tile (derivado del sprite si no se
indica). Cada tile lleva su `position` (índice row-major), `row`/`col` y un
`timestamp_ms` **aproximado** = `round(position / total_tiles * duration_ms)`
cuando la fuente da la duración; si no, `None` (el vídeo sigue procesándose
sin timestamp, paridad con FR-012 del spike).

Puro y determinista (NFR-003): misma imagen + mismos parámetros → mismas
tiles, sin red y sin estado. **PR-036 · decompression bomb**: acepta un
presupuesto `max_pixels` y rehúsa sprites que lo superen con
`ImageTooManyPixelsError` antes de recortar (defensa en profundidad sobre el
open limitado de `assets/fetch.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from xtrace_crawler.assets.fetch import ImageTooManyPixelsError


class StoryboardError(ValueError):
    """Parámetros de grid/tile inválidos o sprite incompatible con el grid declarado."""


@dataclass(frozen=True)
class StoryboardTile:
    """Tile recortada del sprite: imagen PIL + metadatos de posición/timestamp."""

    position: int  # índice de tile row-major: 0..total-1
    row: int
    col: int
    timestamp_ms: int | None  # aproximado desde position+duración; None si la fuente no da duración
    image: Image.Image


def split_storyboard(
    sprite: Image.Image,
    *,
    cols: int,
    rows: int,
    tile_size: tuple[int, int] | None = None,
    duration_ms: int | None = None,
    max_pixels: int | None = None,
) -> list[StoryboardTile]:
    """Recorta `cols`×`rows` tiles del sprite y devuelve sus metadatos (FR-005).

    Args:
        sprite: imagen del sprite storyboard (PIL).
        cols: número de columnas del grid (>= 1).
        rows: número de filas del grid (>= 1).
        tile_size: tamaño `(ancho, alto)` de cada tile; si es `None` se deriva
            del sprite (que debe ser divisible por el grid). Si se indica, el
            sprite debe ser al menos `cols*tile_w` × `rows*tile_h` (el sobrante
            se recorta como padding de la región superior-izquierda).
        duration_ms: duración del vídeo en ms (fuente); produce el timestamp
            aproximado de cada tile. `None` → timestamps `None`.
        max_pixels: **PR-036 · decompression bomb**; presupuesto de píxeles del
            sprite (defensa en profundidad: el open limitado de `fetch.py` ya
            lo verificó; aquí se revalida antes de recortar). `None` → sin
            verificación (uso directo de la función pura).

    Returns:
        Lista de `cols*rows` tiles en orden row-major (position = row*cols+col).

    Raises:
        StoryboardError: grid/tile inválidos, duración negativa, sprite más
            pequeño / no divisible por el grid declarado, o `max_pixels`
            inválido (< 1).
        ImageTooManyPixelsError: el sprite supera `max_pixels` (PR-036).
    """
    if cols < 1 or rows < 1:
        raise StoryboardError(f"cols y rows deben ser >= 1, got cols={cols}, rows={rows}")
    if duration_ms is not None and duration_ms < 0:
        raise StoryboardError(f"duration_ms debe ser >= 0, got {duration_ms}")
    if max_pixels is not None:
        if max_pixels < 1:
            raise StoryboardError(f"max_pixels debe ser >= 1, got {max_pixels}")
        if sprite.width * sprite.height > max_pixels:
            raise ImageTooManyPixelsError(
                f"sprite storyboard {sprite.width}x{sprite.height} "
                f"({sprite.width * sprite.height} px) supera max_pixels={max_pixels}"
            )

    if tile_size is None:
        if sprite.width % cols != 0 or sprite.height % rows != 0:
            raise StoryboardError(
                f"el sprite {sprite.size} no es divisible por el grid {cols}x{rows}; "
                "indica tile_size explícito si el sprite tiene padding"
            )
        tile_w, tile_h = sprite.width // cols, sprite.height // rows
    else:
        tile_w, tile_h = tile_size
        if tile_w < 1 or tile_h < 1:
            raise StoryboardError(f"tile_size debe ser >= 1x1 px, got {tile_size}")
        if sprite.width < cols * tile_w or sprite.height < rows * tile_h:
            raise StoryboardError(
                f"el sprite {sprite.size} es más pequeño que el grid {cols}x{rows} "
                f"de tiles {tile_w}x{tile_h} px"
            )

    total = cols * rows
    tiles: list[StoryboardTile] = []
    for row in range(rows):
        for col in range(cols):
            position = row * cols + col
            box = (col * tile_w, row * tile_h, (col + 1) * tile_w, (row + 1) * tile_h)
            timestamp_ms = None if duration_ms is None else round(position / total * duration_ms)
            tiles.append(
                StoryboardTile(
                    position=position,
                    row=row,
                    col=col,
                    timestamp_ms=timestamp_ms,
                    image=sprite.crop(box),
                )
            )
    return tiles
