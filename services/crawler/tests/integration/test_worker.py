"""Tests de integración del worker contra Supabase local (PR-027 · ADR-0010 · contracts §3).

Cubren el despacho real del worker con `JobsRepo` (PR-026) sobre la tabla
`jobs` de la migración PR-025 (data-model.md), trazados a
FR-006/FR-008/FR-010 · SC-008:

- **despacho real** (FR-006): `JobWorker.run_once` reclama con
  `FOR UPDATE SKIP LOCKED` y completa jobs en BD;
- **crash simulado → lease reset** (ADR-0010): un `running` con lease vencido
  (crash de worker) vuelve a `pending` vía `reset_stale_leases` dentro de
  `run_once` y el job se procesa hasta `done`;
- **estados terminales garantizados en BD** (FR-008): fallo terminal del
  handler (removed tipado del mock, nota revisión PR-021) → `unavailable`;
  fallo transitorio → `pending` con `not_before` futuro (backoff); job sin
  handler → `failed` definitivo; nada queda colgado en `running`;
- **aislamiento SC-008**: un handler que falla no impide que los jobs de otra
  fuente terminen, y el worker sigue operativo después.

Se skippean si la DB local no es alcanzable o la migración PR-025 no está
aplicada (CI sin Supabase): misma comprobación en recolección que
`test_jobs_repo` (patrón del spike), sin skips forzados. Cada test limpia
`jobs` al inicio (constitución §6).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import psycopg
import pytest
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]

from xtrace_crawler.adapters.mock import MockAdapterRemovedError, MockAdapterTransientError
from xtrace_crawler.jobs.repo import JobsRepo
from xtrace_crawler.jobs.types import Job, JobStatus, JobType
from xtrace_crawler.jobs.worker import JobWorker


def _db_available() -> bool:
    """¿Supabase local alcanzable y migración PR-025 aplicada? (patrón del spike)."""
    try:
        with psycopg.connect(resolve_dsn(), connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1 from public.jobs limit 0")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable o sin migración PR-025 (CI sin DB): "
    "integration worker saltada",
)


@pytest.fixture(autouse=True)
def _clean_jobs() -> None:
    """Estado `jobs` limpio por test (constitución §6; DELETE, patrón test_jobs_repo)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("delete from public.jobs")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del worker (sin pytest-asyncio, estilo PR-007/PR-023)."""
    return asyncio.run(coro)


async def _connect() -> psycopg.AsyncConnection[dict[str, Any]]:
    """Conexión auxiliar de los tests para leer/ajustar el estado de la BD."""
    return await psycopg.AsyncConnection.connect(resolve_dsn(), row_factory=psycopg.rows.dict_row)


async def _all_jobs() -> list[Job]:
    """Todos los jobs de la BD (estado sin pasar por el repo)."""
    async with await _connect() as conn:
        cur = await conn.execute("select * from public.jobs order by created_at")
        rows = await cur.fetchall()
    return [Job.model_validate(row) for row in rows]


async def _get_job(job_id: uuid.UUID) -> Job:
    """Lee un job directamente de la BD (estado sin pasar por el repo)."""
    async with await _connect() as conn:
        cur = await conn.execute("select * from public.jobs where id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)


async def _expire_lease(job_id: uuid.UUID) -> None:
    """Vence el lease de un job `running` (simula crash de worker hace 1 h)."""
    async with await _connect() as conn:
        await conn.execute(
            "update public.jobs set locked_at = now() - interval '1 hour' where id = %s",
            (job_id,),
        )
        await conn.commit()


async def _not_before_delay_seconds(job_id: uuid.UUID) -> float:
    """Segundos que faltan para `not_before` (now() de la BD, sin skew de reloj)."""
    async with await _connect() as conn:
        cur = await conn.execute(
            "select extract(epoch from (not_before - now())) as delay from public.jobs "
            "where id = %s",
            (job_id,),
        )
        row = await cur.fetchone()
    assert row is not None and row["delay"] is not None
    return float(row["delay"])


# --- Despacho real (FR-006) -----------------------------------------------------------


def test_run_once_processes_real_jobs_to_done() -> None:
    """`run_once` con `JobsRepo` real: claim SKIP LOCKED → handler → `done` en BD (FR-006)."""

    async def _scenario() -> None:
        repo = JobsRepo()
        await repo.enqueue(JobType.DISCOVER, payload={"n": 1})
        await repo.enqueue(JobType.FETCH_METADATA, payload={"n": 2})
        seen: list[tuple[JobType, dict[str, Any]]] = []

        async def handler(job: Job) -> None:
            seen.append((job.job_type, dict(job.payload)))

        worker = JobWorker(repo, concurrency=2, worker_id="it-worker-a")
        worker.register_handler(JobType.DISCOVER, handler)
        worker.register_handler(JobType.FETCH_METADATA, handler)

        assert await worker.run_once() == 2
        assert len(seen) == 2
        assert {job_type.value for job_type, _ in seen} == {"DISCOVER", "FETCH_METADATA"}
        for job in await _all_jobs():
            assert job.status is JobStatus.DONE  # terminal en BD
            assert job.locked_by == "it-worker-a"  # rastro de lease (FR-014)

    _run(_scenario())


# --- Crash simulado → lease reset (ADR-0010) -----------------------------------------


def test_crash_simulado_recovers_via_lease_reset() -> None:
    """Un job `running` con lease vencido (crash) vuelve a `pending` y se procesa.

    El lease reset ocurre dentro de `run_once` (antes de procesar): el job
    reseteado es reclamado y completado en la misma pasada (ADR-0010, spec
    edge case: crash del worker a mitad de job).
    """

    async def _scenario() -> None:
        repo = JobsRepo()
        job = await repo.enqueue(JobType.FETCH_METADATA)
        claimed = await repo.claim_next("worker-crashed")
        assert claimed is not None and claimed.id == job.id
        assert claimed.status is JobStatus.RUNNING
        await _expire_lease(job.id)  # lease vencido hace 1 h (crash simulado)

        seen: list[uuid.UUID] = []

        async def handler(processed: Job) -> None:
            seen.append(processed.id)

        worker = JobWorker(repo, concurrency=1, worker_id="it-worker-b")
        worker.register_handler(JobType.FETCH_METADATA, handler)

        assert await worker.run_once() == 1
        assert seen == [job.id]  # el handler volvió a ejecutarlo
        final = await _get_job(job.id)
        assert final.status is JobStatus.DONE
        assert final.locked_by == "it-worker-b"  # reclamado por el worker nuevo

    _run(_scenario())


# --- Estados terminales garantizados en BD (FR-008 · contracts §3) -------------------


def test_terminal_handler_failure_marks_unavailable_in_db() -> None:
    """Fallo terminal del handler (removed tipado del mock, PR-021) → `unavailable` en BD.

    Sin reintentos: el job `unavailable` no vuelve a despacharse (FR-008 · spec
    edge case: 404/removed → terminal sin reintentos infinitos).
    """

    async def _scenario() -> None:
        repo = JobsRepo()
        await repo.enqueue(JobType.CHECK_AVAILABILITY)

        async def handler(_job: Job) -> None:
            raise MockAdapterRemovedError("mock adapter: video removed (404)")

        worker = JobWorker(repo, concurrency=1, worker_id="it-worker-c")
        worker.register_handler(JobType.CHECK_AVAILABILITY, handler)

        await worker.run_once()
        final = (await _all_jobs())[0]
        assert final.status is JobStatus.UNAVAILABLE
        assert final.error == "MockAdapterRemovedError: mock adapter: video removed (404)"
        assert await repo.claim_next("it-worker-d") is None  # sin reintentos

    _run(_scenario())


def test_transient_handler_failure_schedules_backoff_in_db() -> None:
    """Fallo transitorio del handler → `pending` con `not_before` futuro (backoff, FR-008).

    Se inyecta `delay_fn` (1 s fijo, patrón de `test_jobs_repo`): el jitter
    completo de `backoff.py` puede dar ~0 y la latencia de medición haría el
    aserto flaky; la matemática del jitter la valida `test_backoff.py` (PR-023).
    """

    async def _scenario() -> None:
        repo = JobsRepo(delay_fn=lambda _attempts: 1.0)
        await repo.enqueue(JobType.FETCH_METADATA)

        async def handler(_job: Job) -> None:
            raise MockAdapterTransientError("429 too many requests")

        worker = JobWorker(repo, concurrency=1, worker_id="it-worker-e")
        worker.register_handler(JobType.FETCH_METADATA, handler)

        await worker.run_once()
        final = (await _all_jobs())[0]
        assert final.status is JobStatus.PENDING  # reintentable
        assert final.attempts == 1
        assert final.error == "MockAdapterTransientError: 429 too many requests"
        assert await _not_before_delay_seconds(final.id) > 0.0  # backoff programado

    _run(_scenario())


def test_job_without_handler_fails_terminal_in_db() -> None:
    """Un job sin handler registrado acaba `failed` definitivo en BD (estados terminales)."""

    async def _scenario() -> None:
        repo = JobsRepo()
        await repo.enqueue(JobType.REINDEX)

        worker = JobWorker(repo, concurrency=1, worker_id="it-worker-f")
        await worker.run_once()  # sin handlers registrados

        final = (await _all_jobs())[0]
        assert final.status is JobStatus.FAILED
        assert final.error is not None and "no hay handler registrado" in final.error
        assert await repo.claim_next("it-worker-g") is None  # terminal: sin reintentos

    _run(_scenario())


# --- Aislamiento por fuente (SC-008 · FR-010) ----------------------------------------


def test_isolation_sc008_one_failing_source_does_not_block_others() -> None:
    """SC-008 contra BD real: una fuente que falla no impide los jobs de otra.

    Con `max_attempts=1` el fallo transitorio de la fuente A queda `failed`
    definitivo en el primer intento (determinista); la fuente B completa; el
    worker sigue operativo y procesa un job nuevo después.
    """

    async def _scenario() -> None:
        repo = JobsRepo()
        await repo.enqueue(JobType.DISCOVER, max_attempts=1)  # fuente A caída
        await repo.enqueue(JobType.DISCOVER, max_attempts=1)  # fuente A caída
        await repo.enqueue(JobType.CHECK_AVAILABILITY)  # fuente B sana

        async def failing(_job: Job) -> None:
            raise MockAdapterTransientError("source A down")

        async def healthy(_job: Job) -> None:
            return None

        worker = JobWorker(repo, concurrency=2, worker_id="it-worker-h")
        worker.register_handler(JobType.DISCOVER, failing)
        worker.register_handler(JobType.CHECK_AVAILABILITY, healthy)

        assert await worker.run_once() == 3  # ningún fallo de handler tumba la pasada
        jobs = await _all_jobs()
        assert all(job.status is not JobStatus.RUNNING for job in jobs)  # nada colgado
        by_type: dict[JobType, Job] = {job.job_type: job for job in jobs}
        assert by_type[JobType.CHECK_AVAILABILITY].status is JobStatus.DONE
        assert by_type[JobType.DISCOVER].status is JobStatus.FAILED  # max_attempts agotado
        assert by_type[JobType.DISCOVER].error == "MockAdapterTransientError: source A down"

        # el worker sigue operativo: el job nuevo de la fuente sana se procesa.
        await repo.enqueue(JobType.CHECK_AVAILABILITY)
        assert await worker.run_once() == 1
        jobs_after = await _all_jobs()
        done = [job for job in jobs_after if job.status is JobStatus.DONE]
        assert len(done) == 2  # la fuente B sana procesó ambos

    _run(_scenario())
