"""Contrato `VectorStore` (FR-006 · ADR-0004/0007 · contracts §2).

Tipos y Protocol estables que toda implementación (in-memory, pgvector) debe
respetar. Los cambios a este contrato requieren actualizar spec/plan primero
(ver `specs/001-visual-search-spike/contracts/README.md`).

Invariantes (contracts §5): embeddings L2-normalizados y distancia coseno en el
ANN (menor = más similar).
"""

from collections.abc import Sequence
from typing import Protocol, TypedDict


class FrameHit(TypedDict):
    """Frame candidato devuelto por `ann_search` (contracts §2)."""

    frame_id: str
    video_id: str
    timestamp_ms: int | None
    distance: float  # menor = más similar (coseno)


class FrameRecord(TypedDict):
    """Registro mínimo que el VectorStore necesita para indexar un frame.

    Los campos adicionales del modelo de datos (pHash, dimensiones, `source_kind`,
    …) viven en la tabla `frames` (`data-model.md`) y quedan fuera de la
    responsabilidad del VectorStore.
    """

    frame_id: str
    video_id: str
    timestamp_ms: int | None
    embedding: Sequence[float]


class VectorStoreStats(TypedDict):
    """Conteo de filas almacenadas (mismo criterio que contar filas en la DB)."""

    videos: int
    frames: int
    vectors: int


class VectorStore(Protocol):
    """Índice vectorial consultable por similitud (FR-006, ADR-0004/0007)."""

    async def upsert_frames(self, frames: Sequence[FrameRecord]) -> int: ...
    async def ann_search(
        self, embedding: Sequence[float], k: int, exclude_videos: bool = True
    ) -> list[FrameHit]: ...
    async def delete_video(self, video_id: str) -> None: ...
    async def stats(self) -> VectorStoreStats: ...
