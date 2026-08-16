"""Tests de integración del repositorio de jobs (PR-026 · FR-006/FR-008 · ADR-0010 · contracts §3).

Cubren la semántica de la cola `jobs` contra Supabase local (data-model.md):

- **claim único entre N conexiones concurrentes** (FR-006): el despacho con
  `FOR UPDATE SKIP LOCKED` garantiza que ningún job es reclamado por dos workers;
- **despacho FIFO y `not_before`**: se toma el pendiente más antiguo y un job
  programado en el futuro no se despacha hasta su `not_before` (ADR-0010);
- **backoff programa `not_before`** (FR-008): `fail` transitorio incrementa
  `attempts`, devuelve el job a `pending` y programa `not_before` con el retraso
  de `jobs/backoff.py` (PR-023), jitter completo en `[0, base]` para el 1er fallo;
- **terminales no reintentan** (FR-008 · contracts §3): `fail(terminal=True)` →
  `failed` definitivo, `unavailable()` → `unavailable` definitivo, agotamiento de
  `max_attempts` → `failed` definitivo; ninguno vuelve a despacharse;
- **lease reset** (ADR-0010): los `running` con `locked_at` vencido vuelven a
  `pending` (crash de worker) y son reclamables;
- **`enqueue` con clave de dedupe por payload** no duplica jobs activos (tasks.md
  PR-026: dedupe por unicidad de payload cuando aplique).

Se skippean si la DB local no es alcanzable o la migración PR-025 no está aplicada
(CI sin Supabase): la comprobación ocurre en recolección vía `pytestmark`, no por
import (patrón del spike). Cada test limpia `jobs` al inicio (constitución §6).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from xtrace_spike.repo import resolve_dsn

from xtrace_crawler.jobs.repo import JobsRepo
from xtrace_crawler.jobs.types import Job, JobStatus, JobType

#: nº de workers concurrentes en el test de claim (cada uno con su conexión).
CONCURRENT_WORKERS = 8


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
    "integration JobsRepo saltada",
)


@pytest.fixture(autouse=True)
def _clean_jobs() -> None:
    """Estado `jobs` limpio por test (misma conexión sync que la comprobación).

    `DELETE` (no `TRUNCATE`): menos bloqueante con locks en una BD local
    compartida (otros worktrees corren sus propios tests de integración).
    """
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("delete from public.jobs")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del repo (sin pytest-asyncio, estilo PR-007/PR-023)."""
    return asyncio.run(coro)


async def _connect() -> psycopg.AsyncConnection[dict[str, Any]]:
    """Conexión auxiliar de los tests para manipular el reloj/estado de la BD."""
    return await psycopg.AsyncConnection.connect(resolve_dsn(), row_factory=psycopg.rows.dict_row)


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


async def _expire_lease(job_id: uuid.UUID) -> None:
    """Vence el lease de un job `running` (simula crash de worker hace 1 h)."""
    async with await _connect() as conn:
        await conn.execute(
            "update public.jobs set locked_at = now() - interval '1 hour' where id = %s",
            (job_id,),
        )
        await conn.commit()


async def _make_eligible(job_id: uuid.UUID) -> None:
    """Adelanta `not_before` al pasado: el job vuelve a ser elegible sin dormir."""
    async with await _connect() as conn:
        await conn.execute(
            "update public.jobs set not_before = now() - interval '1 second' where id = %s",
            (job_id,),
        )
        await conn.commit()


async def _job_count() -> int:
    async with await _connect() as conn:
        cur = await conn.execute("select count(*) as total from public.jobs")
        row = await cur.fetchone()
    assert row is not None and row["total"] is not None
    return int(row["total"])


# --- Claim único entre N conexiones concurrentes (FR-006 · ADR-0010) ---------------


def test_concurrent_claims_are_unique() -> None:
    """N workers concurrentes (N conexiones) nunca reclaman el mismo job (FR-006).

    `FOR UPDATE SKIP LOCKED`: cada `claim_next` toma un job distinto; el conjunto
    de ids reclamados por N workers tiene tamaño N (nadie reclama dos veces el
    mismo job) y el estado resultante es `running` con lease por worker.
    """

    async def _scenario() -> None:
        repo = JobsRepo()
        jobs = await asyncio.gather(
            *[repo.enqueue(JobType.DISCOVER, payload={"n": i}) for i in range(CONCURRENT_WORKERS)]
        )
        assert len({j.id for j in jobs}) == CONCURRENT_WORKERS

        claimed = await asyncio.gather(
            *[JobsRepo().claim_next(f"worker-{i}") for i in range(CONCURRENT_WORKERS)]
        )
        claimed_ids = [j.id for j in claimed if j is not None]
        assert len(claimed_ids) == CONCURRENT_WORKERS
        assert len(set(claimed_ids)) == CONCURRENT_WORKERS  # claim único por job
        for i, job in enumerate(claimed):
            assert job is not None
            assert job.status is JobStatus.RUNNING
            assert job.locked_by == f"worker-{i}"

    _run(_scenario())


