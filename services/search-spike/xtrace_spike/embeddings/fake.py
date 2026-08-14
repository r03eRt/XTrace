"""FakeEmbeddingProvider: proveedor determinista para tests/CI (FR-005 · ADR-0007 · contracts §3).

Sustituye a SiglipLocalProvider en tests unitarios e integración y en CI,
evitando cargar Torch. Es determinista: el vector de cada imagen es una
función pura de sus bytes (SHA-256 sobre píxeles RGB + contador por bloque),
sin estado ni aleatoriedad, con L2-normalización.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, cast

import numpy as np
from PIL import Image

from xtrace_spike.embeddings.provider import EmbeddingProvider


class FakeEmbeddingProvider(EmbeddingProvider):
    """Proveedor determinista hash->vector L2-normalizado (dimensión configurable)."""

    def __init__(self, dimension: int = 512, model_id: str = "fake-deterministic-hash") -> None:
        if dimension < 1:
            raise ValueError(f"dimension debe ser >= 1, se recibió {dimension}")
        self.dimension = dimension
        self.model_id = model_id

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray[Any, Any]:
        """Embedding determinista del lote: shape (N, D), filas L2-normalizadas."""
        if len(images) == 0:
            return np.zeros((0, self.dimension), dtype=np.float32)
        vectors: np.ndarray[Any, Any] = np.stack(
            [self._vector_from_image(image) for image in images]
        )
        return vectors

    def _vector_from_image(self, image: Image.Image) -> np.ndarray[Any, Any]:
        """Vector unitario de dimension derivado de los bytes de la imagen.

        Bloques de 32 bytes de SHA-256(píxeles RGB + contador) se concatenan
        hasta cubrir dimension; cada byte mapea a [0, 1). El resultado se
        L2-normaliza (norma degenerada 0 -> error explícito, imposible en la
        práctica con SHA-256).
        """
        pixels = image.convert("RGB").tobytes()
        block_count = (self.dimension + 31) // 32
        raw = bytearray()
        for block in range(block_count):
            raw.extend(hashlib.sha256(pixels + block.to_bytes(4, "big")).digest())
        values = np.frombuffer(bytes(raw), dtype=np.uint8)[: self.dimension]
        vector = values.astype(np.float32) / 255.0
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError(
                f"FakeEmbeddingProvider: hash degenerado (norma 0) con dimension={self.dimension}"
            )
        return cast(np.ndarray[Any, Any], vector / norm)
