"""SiglipLocalProvider: embeddings SigLIP v1 en local (PR-005 · FR-005 · ADR-0005/0007).

Proveedor real de `EmbeddingProvider` (contracts §3) que calcula embeddings
visuales con **SigLIP v1** vía open_clip + torch, en CPU local (sin GPU en el
spike). El modelo por defecto es `ViT-B-16-SigLIP` (pretrained `webli`),
imagen 224×224, **D = 768** — dimensión fijada y documentada en
`docs/adr/0005-phash-plus-embeddings.md` (la usará el esquema DB, PR-006).

Aislamiento del extra `siglip` (pyproject.toml): torch y open_clip se importan
de forma **LAZY** (`importlib` en tiempo de ejecución, dentro de
`_ensure_loaded`), de modo que los gates BASE (ruff/mypy/pytest) pasan sin
tener el extra instalado (`uv sync --locked`). El test de integración @slow
que ejercita este módulo usa `pytest.importorskip("torch")`.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

import numpy as np
from PIL import Image

from xtrace_spike.embeddings.provider import EmbeddingProvider

# Dimensión D por modelo SigLIP v1 (embed_dim de los configs de open_clip
# `model_configs/`: ViT-B-16 → 768, ViT-L-16 → 1024). PR-005 fija D=768 con
# ViT-B-16-SigLIP (ver ADR-0005); el mapa evita cargar el modelo solo para
# conocer la dimensión del contrato.
_MODEL_DIMENSIONS: dict[str, int] = {
    "ViT-B-16-SigLIP": 768,
    "ViT-B-16-SigLIP-256": 768,
    "ViT-B-16-SigLIP-384": 768,
    "ViT-B-16-SigLIP-512": 768,
    "ViT-L-16-SigLIP": 1024,
    "ViT-L-16-SigLIP-256": 1024,
    "ViT-L-16-SigLIP-384": 1024,
    "ViT-L-16-SigLIP-512": 1024,
}


class SiglipLocalProvider(EmbeddingProvider):
    """Embeddings SigLIP v1 en local (CPU) vía open_clip (FR-005 · ADR-0005 · contracts §3).

    Cumple el contrato `EmbeddingProvider` (contracts §3): `model_id`,
    `dimension` y `embed_images(images) -> np.ndarray (N, D) float32 con
    filas L2-normalizadas`. El modelo se carga una sola vez (lazy) y se
    reutiliza entre llamadas; el lote se procesa en sub-batches
    (`batch_size`) para acotar memoria en CPU (FR-005: batches).
    """

    def __init__(
        self,
        model_name: str = "ViT-B-16-SigLIP",
        pretrained: str = "webli",
        dimension: int | None = None,
        batch_size: int = 32,
        device: str = "cpu",
    ) -> None:
        """Constructor sin carga de pesos: torch/open_clip se importan al primer uso.

        Args:
            model_name: nombre del modelo open_clip (SigLIP v1).
            pretrained: identificador de pesos pretrained (p. ej. `webli`).
            dimension: override de D; si es None se usa el mapa conocido del
                modelo (768 para ViT-B-16-SigLIP) y, si el modelo no está en
                el mapa, se resuelve cargando el modelo (embed_dim).
            batch_size: tamaño de sub-batch en `embed_images`.
            device: dispositivo torch (spike: `cpu`).
        """
        if batch_size < 1:
            raise ValueError(f"batch_size debe ser >= 1, se recibió {batch_size}")
        if dimension is not None and dimension < 1:
            raise ValueError(f"dimension debe ser >= 1, se recibió {dimension}")
        self.model_name = model_name
        self.pretrained = pretrained
        self.batch_size = batch_size
        self.device = device
        self._dimension_override = dimension
        self._model_id_value = f"openclip-{model_name}-{pretrained}"
        # Estado lazy (se rellena en `_ensure_loaded`); Any: torch/open_clip
        # no tienen stubs de tipos y solo existen con el extra `siglip`.
        self._model: Any = None
        self._preprocess: Any = None
        self._torch: Any = None

    @property
    def model_id(self) -> str:  # type: ignore[override]  # el contrato no exige mutabilidad
        """Identificador estable del proveedor (contracts §3)."""
        return self._model_id_value

    @property
    def dimension(self) -> int:  # type: ignore[override]  # el contrato no exige mutabilidad
        """Dimensión D de los vectores devueltos (contracts §3).

        D queda fijada por el modelo elegido en PR-005 (768 para
        ViT-B-16-SigLIP); si hay override explícito, gana el override.
        """
        if self._dimension_override is not None:
            return self._dimension_override
        known = _MODEL_DIMENSIONS.get(self.model_name)
        if known is not None:
            return known
        # Modelo fuera del mapa: se resuelve con una pasada forward de sonda
        # (los modelos de open_clip no exponen un atributo de dimensión común).
        self._ensure_loaded()
        return self._probe_dimension()

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        """Embedding del lote (contracts §3): shape (N, D), float32, filas L2-normalizadas.

        Procesa en sub-batches de `batch_size` (FR-005) y devuelve el
        resultado concatenado; el orden de las filas coincide con el orden
        de `images`. Un lote vacío devuelve shape (0, D).
        """
        self._ensure_loaded()
        torch = self._torch
        if len(images) == 0:
            return np.zeros((0, self.dimension), dtype=np.float32)
        chunks: list[np.ndarray] = []
        with torch.no_grad():
            for start in range(0, len(images), self.batch_size):
                batch = images[start : start + self.batch_size]
                batch_tensor = torch.stack([self._preprocess(image) for image in batch])
                features = self._model.encode_image(batch_tensor)
                chunks.append(features.detach().cpu().numpy())
        vectors = np.concatenate(chunks, axis=0).astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0.0):
            raise ValueError(
                f"SiglipLocalProvider: vector degenerado (norma 0) con model={self.model_name}"
            )
        normalized: np.ndarray = vectors / norms
        return normalized

    def _ensure_loaded(self) -> None:
        """Carga lazy del modelo (torch/open_clip importados solo aquí).

        El extra `siglip` solo se necesita en este punto: los imports BASE
        (módulo, gates ruff/mypy/pytest) nunca tocan torch ni open_clip.
        """
        if self._model is not None:
            return
        open_clip = importlib.import_module("open_clip")
        torch = importlib.import_module("torch")
        model, _, preprocess = open_clip.create_model_and_transforms(
            self.model_name,
            pretrained=self.pretrained,
            device=self.device,
        )
        model.eval()
        self._model = model
        self._preprocess = preprocess
        self._torch = torch

    def _probe_dimension(self) -> int:
        """Dimensión de salida del modelo mediante una pasada forward de sonda.

        Usado solo para modelos no incluidos en `_MODEL_DIMENSIONS`: la
        salida de `encode_image` tiene shape (1, D) y D es el ancho del
        tower visual (p. ej. 768 para ViT-B-16-SigLIP, 1024 para ViT-L-16).
        """
        torch = self._torch
        probe = self._preprocess(Image.new("RGB", (8, 8))).unsqueeze(0)
        with torch.no_grad():
            features = self._model.encode_image(probe)
        return int(features.shape[-1])
