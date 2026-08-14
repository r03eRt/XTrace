"""pHash 64-bit y distancia de Hamming (PR-004 · FR-004 · ADR-0005).

Un pHash es una firma perceptual de 64 bits: imágenes visualmente casi
idénticas (recompresión, resize leve) producen hashes con una distancia de
Hamming pequeña. Se usa para matching near-exact (FR-004) y como base de la
deduplicación de frames (FR-003, ADR-0005).
"""

import imagehash
import numpy as np
from PIL import Image

PHASH_BITS = 64


def compute_phash(image: Image.Image) -> int:
    """Devuelve el pHash de 64 bits de `image` (FR-004).

    La imagen se normaliza internamente (escala de grises, 32×32, DCT), así
    que el modo y el tamaño de entrada no cambian el contrato: el resultado
    es siempre un entero sin signo de exactamente `PHASH_BITS` bits.
    """
    bits = imagehash.phash(image, hash_size=8).hash
    packed = np.packbits(bits)
    return int.from_bytes(packed.tobytes(), "big")


def hamming_distance(a: int, b: int) -> int:
    """Distancia de Hamming entre dos pHash: nº de bits en los que difieren.

    Precondición: `a` y `b` son pHash de 64 bits (salida de
    `compute_phash`). Distancia 0 → imágenes idénticas; distancia pequeña
    (p. ej. ≤ 10) → near-exact (recompresión/resize leve, ADR-0005).
    """
    return (a ^ b).bit_count()
