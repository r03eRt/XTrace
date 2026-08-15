"""Pipeline de indexación del spike (PR-010 · FR-005/006/007/008/009 · SC-005/006
· ADR-0006/0007).

Orquesta la cadena ingest→dedupe→embed(batch)→VectorStore.upsert por vídeo
sobre las interfaces `VectorStore`, `EmbeddingProvider` y `VideoStateStore`
(ADR-0007), gestiona el estado del vídeo (FR-007) y garantiza el cleanup de
temporales en try/finally (FR-009 · SC-006).

- `indexing/state.py`: estado del vídeo (`VideoStateStore` con impl. en
  memoria para tests y PostgreSQL para producción, FR-007).
- `indexing/pipeline.py`: `IndexingPipeline` (inyección de dependencias),
  ids estables por vídeo/frame (FR-008) y configuración (`IndexingConfig`).
"""

from xtrace_spike.indexing.pipeline import (
    IndexingConfig,
    IndexingPipeline,
    IndexingReport,
    VideoIndexingResult,
    frame_id_for,
    video_id_for,
)
from xtrace_spike.indexing.state import (
    InMemoryVideoStateStore,
    PgVideoStateStore,
    VideoStateStore,
)

__all__ = [
    "IndexingConfig",
    "IndexingPipeline",
    "IndexingReport",
    "VideoIndexingResult",
    "VideoStateStore",
    "InMemoryVideoStateStore",
    "PgVideoStateStore",
    "frame_id_for",
    "video_id_for",
]
