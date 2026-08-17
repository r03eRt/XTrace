"""Analítica de búsquedas sin media (PR-055/056 · FR-012 · DATA-001 · SEC-005).

`record_search` inserta una fila en `searches` (tabla existente del spike,
DATA-001: sin migraciones) por cada búsqueda aceptada: `id = search_id`,
`search_type='image'`, `processing_ms`, `results_count` (contracts §7.6).
Nada más se registra: la analítica **no contiene media ni nombres de
fichero** (SEC-005).

TTL sin migración (PR-056 · data-model.md): `delete_expired_searches` borra
las filas con `created_at` vencido (`created_at < now() - TTL`); el lifespan
del servicio ejecuta un **purge inicial al arrancar** y `searches_ttl_loop`
lo repite cada intervalo configurado. Ambas vías son best-effort (un fallo
de BD se loguea y se reintenta; la analítica nunca rompe el servicio).
"""

from __future__ import annotations

import asyncio
import logging

from xtrace_spike.repo import PgRepo, parse_uuid  # type: ignore[import-untyped]

from xtrace_api.config import Settings

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


# ---------------------------------------------------------------------------
# TTL de `searches` sin migración (PR-056 · FR-012 · DATA-001 · data-model.md)
# ---------------------------------------------------------------------------


async def delete_expired_searches(ttl_days: int, *, repo: PgRepo | None = None) -> int:
    """Borra las búsquedas con `created_at` vencido; devuelve el nº de filas.

    SQL del data-model.md (sin cambio de esquema): `delete ... where
    created_at < now() - make_interval(days => <ttl_days>)`. `repo` es
    inyectable en tests (repo fake; default `PgRepo` con credenciales de
    servidor, SEC-004). Puede lanzar `psycopg.Error` si la BD no está
    disponible — los llamadores (lifespan) lo tratan como best-effort.
    """
    active_repo = repo or PgRepo()
    async with await active_repo.connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "delete from public.searches where created_at < now() - make_interval(days => %s)",
                (ttl_days,),
            )
            return int(cur.rowcount)


async def searches_ttl_round(settings: Settings) -> None:
    """Una iteración del cleanup TTL: best-effort (nunca lanza).

    Un fallo de BD se loguea como warning (sin media ni datos sensibles,
    SEC-005) y el loop lo reintenta en el siguiente intervalo.
    """
    try:
        await delete_expired_searches(settings.searches_ttl_days)
    except Exception:
        logger.warning(
            "TTL de searches: cleanup falló; se reintenta en el siguiente intervalo",
            exc_info=True,
        )


async def searches_ttl_loop(settings: Settings) -> None:
    """Cleanup periódico de `searches` (FR-012 · data-model.md).

    Repite el purge cada `searches_ttl_cleanup_min` minutos; el **purge
    inicial al arrancar** lo ejecuta el lifespan. El loop no termina nunca:
    el cleanup es best-effort por iteración (`searches_ttl_round`).
    """
    while True:
        await searches_ttl_round(settings)
        await asyncio.sleep(settings.searches_ttl_cleanup_min * 60)
