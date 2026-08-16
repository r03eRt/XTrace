"""Repositorio de la cola `jobs` en Postgres (PR-026 · FR-006/FR-008 · ADR-0010 · contracts §3).

Semántica de la cola sobre la tabla `jobs` de la migración PR-025 (data-model.md):

- `enqueue`: persiste un job nuevo; con `dedupe_key` evita duplicar jobs **activos**
  (no terminales) con la misma clave de payload (tasks.md PR-026: dedupe por
  unicidad de payload cuando aplique; sin índice único en la BD, es best-effort).
- `claim_next`: despacho canónico `status='pending' AND not_before<=now() ORDER BY
  created_at FOR UPDATE SKIP LOCKED LIMIT 1` y marca `running`/`locked_by`/
  `locked_at` en la **misma transacción** (FR-006 · ADR-0010 · contracts §3).
- `complete`/`fail`/`unavailable`: transiciones a estados terminales o de reintento
  (FR-008). `fail` usa `jobs/backoff.py` (PR-023) para programar `not_before`.
- `reset_stale_leases`: los `running` con lease vencido vuelven a `pending` (crash
  de worker, ADR-0010).

Conexiones psycopg async con `row_factory=dict_row` y DSN vía `resolve_dsn()` del
spike (ADR-0011: `SUPABASE_DB_URL` o Supabase local por defecto). Cada operación
abre su propia conexión y transacción (patrón del spike; sin pool en v1).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# El spike no declara py.typed (intocable, ADR-0011): resolve_dsn se tipa como Any.
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]

from xtrace_crawler.jobs.backoff import next_attempt_delay
from xtrace_crawler.jobs.types import Job, JobType


class JobsRepo:
    """Acceso a la cola `jobs` (FR-006/FR-008 · ADR-0010 · contracts §3).

    Un job no encontrado por id en `complete`/`fail`/`unavailable` es un error
    de uso (el worker reclama antes de operar): se eleva `ValueError`.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        delay_fn: Callable[[int], float] | None = None,
    ) -> None:
        """Crea el repo; `delay_fn` permite inyectar el retraso de reintento.

        Por defecto `fail` usa `next_attempt_delay` de `jobs/backoff.py`
        (PR-023). `delay_fn` (número de fallos → segundos) es un punto de
        inyección para determinismo en tests (NFR-003), mismo patrón que el
        parámetro `rng` de `backoff.py`.
        """
        self._dsn = dsn or resolve_dsn()
        self._delay_fn = delay_fn

    async def _connect(self) -> psycopg.AsyncConnection[dict[str, Any]]:
        """Nueva conexión async (filas como dict; transacciones explícitas)."""
        return await psycopg.AsyncConnection.connect(
            self._dsn, row_factory=dict_row, autocommit=False
        )

    async def enqueue(
        self,
        job_type: JobType,
        *,
        source_id: uuid.UUID | None = None,
        video_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
        not_before: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> Job:
        """Persiste un job en estado `pending` y lo devuelve (FR-006).

        Con `dedupe_key`, la clave se persiste en `payload["dedupe_key"]` y, si ya
        existe un job **no terminal** (`pending`/`running`/`failed`) del mismo
        `job_type` con esa clave, se devuelve el existente sin insertar (tasks.md
        PR-026). Sin índice único en la BD (migración PR-025) el dedupe es
        best-effort: suficiente para evitar duplicados en operación normal.

        Args:
            job_type: tipo del job (DATA-002 · contracts §3).
            source_id: fuente opcional asociada (FK → sources, `ON DELETE SET NULL`).
            video_id: vídeo opcional asociado (FK → videos, `ON DELETE CASCADE`).
            payload: parámetros del job (cursor, limit, …); default `{}`.
            max_attempts: tope de reintentos (FR-008); default 3 (data-model.md).
            not_before: programación inicial (backoff); default `now()` de la BD.
            dedupe_key: clave de unicidad de payload para no duplicar jobs activos.
        """
        data = dict(payload or {})
        if dedupe_key is not None and "dedupe_key" not in data:
            data["dedupe_key"] = dedupe_key
        async with await self._connect() as conn:
            async with conn.transaction():
                if dedupe_key is not None:
                    existing = await self._find_active_by_dedupe_key(conn, job_type, dedupe_key)
                    if existing is not None:
                        return existing
                cur = await conn.execute(
                    "insert into public.jobs "
                    "  (job_type, status, source_id, video_id, payload, max_attempts, not_before) "
                    "values (%s, 'pending', %s, %s, %s, %s, coalesce(%s, now())) "
                    "returning *",
                    (job_type.value, source_id, video_id, Jsonb(data), max_attempts, not_before),
                )
                row = await cur.fetchone()
        assert row is not None  # INSERT ... RETURNING siempre devuelve la fila
        return Job.model_validate(row)

    async def claim_next(self, worker_id: str) -> Job | None:
        """Reclama el siguiente job elegible para `worker_id` (FR-006 · ADR-0010).

        Despacho canónico (data-model.md): `status='pending' AND not_before<=now()`
        ordenado por `created_at`, con `FOR UPDATE SKIP LOCKED` — la fila elegida
        queda bloqueada para el resto de workers — y la marca `running`/
        `locked_by`/`locked_at` ocurre en la **misma transacción** (contracts §3).
        Devuelve `None` si no hay jobs elegibles.
        """
        async with await self._connect() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "update public.jobs "
                    "set status = 'running', locked_by = %s, locked_at = now() "
                    "where id = ("
                    "  select id from public.jobs "
                    "  where status = 'pending' and not_before <= now() "
                    "  order by created_at "
                    "  for update skip locked "
                    "  limit 1"
                    ") "
                    "returning *",
                    (worker_id,),
                )
                row = await cur.fetchone()
        return Job.model_validate(row) if row is not None else None

    async def complete(self, job_id: uuid.UUID) -> Job:
        """Marca el job como `done` (éxito) y limpia el error (FR-008).

        Conserva `locked_by`/`locked_at` como rastro de quién lo completó
        (observabilidad, FR-014). Un job `done` es terminal: no se redespacha.
        """
        async with await self._connect() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "update public.jobs set status = 'done', error = null "
                    "where id = %s returning *",
                    (job_id,),
                )
                row = await cur.fetchone()
        if row is None:
            raise ValueError(f"job no encontrado: {job_id}")
        return Job.model_validate(row)

    async def fail(
        self,
        job_id: uuid.UUID,
        error: str,
        *,
        terminal: bool = False,
    ) -> Job:
        """Registra el fallo `error` de un job (FR-008 · contracts §3 · ADR-0010).

        Siempre incrementa `attempts` y registra `error`. Después:

        - error **terminal** (`terminal=True`, p. ej. bloqueo robots/ToS) o
          intentos agotados (`attempts >= max_attempts`) → `failed` definitivo,
          sin reintentos (contracts §3);
        - fallo transitorio con intentos restantes → vuelve a `pending` con
          `not_before = now() + next_attempt_delay(attempts)` (jobs/backoff.py,
          PR-023: backoff exponencial base 1 s, factor 2, cap 1 h, jitter completo).
        """
        async with await self._connect() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "update public.jobs set attempts = attempts + 1, error = %s "
                    "where id = %s returning *",
                    (error, job_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ValueError(f"job no encontrado: {job_id}")
                job = Job.model_validate(row)
                if terminal or job.attempts >= job.max_attempts:
                    cur = await conn.execute(
                        "update public.jobs set status = 'failed' where id = %s returning *",
                        (job_id,),
                    )
                else:
                    delay = (
                        self._delay_fn(job.attempts)
                        if self._delay_fn is not None
                        else next_attempt_delay(job.attempts)
                    )
                    cur = await conn.execute(
                        "update public.jobs set status = 'pending', "
                        "not_before = now() + make_interval(secs => %s) "
                        "where id = %s returning *",
                        (delay, job_id),
                    )
                final_row = await cur.fetchone()
        assert final_row is not None  # la fila ya existe (validado arriba)
        return Job.model_validate(final_row)

    async def unavailable(self, job_id: uuid.UUID, error: str | None = None) -> Job:
        """Marca el job como `unavailable` definitivo (FR-008 · contracts §3).

        Para contenido retirado o no disponible (404/removed): el job deja de
        consumir reintentos (spec edge cases). Conserva el error anterior si no
        se aporta uno nuevo.
        """
        async with await self._connect() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "update public.jobs set status = 'unavailable', "
                    "error = coalesce(%s, error) where id = %s returning *",
                    (error, job_id),
                )
                row = await cur.fetchone()
        if row is None:
            raise ValueError(f"job no encontrado: {job_id}")
        return Job.model_validate(row)

    async def reset_stale_leases(self, timeout_seconds: float) -> int:
        """Devuelve a `pending` los `running` con lease vencido (ADR-0010).

        Crash de worker: los jobs `running` con `locked_at <= now() - timeout`
        vuelven a `pending` y se limpia el lease (`locked_by`/`locked_at`),
        quedando elegibles para otro worker. Devuelve el nº de jobs reseteados.
        """
        async with await self._connect() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "update public.jobs set status = 'pending', locked_by = null, "
                    "locked_at = null "
                    "where status = 'running' "
                    "and locked_at <= now() - make_interval(secs => %s)",
                    (timeout_seconds,),
                )
                count = cur.rowcount
        assert count is not None
        return int(count)

    async def _find_active_by_dedupe_key(
        self,
        conn: psycopg.AsyncConnection[dict[str, Any]],
        job_type: JobType,
        dedupe_key: str,
    ) -> Job | None:
        """Job activo (no terminal) con el mismo `job_type` y `payload->>'dedupe_key'`."""
        cur = await conn.execute(
            "select * from public.jobs "
            "where job_type = %s and payload ->> 'dedupe_key' = %s "
            "and status <> 'done' and status <> 'unavailable' "
            "limit 1",
            (job_type.value, dedupe_key),
        )
        row = await cur.fetchone()
        return Job.model_validate(row) if row is not None else None
