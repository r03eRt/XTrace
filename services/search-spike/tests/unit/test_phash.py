"""Unit tests del módulo pHash (PR-004 · FR-004 · ADR-0005).

Criterios verificables:
- `compute_phash` devuelve un pHash de 64 bits (int) determinista.
- El pHash es estable ante recompresión y resize leves (matching near-exact).
- `hamming_distance` es coherente: imágenes similares → distancia pequeña;
  imágenes distintas → distancia mayor.
"""

import io

import numpy as np
from PIL import Image

from xtrace_spike.hashing.phash import PHASH_BITS, compute_phash, hamming_distance

_WIDTH = 640
_HEIGHT = 480


def _synthetic_image(seed: int) -> Image.Image:
    """Imagen sintética determinista con estructura tipo frame de vídeo.

    La semilla controla la paleta, la posición de las formas y el ruido:
    semillas distintas producen imágenes claramente distintas y reproducibles.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:_HEIGHT, 0:_WIDTH]

    # Fondo: gradientes con fase (paleta) derivada de la semilla.
    phase = int(rng.integers(0, 256))
    img = np.zeros((_HEIGHT, _WIDTH, 3), dtype=np.uint8)
    img[..., 0] = ((xx * 255) // _WIDTH + phase) % 256
    img[..., 1] = ((yy * 255) // _HEIGHT + phase * 2) % 256
    img[..., 2] = (((xx + yy) * 255) // (_WIDTH + _HEIGHT) + phase * 3) % 256

    # Damero: tamaño de bloque, región y tono derivados de la semilla.
    block = int(rng.integers(12, 40))
    x_max = int(rng.integers(120, 280))
    y_max = int(rng.integers(80, 200))
    checker = ((xx // block) + (yy // block)) % 2
    mask = (xx < x_max) & (yy < y_max)
    tone = int(rng.integers(160, 240))
    img[mask] = np.where(checker[mask, None] == 0, tone, tone // 5)

    # Dos rectángulos de color en posiciones derivadas de la semilla.
    x0, y0 = int(rng.integers(0, 200)), int(rng.integers(0, 120))
    x1, y1 = int(rng.integers(320, 480)), int(rng.integers(200, 360))
    img[y0 : y0 + 100, x0 : x0 + 160] = rng.integers(0, 256, size=3)
    img[y1 : y1 + 90, x1 : x1 + 160] = rng.integers(0, 256, size=3)

    # Anillo circular con centro y radio derivados de la semilla.
    cx, cy = int(rng.integers(80, 560)), int(rng.integers(60, 420))
    r_in = int(rng.integers(20, 50))
    radius = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    ring = (radius > r_in) & (radius < r_in + 30)
    img[ring] = rng.integers(0, 256, size=3)

    # Ruido leve (textura de sensor).
    noise = rng.integers(0, 24, size=(_HEIGHT, _WIDTH, 1), dtype=np.uint8)
    return Image.fromarray(np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8))


def _roundtrip(image: Image.Image, fmt: str, **save_kwargs: object) -> Image.Image:
    """Re-encodea la imagen en memoria (sin tocar disco) y la recarga."""
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **save_kwargs)
    buffer.seek(0)
    return Image.open(buffer)


def test_compute_phash_returns_64bit_integer() -> None:
    """El pHash es un int de 64 bits (FR-004)."""
    phash = compute_phash(_synthetic_image(1))
    assert isinstance(phash, int)
    assert 0 <= phash < 2**PHASH_BITS
    assert phash.bit_length() <= PHASH_BITS


def test_compute_phash_is_deterministic() -> None:
    """Misma imagen → mismo pHash, también tras copia y re-lectura."""
    image = _synthetic_image(3)
    first = compute_phash(image)
    second = compute_phash(image)
    third = compute_phash(image.copy())
    assert first == second == third


def test_phash_stable_under_png_jpeg_recompression() -> None:
    """Recompresión PNG→JPEG (calidad 85) apenas altera el pHash (near-exact)."""
    image = _synthetic_image(7)
    as_png = _roundtrip(image, "PNG")
    as_jpeg = _roundtrip(image, "JPEG", quality=85)
    dist = hamming_distance(compute_phash(image), compute_phash(as_jpeg))
    assert dist <= 10, f"pHash inestable ante recompresión JPEG: {dist} bits"
    assert hamming_distance(compute_phash(as_png), compute_phash(as_jpeg)) <= 10


def test_phash_stable_under_light_resize() -> None:
    """Resize leve (×0.9 y ×1.1) apenas altera el pHash (near-exact)."""
    image = _synthetic_image(11)
    down = image.resize((int(_WIDTH * 0.9), int(_HEIGHT * 0.9)), Image.Resampling.LANCZOS)
    up = image.resize((int(_WIDTH * 1.1), int(_HEIGHT * 1.1)), Image.Resampling.LANCZOS)
    for variant in (down, up):
        dist = hamming_distance(compute_phash(image), compute_phash(variant))
        assert dist <= 10, f"pHash inestable ante resize: {dist} bits"


def test_phash_stable_under_resize_plus_recompression() -> None:
    """Resize + recompresión combinados mantienen el pHash cerca del original."""
    image = _synthetic_image(13)
    resized = image.resize((320, 240), Image.Resampling.LANCZOS)
    recompressed = _roundtrip(resized, "JPEG", quality=80)
    dist = hamming_distance(compute_phash(image), compute_phash(recompressed))
    assert dist <= 12, f"pHash inestable ante resize+recompresión: {dist} bits"


def test_hamming_distance_is_coherent_with_similar_images() -> None:
    """Imágenes iguales → distancia 0; variantes leves → distancia pequeña."""
    image = _synthetic_image(17)
    assert hamming_distance(compute_phash(image), compute_phash(image)) == 0
    recompressed = _roundtrip(image, "JPEG", quality=85)
    dist = hamming_distance(compute_phash(image), compute_phash(recompressed))
    # La recompresión puede dejar el pHash intacto (distancia 0) o
    # alterarlo levemente; en ambos casos debe quedar cerca del original.
    assert dist <= 10, f"pHash inestable ante recompresión: {dist} bits"


def test_hamming_distance_is_coherent_with_different_images() -> None:
    """Imágenes distintas (semillas diferentes) → distancia claramente mayor."""
    phash_a = compute_phash(_synthetic_image(1))
    phash_b = compute_phash(_synthetic_image(2))
    assert hamming_distance(phash_a, phash_b) > 10


def test_hamming_distance_basic_values() -> None:
    """Casos base de la distancia de Hamming (nº de bits distintos)."""
    assert hamming_distance(0, 0) == 0
    assert hamming_distance(0b1010, 0b1111) == 2
    assert hamming_distance(0b0000_1111, 0b1111_0000) == 8
    assert hamming_distance(2**63, 0) == 1
    assert hamming_distance(0, 2**PHASH_BITS - 1) == PHASH_BITS
    assert hamming_distance(0b1010, 0b1010) == 0


def test_hamming_distance_is_symmetric_and_bounded() -> None:
    """La distancia es simétrica y nunca supera los 64 bits del pHash."""
    pairs = [
        (compute_phash(_synthetic_image(1)), compute_phash(_synthetic_image(2))),
        (compute_phash(_synthetic_image(5)), compute_phash(_synthetic_image(9))),
        (0, 0),
        (2**63, 1),
    ]
    for a, b in pairs:
        assert hamming_distance(a, b) == hamming_distance(b, a)
        assert 0 <= hamming_distance(a, b) <= PHASH_BITS


def test_compute_phash_accepts_grayscale_and_rgba() -> None:
    """Modos de imagen habituales (L, RGBA) producen un pHash de 64 bits."""
    rgb = _synthetic_image(23)
    for variant in (rgb.convert("L"), rgb.convert("RGBA")):
        phash = compute_phash(variant)
        assert isinstance(phash, int)
        assert 0 <= phash < 2**PHASH_BITS