# --- Despacho FIFO y respeto de `not_before` (ADR-0010 · data-model.md) ------------


def test_dispatch_orders_by_created_at() -> None:
    """El despacho toma el job pendiente más antiguo (ORDER BY created_at, ADR-0010)."""

    async def _scenario() -> None:
        repo = JobsRepo()
        first = await repo.enqueue(JobType.DISCOVER)
        await asyncio.sleep(0.05)  # created_at distinto (now() de la BD)
        second = await repo.enqueue(JobType.DISCOVER)
        claimed = await repo.claim_next("worker-a")
        assert claimed is not None
        assert claimed.id == first.id
        assert claimed.id != second.id

    _run(_scenario())


def test_future_not_before_is_not_claimed() -> None:
    """Un job programado en el futuro no se despacha hasta su `not_before` (FR-008)."""

    async def _scenario() -> None:
        repo = JobsRepo()
        future = datetime.now(UTC) + timedelta(minutes=5)
        job = await repo.enqueue(JobType.DISCOVER, not_before=future)
        assert job.not_before > datetime.now(UTC)
        assert await repo.claim_next("worker-a") is None
        await _make_eligible(job.id)
        claimed = await repo.claim_next("worker-a")
        assert claimed is not None and claimed.id == job.id

    _run(_scenario())


# --- Transiciones enqueue → claim → complete (FR-006) ------------------------------


def test_enqueue_claim_complete_transitions() -> None:
    """pending → running (lease `locked_by`/`locked_at`) → done; done no se redespacha."""

    async def _scenario() -> None:
        repo = JobsRepo()
        payload = {"cursor": "abc", "limit": 10}
        job = await repo.enqueue(JobType.DISCOVER, payload=payload)
        assert job.status is JobStatus.PENDING
        assert job.job_type is JobType.DISCOVER
        assert job.payload == payload
        assert job.attempts == 0

        claimed = await repo.claim_next("worker-a")
        assert claimed is not None and claimed.id == job.id
        assert claimed.status is JobStatus.RUNNING
        assert claimed.locked_by == "worker-a"
        assert claimed.locked_at is not None

        done = await repo.complete(claimed.id)
        assert done.status is JobStatus.DONE
        assert done.error is None
        assert await repo.claim_next("worker-a") is None

    _run(_scenario())


# --- Backoff programa `not_before` (FR-008 · contracts §3) --------------------------


def test_transient_failure_schedules_backoff() -> None:
    """`fail` transitorio: attempts+1, vuelve a `pending` y `not_before` lo programa.

    Se inyecta `delay_fn` (1 s fijo) para una aserción estable (NFR-003): el
    jitter real de `backoff.py` puede ser ~0 y la latencia de la conexión de
    medición haría `not_before` ya pasado (test flaky). El retraso programado
    por `next_attempt_delay` en sí lo valida `test_backoff.py` (PR-023); aquí
    se valida la integración: attempts → delay → `not_before ≈ now + delay`.
    """

    async def _scenario() -> None:
        repo = JobsRepo(delay_fn=lambda _attempts: 1.0)
        await repo.enqueue(JobType.FETCH_METADATA)
        claimed = await repo.claim_next("worker-a")
        assert claimed is not None

        failed = await repo.fail(claimed.id, "429 too many requests")
        assert failed.status is JobStatus.PENDING  # reintentable
        assert failed.attempts == 1
        assert failed.error == "429 too many requests"
        delay = await _not_before_delay_seconds(failed.id)
        assert 0.5 <= delay <= 1.0 + 0.5  # ≈ now + 1 s (tolerancia de latencia)

    _run(_scenario())


# --- Estados terminales sin reintentos (FR-008 · contracts §3) ---------------------


