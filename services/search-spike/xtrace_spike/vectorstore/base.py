"""Contrato `VectorStore` (FR-006 · ADR-0004/0007 · contracts §2).

Tipos y Protocol estables que toda implementación (in-memory, pgvector) debe
respetar. Los cambios a este contrato requieren actualizar spec/plan primero
(ver `specs/001-visual-search-spike/contracts/README.md`).

Invariantes (contracts §5): embeddings L2-normalizados y distancia coseno en el
ANN (menor = más similar). Cada `FrameRecord` transporta el pHash del frame
(FR-004/FR-006, FIX-phash): la firma perceptual real del frame representativo
se persiste en el índice junto a video_id, timestamp y embedding.
"""

from collections.abc import Sequence
from typing import Protocol, TypedDict, runtime_checkable


class FrameHit(TypedDict):
    """Frame candidato devuelto por `ann_search` (contracts §2)."""

    frame_id: str
    video_id: str
    timestamp_ms: int | None
    distance: float  # menor = más similar (coseno)


class FrameRecord(TypedDict):
    """Registro mínimo que el VectorStore necesita para indexar un frame.

    El pHash viaja en el contrato (FIX-phash · FR-004/FR-006): es la firma
    perceptual de 64 bits del frame representativo (salida de
    `hashing.phash.compute_phash`, PR-004), la misma que usa el dedupe
    (FR-003). Ambas implementaciones lo persisten; `PgVectorStore` lo guarda
    en la columna `frames.phash` (bigint) con una codificación con signo
    (ver `phash_to_db` y `phash_from_db` en pgvector.py). Los campos
    restantes del modelo de datos (dimensiones, `source_kind`, …) viven en
    la tabla `frames` (`data-model.md`) y quedan fuera del contrato.
    """

    frame_id: str
    video_id: str
    timestamp_ms: int | None
    phash: int
    embedding: Sequence[float]


class VectorStoreStats(TypedDict):
    """Conteo de filas almacenadas (mismo criterio que contar filas en la DB)."""

    videos: int
    frames: int
    vectors: int


class VideoIndexMetadata(TypedDict):
    """Committed metadata needed to hydrate an external video-state store."""

    frame_count: int
    duration_ms: int | None


@runtime_checkable
class VideoIndexMetadataProvider(Protocol):
    """Optional public capability for stores that expose committed metadata.

    PostgreSQL keeps the metadata and frames in the same transaction and does
    not implement this read-back capability; the local in-memory pipeline uses
    it when a fresh state store is paired with an existing vector store.
    """

    async def get_video_index_metadata(self, video_id: str) -> VideoIndexMetadata | None: ...


class VectorStore(Protocol):
    """Índice vectorial consultable por similitud (FR-006, ADR-0004/0007)."""

    async def upsert_frames(self, frames: Sequence[FrameRecord]) -> int: ...
    async def replace_video_index(
        self,
        video_id: str,
        frames: Sequence[FrameRecord],
        *,
        duration_ms: int | None,
    ) -> None: ...
    async def ann_search(
        self, embedding: Sequence[float], k: int, exclude_videos: bool = True
    ) -> list[FrameHit]: ...
    async def delete_video(self, video_id: str) -> None: ...
    async def stats(self) -> VectorStoreStats: ...
