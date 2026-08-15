"""Búsqueda por imagen del spike (PR-012 · FR-010 · FR-012 parcial · contracts §1).

`search/image_search.py`: pipeline normalizar → pHash → embed → ANN →
agrupar por `video_id`, sobre las interfaces `VectorStore` y
`EmbeddingProvider` (ADR-0007). El ranking configurable con match score
y evidencia pHash es PR-013 (`search/ranking.py`).
"""

from xtrace_spike.search.image_search import (
    DEFAULT_TOP_K,
    ImageSearch,
    ImageSearchResult,
    VideoCandidate,
    group_hits_by_video,
    normalize_query_image,
)

__all__ = [
    "DEFAULT_TOP_K",
    "ImageSearch",
    "ImageSearchResult",
    "VideoCandidate",
    "group_hits_by_video",
    "normalize_query_image",
]
