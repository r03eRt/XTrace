"""Analítica de búsquedas sin media (PR-055 · FR-012 · DATA-001 · SEC-005).

`record_search` inserta una fila en `searches` (tabla existente del spike,
DATA-001: sin migraciones) por cada búsqueda aceptada: `id = search_id`,
`search_type='image'`, `processing_ms`, `results_count` (contracts §7.6).
Nada más se registra: la analítica **no contiene media ni nombres de
fichero** (SEC-005). El TTL (cleanup por `created_at`, configurable) llega
en PR-056.
"""

from __future__ import annotations

import asyncio
import logging

from xtrace_spike.repo import PgRepo, parse_uuid  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def record_search(*, search_id: str, processing_ms: int, results_count: int) -> None:
    """Registra una búsqueda aceptada en `searches` (FR-012, best-effort).

    Un fallo de registro (p. ej. la BD cae entre la búsqueda y el insert) se
    registra como warning **sin enmascarar el resultado** de la búsqueda
    (mismo criterio que el fallo de borrado de media del edge case de la
    spec): la analítica no bloquea la respuesta 200.
    """
    try:
        asyncio.run(_insert_search(search_id, processing_ms, results_count))
    except Exception:
        logger.warning(
            "no se pudo registrar la búsqueda %s en searches (analítica; la búsqueda no falló)",
            search_id,
            exc_info=True,
        )


async def _insert_search(search_id: str, processing_ms: int, results_count: int) -> None:
    """Insert en `searches` (FR-012): el `id` de la fila es el `search_id`."""
    search_uuid = parse_uuid(search_id, "search_id")
    async with await PgRepo().connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "insert into public.searches (id, search_type, processing_ms, results_count) "
                "values (%s, 'image', %s, %s)",
                (search_uuid, processing_ms, results_count),
            )
