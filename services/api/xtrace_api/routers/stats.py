"""GET /stats (PR-056 · FR-007 · contracts §3).

Métricas básicas del índice con **los mismos campos que la CLI `stats`** del
spike (coherencia FR-007): `videos` (vídeos con ≥ 1 frame indexado),
`frames`, `vectors`, `backend` (etiqueta estable `postgres` | `in-memory`) y
`embedding_provider` (`model_id` del proveedor activo).

La resolución del backend y del proveedor usa la **misma regla que la CLI**
(`deps.get_search_components` → `xtrace_spike.cli.build_backend` +
`resolve_embedding_provider`): paridad por construcción. Con la BD caída, el
`psycopg.Error` de `VectorStore.stats()` se traduce a 503
`index_unavailable` (handler global de main.py, contracts §5).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from xtrace_api.deps import get_search_components
from xtrace_api.schemas import Stats

router = APIRouter(tags=["stats"])


@router.get("/stats", response_model=Stats)
def stats() -> Stats:
    """Métricas del índice (FR-007 · contracts §3), mismas que la CLI `stats`.

    Handler **sync** (threadpool de FastAPI): la cadena async del spike se
    ejecuta con `asyncio.run` (mismo patrón que la CLI y que `POST /search`).
    """
    components = get_search_components()
    index_stats = asyncio.run(components.backend.store.stats())
    return Stats(
        videos=index_stats["videos"],
        frames=index_stats["frames"],
        vectors=index_stats["vectors"],
        backend=components.backend.label,
        embedding_provider=components.embeddings.model_id,
    )