def test_terminal_failures_are_not_retried() -> None:
    """`fail(terminal=True)`, `unavailable()` y agotar `max_attempts` → definitivos.

    Ninguno de los jobs terminales vuelve a ser despachado por `claim_next`
    (contracts §3: terminales sin reintentos; FR-008: sin reintentos infinitos).
    """

    async def _scenario() -> None:
        repo = JobsRepo()

        # terminal explícito (bloqueo robots/ToS) → failed definitivo.
        await repo.enqueue(JobType.CHECK_AVAILABILITY)
        c1 = await repo.claim_next("worker-a")
        assert c1 is not None
        f1 = await repo.fail(c1.id, "blocked by robots.txt", terminal=True)
        assert f1.status is JobStatus.FAILED
        assert f1.attempts == 1

        # contenido retirado/no disponible (404/removed) → unavailable definitivo.
        await repo.enqueue(JobType.CHECK_AVAILABILITY)
        c2 = await repo.claim_next("worker-a")
        assert c2 is not None
        u = await repo.unavailable(c2.id, "video removed (404)")
        assert u.status is JobStatus.UNAVAILABLE

        # agotamiento de max_attempts → failed definitivo.
        await repo.enqueue(JobType.FETCH_METADATA, max_attempts=2)
        c3 = await repo.claim_next("worker-a")
        assert c3 is not None
        await repo.fail(c3.id, "transient")
        await _make_eligible(c3.id)  # adelanta el backoff para reintentar sin dormir
        c4 = await repo.claim_next("worker-a")
        assert c4 is not None and c4.id == c3.id
        assert c4.attempts == 1
        f2 = await repo.fail(c4.id, "transient again")
        assert f2.attempts == 2
        assert f2.status is JobStatus.FAILED

        # nada de lo anterior es elegible: cola vacía para el despacho.
        assert await repo.claim_next("worker-a") is None

    _run(_scenario())


# --- Lease reset de `running` vencidos (ADR-0010 · data-model.md) ------------------


def test_reset_stale_leases_returns_running_to_pending() -> None:
    """Lease vencido (crash de worker) → `running` vuelve a `pending` y se reclama.

    `reset_stale_leases(timeout)` devuelve el nº de jobs reseteados y limpia el
    lease (`locked_by`/`locked_at`); el job vuelve a ser elegible para otro worker.
    """

    async def _scenario() -> None:
        repo = JobsRepo()
        await repo.enqueue(JobType.EXTRACT_FRAMES)
        claimed = await repo.claim_next("worker-a")
        assert claimed is not None
        assert claimed.status is JobStatus.RUNNING
        assert claimed.locked_by == "worker-a"
        assert claimed.locked_at is not None

        await _expire_lease(claimed.id)  # lease vencido hace 1 h (crash simulado)

        assert await repo.reset_stale_leases(timeout_seconds=300) == 1
        # lease limpio y pendiente de nuevo
        pending = await _get_job(claimed.id)
        assert pending.status is JobStatus.PENDING
        assert pending.locked_by is None
        assert pending.locked_at is None

        again = await repo.claim_next("worker-b")
        assert again is not None and again.id == claimed.id
        assert again.locked_by == "worker-b"

    _run(_scenario())


def test_reset_stale_leases_keeps_fresh_running_jobs() -> None:
    """Un `running` con lease vigente NO se devuelve a `pending` (ADR-0010)."""

    async def _scenario() -> None:
        repo = JobsRepo()
        await repo.enqueue(JobType.EXTRACT_FRAMES)
        claimed = await repo.claim_next("worker-a")
        assert claimed is not None
        assert await repo.reset_stale_leases(timeout_seconds=300) == 0
        still = await _get_job(claimed.id)
        assert still.status is JobStatus.RUNNING
        assert still.locked_by == "worker-a"

    _run(_scenario())


# --- Dedupe por clave de unicidad de payload (tasks.md PR-026) ---------------------


def test_enqueue_dedupe_key_does_not_duplicate_active_jobs() -> None:
    """`dedupe_key` evita duplicar jobs activos con el mismo payload (tasks.md PR-026).

    El dedupe aplica a jobs no terminales (pending/running/failed): re-encolar con
    la misma clave devuelve el job existente; sin clave (o tras un estado terminal)
    se inserta uno nuevo.
    """

    async def _scenario() -> None:
        repo = JobsRepo()
        payload = {"video_external_id": "v-123"}

        a = await repo.enqueue(JobType.FETCH_METADATA, payload=payload, dedupe_key="v-123")
        b = await repo.enqueue(JobType.FETCH_METADATA, payload=payload, dedupe_key="v-123")
        assert b.id == a.id  # no duplicó: devuelve el existente activo
        assert await _job_count() == 1

        # sin clave de dedupe → inserta (el caller decide cuándo aplica el dedupe).
        c = await repo.enqueue(JobType.FETCH_METADATA, payload=payload)
        assert c.id != a.id
        assert await _job_count() == 2

        # un job terminal ya no bloquea un re-encolado con la misma clave.
        claimed = await repo.claim_next("worker-a")
        assert claimed is not None
        await repo.complete(claimed.id)
        d = await repo.enqueue(JobType.FETCH_METADATA, payload=payload, dedupe_key="v-123")
        assert d.id != a.id

    _run(_scenario())


# --- Helpers ------------------------------------------------------------------------


async def _get_job(job_id: uuid.UUID) -> Job:
    """Lee un job directamente de la BD (estado sin pasar por el repo)."""
    async with await _connect() as conn:
        cur = await conn.execute("select * from public.jobs where id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)
