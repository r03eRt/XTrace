"""Interfaz EmbeddingProvider (FR-005 · ADR-0007 · contracts §3).

Contrato estable (specs/001-visual-search-spike/contracts/README.md §3):

    class EmbeddingProvider(Protocol):
        model_id: str
        dimension: int
        def embed_images(self, images: Sequence[PIL.Image]) -> np.ndarray: ...

- embed_images procesa un lote de imágenes (batch, FR-005) y devuelve un
  array NumPy de shape (N, D) con filas L2-normalizadas (dtype float32).
- El dominio (indexación, búsqueda, ranking) depende solo de esta interfaz;
  cambiar de proveedor (SigLIP local, GPU, serverless) no debe tocar el dominio
  (ADR-0007).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import numpy as np
from PIL import Image


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Calcula embeddings visuales por lote (FR-005, ADR-0007, contracts §3)."""

    model_id: str
    """Identificador estable del modelo/proveedor (p. ej. siglip2-base)."""

    dimension: int
    """Dimensión D de los vectores devueltos (shape (N, D))."""

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray[Any, Any]:
        """Embedding del lote de imágenes.

        Args:
            images: lote de imágenes PIL.

        Returns:
            Array de shape (N, D) en float32, con cada fila L2-normalizada
            (distancia coseno = producto escalar).
        """
        ...
