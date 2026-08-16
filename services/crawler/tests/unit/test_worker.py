"""Tests del worker de jobs (PR-027 · FR-006/FR-008/FR-010 · SC-008 · ADR-0010 · contracts §3).

Validan `jobs/worker.py` con **repo y handlers fake** (sin BD, sin red — NFR-003):

- transiciones pending → running → done/failed/unavailable (FR-006/FR-008):
  el worker reclama, ejecuta el handler registrado para el `job_type` y aplica
  la transición según el resultado;
- **aislamiento (SC-008 · FR-010)**: un handler que lanza una excepción no rompe
  el bucle ni impide procesar otros jobs;
- **backoff aplicado (FR-008)**: fallo transitorio → `fail()` (reintento con
  `not_before` programado), fallo terminal (removed/404/robots/ToS) →
  `unavailable()` sin reintentos; job sin handler → `failed` definitivo;
- **lease reset invocable** (ADR-0010, crash de worker): `run_once` resetea los
  leases vencidos antes de procesar; `run_forever` lo invoca periódicamente;
- handlers base **genéricos** de DISCOVER/CHECK_AVAILABILITY con dependencias
  inyectadas (la lógica concreta de discover la cierra PR-030);
- **coordinación worker ↔ `jobs/backoff.classify_error`** con los errores
  tipados del MockAdapter (nota de la revisión PR-021): un error "removed"
  (mensaje/atributo) acaba en `unavailable`.

Trazabilidad (constitución §3): cada test indica el requisito que valida.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec
from xtrace_crawler.adapters.mock import (
    MockAdapter,
    MockAdapterRemovedError,
    MockAdapterTimeoutError,
    MockAdapterTransientError,
    MockFaults,
)
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.jobs.backoff import ErrorClass, classify_error
from xtrace_crawler.jobs.types import Job, JobStatus, JobType
from xtrace_crawler.jobs.worker import (
    DEFAULT_DISCOVER_LIMIT,
    JobWorker,
    check_availability_handler,
    discover_handler,
)


class FakeJobsRepo:
    """Fake del contrato de repo que el worker necesita (`JobsRepoProtocol`).

    Misma semántica que `JobsRepo` (PR-026) pero en memoria: `claim_next` toma
    el pendiente más antiguo elegible (`pending` y `not_before <= now`) y lo
    marca `running` con lease; `fail` transitorio vuelve a `pending` con
    `not_before` futuro; `reset_stale_leases` devuelve a `pending` los
    `running` con lease vencido.
    """

    def __init__(self, jobs: list[Job] | None = None) -> None:
        self.jobs: dict[uuid.UUID, Job] = {job.id: job for job in (jobs or [])}
        self.claims: list[str] = []
        self.completed: list[uuid.UUID] = []
        self.failed: list[tuple[uuid.UUID, str, bool]] = []
        self.unavailables: list[tuple[uuid.UUID, str | None]] = []
        self.reset_calls: list[float] = []

    async def claim_next(self, worker_id: str) -> Job | None:
        self.claims.append(worker_id)
        now = datetime.now(UTC)
        eligible = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status is JobStatus.PENDING and job.not_before <= now
            ),
            key=lambda job: job.created_at,
        )
        if not eligible:
            return None
        job = eligible[0]
        self.jobs[job.id] = job.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "locked_by": worker_id,
                "locked_at": now,
            }
        )
        return self.jobs[job.id]

    async def complete(self, job_id: uuid.UUID) -> Job:
        self.completed.append(job_id)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={"status": JobStatus.DONE, "error": None}
        )
        return self.jobs[job_id]

    async def fail(self, job_id: uuid.UUID, error: str, *, terminal: bool = False) -> Job:
        self.failed.append((job_id, error, terminal))
        job = self.jobs[job_id]
        attempts = job.attempts + 1
        if terminal or attempts >= job.max_attempts:
            status, not_before = JobStatus.FAILED, job.not_before
        else:
            # transitorio: reintentable, con backoff programado en el futuro.
            status, not_before = JobStatus.PENDING, datetime.now(UTC) + timedelta(seconds=10)
        self.jobs[job_id] = job.model_copy(
            update={
                "status": status,
                "error": error,
                "attempts": attempts,
                "not_before": not_before,
            }
        )
        return self.jobs[job_id]

    async def unavailable(self, job_id: uuid.UUID, error: str | None = None) -> Job:
        self.unavailables.append((job_id, error))
        job = self.jobs[job_id]
        self.jobs[job_id] = job.model_copy(
            update={"status": JobStatus.UNAVAILABLE, "error": error or job.error}
        )
        return self.jobs[job_id]

    async def reset_stale_leases(self, timeout_seconds: float) -> int:
        self.reset_calls.append(timeout_seconds)
        cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
        count = 0
        for job_id, job in self.jobs.items():
            if (
                job.status is JobStatus.RUNNING
                and job.locked_at is not None
                and job.locked_at <= cutoff
            ):
                self.jobs[job_id] = job.model_copy(
                    update={"status": JobStatus.PENDING, "locked_by": None, "locked_at": None}
                )
                count += 1
        return count


class _RecordingHandler:
    """Handler fake: registra los jobs recibidos y ejecuta un efecto opcional."""

    def __init__(self, effect: Callable[[Job], Awaitable[None]] | None = None) -> None:
        self.calls: list[Job] = []
        self._effect = effect

    async def __call__(self, job: Job) -> None:
        self.calls.append(job)
        if self._effect is not None:
            await self._effect(job)


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del worker (sin pytest-asyncio, estilo PR-007/PR-023)."""
    return asyncio.run(coro)


def _make_job(
    job_type: JobType,
    *,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
    created_at: datetime | None = None,
) -> Job:
    """Job `pending` elegible (not_before en el pasado) para el repo fake."""
    now = created_at or datetime.now(UTC)
    return Job(
        id=uuid.uuid4(),
        job_type=job_type,
        status=JobStatus.PENDING,
        payload=payload or {},
        attempts=0,
        max_attempts=max_attempts,
        not_before=now - timedelta(seconds=1),
        locked_by=None,
        locked_at=None,
        error=None,
        created_at=now,
        updated_at=now,
    )


async def _noop(_job: Job) -> None:
    """Handler fake que siempre tiene éxito."""


async def _noop_ids(_job: Job, _ids: list[str]) -> None:
    """Callback fake de `on_discovered` (sin efecto)."""


async def _noop_cursor(_job: Job, _cursor: str) -> None:
    """Callback fake de `on_next_cursor` (sin efecto)."""


async def _raise_transient(_job: Job) -> None:
    """Handler fake con fallo transitorio tipado del mock (PR-021)."""
    raise MockAdapterTransientError("timeout inyectado del mock adapter")


async def _raise_terminal(_job: Job) -> None:
    """Handler fake con fallo terminal tipado del mock: contenido retirado (PR-021)."""
    raise MockAdapterRemovedError("mock adapter: video removed (fallo inyectado)")


async def _raise_robots(_job: Job) -> None:
    """Handler fake con bloqueo declarado por robots (terminal, contracts §3)."""
    raise RuntimeError("blocked by robots.txt")


# --- Transiciones pending → running → done/failed/unavailable (FR-006/FR-008) --------


def test_handler_success_transitions_to_done() -> None:
    """El worker reclama (pending → running con lease) y completa (→ done, FR-006)."""

    async def _scenario() -> None:
        job = _make_job(JobType.DISCOVER)
        repo = FakeJobsRepo(jobs=[job])
        handler = _RecordingHandler()
        worker = JobWorker(repo, concurrency=1, worker_id="w-ok")
        worker.register_handler(JobType.DISCOVER, handler)

        assert await worker.run_once() == 1
        claimed = handler.calls[0]
        assert claimed.id == job.id
        assert claimed.status is JobStatus.RUNNING  # lo que vio el handler
        assert claimed.locked_by == "w-ok"
        final = repo.jobs[job.id]
        assert final.status is JobStatus.DONE
        assert final.error is None
        assert repo.failed == [] and repo.unavailables == []

    _run(_scenario())


def test_transient_failure_schedules_backoff() -> None:
    """Fallo transitorio (MockAdapterTransientError, PR-021) → `fail` con backoff (FR-008).

    El job vuelve a `pending` (reintentable), `attempts` se incrementa y
    `not_before` queda en el futuro: el reintento respeta `jobs/backoff.py`
    (PR-023) acotado por `max_attempts`.
    """

    async def _scenario() -> None:
        job = _make_job(JobType.DISCOVER)
        repo = FakeJobsRepo(jobs=[job])
        worker = JobWorker(repo, concurrency=1, worker_id="w-backoff")
        worker.register_handler(JobType.DISCOVER, _RecordingHandler(_raise_transient))

        assert await worker.run_once() == 1
        updated = repo.jobs[job.id]
        assert updated.status is JobStatus.PENDING  # reintentable
        assert updated.attempts == 1
        assert updated.not_before > datetime.now(UTC)  # backoff programado
        assert repo.failed == [
            (job.id, "MockAdapterTransientError: timeout inyectado del mock adapter", False)
        ]
        assert repo.unavailables == []

    _run(_scenario())


def test_terminal_failures_are_unavailable_without_retries() -> None:
    """Fallo terminal (removed tipado del mock; bloqueo robots) → `unavailable` (FR-008).

    Nota revisión PR-021: el error tipado `MockAdapterRemovedError` (mensaje
    "removed") y el mensaje robots/ToS se clasifican terminales
    (`jobs/backoff.classify_error`) y acaban en `unavailable` definitivo, sin
    reintentos (tasks.md PR-027 · contracts §3).
    """

    async def _scenario() -> None:
        removed = _make_job(JobType.CHECK_AVAILABILITY)
        robots = _make_job(JobType.FETCH_METADATA)
        repo = FakeJobsRepo(jobs=[removed, robots])
        worker = JobWorker(repo, concurrency=2, worker_id="w-terminal")
        worker.register_handler(JobType.CHECK_AVAILABILITY, _RecordingHandler(_raise_terminal))
        worker.register_handler(JobType.FETCH_METADATA, _RecordingHandler(_raise_robots))

        assert await worker.run_once() == 2
        assert repo.jobs[removed.id].status is JobStatus.UNAVAILABLE
        assert repo.jobs[robots.id].status is JobStatus.UNAVAILABLE
        assert len(repo.unavailables) == 2
        assert repo.failed == []  # terminales NUNCA pasan por `fail`
        assert sorted(e for _, e in repo.unavailables if e is not None) == [
            "MockAdapterRemovedError: mock adapter: video removed (fallo inyectado)",
            "RuntimeError: blocked by robots.txt",
        ]

    _run(_scenario())


def test_job_without_handler_fails_terminal() -> None:
    """Un job sin handler registrado es misconfiguración: `failed` definitivo (FR-008).

    Quemar reintentos no tiene sentido (el handler no aparecerá solo): el job
    alcanza un estado terminal en el primer intento — "estados terminales
    garantizados" (tasks.md PR-027, Done).
    """

    async def _scenario() -> None:
        job = _make_job(JobType.REINDEX)
        repo = FakeJobsRepo(jobs=[job])
        worker = JobWorker(repo, concurrency=1, worker_id="w-nohandler")

        assert await worker.run_once() == 1
        updated = repo.jobs[job.id]
        assert updated.status is JobStatus.FAILED
        assert repo.failed == [
            (job.id, f"no hay handler registrado para job_type={JobType.REINDEX.value}", True)
        ]
        assert repo.unavailables == []

    _run(_scenario())


# --- Aislamiento de fallos por fuente (SC-008 · FR-010) ------------------------------


def test_handler_failure_does_not_break_worker_or_other_jobs() -> None:
    """SC-008: un handler que falla no tumba el worker ni impide otros jobs.

    Dos jobs de la fuente "caída" (handler que lanza removed) y uno sano: el
    worker procesa los tres, los caídos acaban `unavailable` y el sano `done`;
    `run_once` no propaga la excepción del handler.
    """

    async def _scenario() -> None:
        failing = [_make_job(JobType.DISCOVER) for _ in range(2)]
        healthy = _make_job(JobType.FETCH_METADATA)
        repo = FakeJobsRepo(jobs=[*failing, healthy])
        bad = _RecordingHandler(_raise_terminal)
        good = _RecordingHandler(_noop)
        worker = JobWorker(repo, concurrency=2, worker_id="w-isolation")
        worker.register_handler(JobType.DISCOVER, bad)
        worker.register_handler(JobType.FETCH_METADATA, good)

        assert await worker.run_once() == 3  # no lanza: aislamiento
        assert {job.id for job in bad.calls} == {job.id for job in failing}
        assert [job.id for job in good.calls] == [healthy.id]
        for job in failing:
            assert repo.jobs[job.id].status is JobStatus.UNAVAILABLE
        assert repo.jobs[healthy.id].status is JobStatus.DONE
        assert len(repo.unavailables) == 2

    _run(_scenario())


def test_unexpected_exception_is_contained_and_retried() -> None:
    """Un error desconocido del handler se contiene y se trata como transitorio.

    Default fail-safe de `jobs/backoff.classify_error` (PR-023): lo no
    reconocido como terminal se reintenta con backoff; el worker sigue vivo.
    """

    async def _scenario() -> None:
        async def _boom(_job: Job) -> None:
            raise RuntimeError("html structure changed")

        job = _make_job(JobType.DISCOVER)
        repo = FakeJobsRepo(jobs=[job])
        worker = JobWorker(repo, concurrency=1, worker_id="w-unknown")
        worker.register_handler(JobType.DISCOVER, _RecordingHandler(_boom))

        assert await worker.run_once() == 1  # sin propagación
        updated = repo.jobs[job.id]
        assert updated.status is JobStatus.PENDING
        assert updated.error == "RuntimeError: html structure changed"

    _run(_scenario())


# --- Lease reset (ADR-0010 · crash de worker) ----------------------------------------


def test_run_once_resets_stale_leases_before_processing() -> None:
    """`run_once` resetea los `running` con lease vencido y procesa el job (crash).

    El job quedó `running` con lease vencido hace 1 h (crash simulado): el
    lease reset lo devuelve a `pending` y en la misma pasada se reclama y se
    completa.
    """

    async def _scenario() -> None:
        stale = _make_job(JobType.DISCOVER).model_copy(
            update={
                "status": JobStatus.RUNNING,
                "locked_by": "worker-crashed",
                "locked_at": datetime.now(UTC) - timedelta(hours=1),
            }
        )
        fresh = _make_job(JobType.DISCOVER)
        repo = FakeJobsRepo(jobs=[stale, fresh])
        handler = _RecordingHandler()
        worker = JobWorker(repo, concurrency=1, worker_id="w-lease", lease_timeout_seconds=300)
        worker.register_handler(JobType.DISCOVER, handler)

        assert await worker.run_once() == 2
        assert repo.reset_calls == [300.0]  # lease reset invocado con el timeout
        claimed_ids = {job.id for job in handler.calls}
        assert stale.id in claimed_ids and fresh.id in claimed_ids
        assert repo.jobs[stale.id].status is JobStatus.DONE
        assert repo.jobs[fresh.id].status is JobStatus.DONE

    _run(_scenario())


def test_run_once_keeps_fresh_running_jobs() -> None:
    """Un `running` con lease vigente NO se resetea ni se procesa (ADR-0010)."""

    async def _scenario() -> None:
        active = _make_job(JobType.DISCOVER).model_copy(
            update={
                "status": JobStatus.RUNNING,
                "locked_by": "otro-worker",
                "locked_at": datetime.now(UTC),
            }
        )
        repo = FakeJobsRepo(jobs=[active])
        handler = _RecordingHandler()
        worker = JobWorker(repo, concurrency=1, worker_id="w-lease")
        worker.register_handler(JobType.DISCOVER, handler)

        assert await worker.run_once() == 0
        assert handler.calls == []
        assert repo.jobs[active.id].status is JobStatus.RUNNING

    _run(_scenario())


def test_run_forever_invokes_lease_reset_periodically() -> None:
    """`run_forever` ejecuta el lease reset periódicamente hasta `stop` (ADR-0010)."""

    async def _scenario() -> None:
        repo = FakeJobsRepo()
        worker = JobWorker(
            repo,
            concurrency=1,
            worker_id="w-forever",
            lease_reset_interval_seconds=0.02,
            poll_interval_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop))
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert repo.reset_calls, "el lease reset periódico debe invocarse"

    _run(_scenario())


def test_run_forever_stops_cleanly_on_event() -> None:
    """`run_forever` termina limpiamente cuando se marca `stop` (CLI Ctrl+C/CI)."""

    async def _scenario() -> None:
        repo = FakeJobsRepo()
        worker = JobWorker(repo, concurrency=2, poll_interval_seconds=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert repo.completed == []  # cola vacía: nada procesado, salida limpia

    _run(_scenario())


# --- Validación de configuración -----------------------------------------------------


def test_invalid_concurrency_raises() -> None:
    """La concurrencia debe ser >= 1 (bucle con N tasks)."""
    with pytest.raises(ValueError, match="concurrency"):
        JobWorker(FakeJobsRepo(), concurrency=0)


def test_register_handler_duplicate_raises() -> None:
    """Registrar dos handlers para el mismo `job_type` es error de uso."""
    worker = JobWorker(FakeJobsRepo())
    worker.register_handler(JobType.DISCOVER, _RecordingHandler())
    with pytest.raises(ValueError, match="ya hay un handler"):
        worker.register_handler(JobType.DISCOVER, _RecordingHandler())


# --- Coordinación worker ↔ classify_error (nota revisión PR-021) ---------------------


def test_classify_error_coordinates_with_mock_adapter_typed_errors() -> None:
    """Errores tipados del MockAdapter (PR-021): removed → terminal; transient/timeout no.

    La nota de la revisión PR-021 exige que "removed" (mensaje/atributo) acabe
    en `unavailable`: `classify_error` inspecciona el mensaje de la excepción,
    de modo que el worker puede decidir sin conocer el tipo concreto del
    adapter (fail-safe: lo desconocido es transitorio).
    """
    assert (
        classify_error(MockAdapterRemovedError("mock adapter: video removed"))
        is ErrorClass.TERMINAL
    )
    assert (
        classify_error(MockAdapterTransientError("fallo transitorio inyectado"))
        is ErrorClass.TRANSIENT
    )
    assert classify_error(MockAdapterTimeoutError("timeout inyectado")) is ErrorClass.TRANSIENT
    assert classify_error(RuntimeError("HTTP 404 page not found")) is ErrorClass.TERMINAL


def test_classify_error_honors_terminal_attribute_and_http_response() -> None:
    """Convención del worker: atributo `terminal=True` y `.response.status_code` cuentan.

    Los errores tipados de los adapters pueden declarar terminalidad por
    atributo (`terminal=True`) o envolver una respuesta HTTP (404/410 →
    terminal; 429 → transitorio, spec edge cases).
    """

    class _TerminalByAttribute(Exception):
        terminal = True

    class _FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _HttpError(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"HTTP {status}")
            self.response = _FakeResponse(status)

    assert classify_error(_TerminalByAttribute("cualquier mensaje")) is ErrorClass.TERMINAL
    assert classify_error(_HttpError(404)) is ErrorClass.TERMINAL
    assert classify_error(_HttpError(410)) is ErrorClass.TERMINAL
    assert classify_error(_HttpError(429)) is ErrorClass.TRANSIENT  # backoff (FR-008)


# --- Handlers base genéricos DISCOVER/CHECK_AVAILABILITY (PR-027; PR-030 cierra) -----


def test_discover_handler_paginates_and_delegates() -> None:
    """El handler base de DISCOVER pagina el adapter y delega ids + cursor.

    Usa el MockAdapter real (FR-003, sin red): el handler es genérico — la
    lógica concreta (upsert de vídeos, encolar FETCH_METADATA) llega en PR-030
    vía los callbacks inyectados.
    """

    async def _scenario() -> None:
        adapter = MockAdapter(seed=1, catalog_size=5)
        seen_ids: list[list[str]] = []
        seen_cursors: list[str] = []

        async def on_discovered(_job: Job, ids: list[str]) -> None:
            seen_ids.append(ids)

        async def on_next_cursor(_job: Job, cursor: str) -> None:
            seen_cursors.append(cursor)

        job = _make_job(JobType.DISCOVER, payload={"cursor": None, "limit": 3})
        handler = discover_handler(
            adapter_for=lambda _j: adapter,
            on_discovered=on_discovered,
            on_next_cursor=on_next_cursor,
        )
        await handler(job)
        assert seen_ids == [["mock-vid-0000", "mock-vid-0001", "mock-vid-0002"]]
        assert seen_cursors == ["3"]
        assert adapter.catalog_ids()[:3] == seen_ids[0]  # ids del catálogo real

    _run(_scenario())


def test_discover_handler_uses_default_limit() -> None:
    """Sin `limit` en el payload se usa `DEFAULT_DISCOVER_LIMIT` (página por defecto)."""

    async def _scenario() -> None:
        adapter = MockAdapter(seed=1, catalog_size=DEFAULT_DISCOVER_LIMIT + 5)
        requested: list[int] = []

        async def on_discovered(_job: Job, ids: list[str]) -> None:
            requested.append(len(ids))

        job = _make_job(JobType.DISCOVER)
        handler = discover_handler(
            adapter_for=lambda _j: adapter,
            on_discovered=on_discovered,
            on_next_cursor=_noop_cursor,
        )
        await handler(job)
        assert requested == [DEFAULT_DISCOVER_LIMIT]

    _run(_scenario())


def test_discover_handler_rejects_invalid_payload() -> None:
    """`limit` inválido (no entero, < 1) es error del handler → el worker lo clasifica."""
    adapter = MockAdapter(seed=1, catalog_size=5)
    handler = discover_handler(
        adapter_for=lambda _j: adapter,
        on_discovered=_noop_ids,
        on_next_cursor=_noop_cursor,
    )

    async def _scenario() -> None:
        with pytest.raises(ValueError, match="limit"):
            await handler(_make_job(JobType.DISCOVER, payload={"limit": 0}))
        with pytest.raises(ValueError, match="limit"):
            await handler(_make_job(JobType.DISCOVER, payload={"limit": "abc"}))
        with pytest.raises(ValueError, match="cursor"):
            await handler(_make_job(JobType.DISCOVER, payload={"cursor": 123}))

    _run(_scenario())


def test_check_availability_handler_delegates_result() -> None:
    """El handler base de CHECK_AVAILABILITY delega el resultado (available/unavailable/removed).

    El estado del VÍDEO lo aplica `on_result` (PR-030, FR-012 · spec edge
    cases); el handler solo orquesta la consulta contra el adapter.
    """

    async def _scenario() -> None:
        adapter = MockAdapter(
            seed=7,
            catalog_size=10,
            faults=MockFaults(
                availability={
                    "mock-vid-0000": VideoAvailability.REMOVED,
                    "mock-vid-0001": VideoAvailability.UNAVAILABLE,
                }
            ),
        )
        results: list[tuple[str, VideoAvailability]] = []

        async def on_result(_job: Job, availability: VideoAvailability) -> None:
            results.append((_job.payload["video_external_id"], availability))

        for external_id in ("mock-vid-0000", "mock-vid-0001", "mock-vid-0002"):
            video = adapter.get_catalog_video(external_id)
            assert video is not None
            job = _make_job(JobType.CHECK_AVAILABILITY, payload={"video_external_id": external_id})

            async def video_for(_job: Job) -> VideoSource:
                return video  # noqa: B023 — captura por iteración; se invoca en esta misma iteración

            handler = check_availability_handler(
                adapter_for=lambda _j: adapter,
                video_for=video_for,
                on_result=on_result,
            )
            await handler(job)

        assert results == [
            ("mock-vid-0000", VideoAvailability.REMOVED),
            ("mock-vid-0001", VideoAvailability.UNAVAILABLE),
            ("mock-vid-0002", VideoAvailability.AVAILABLE),
        ]

    _run(_scenario())


def test_check_availability_handler_propagates_adapter_errors() -> None:
    """Los fallos del adapter NO se capturan en el handler: los clasifica el worker.

    Así el flujo de errores es único: transitorio → retry con backoff;
    terminal → `unavailable` (aislamiento SC-008, FR-008). El MockAdapter no
    lanza en `check_availability` (solo estados), así que se usa un fake con
    error tipado "removed".
    """

    class _RaisingCheckAdapter:
        """Adapter fake cuyo `check_availability` siempre lanza removed (PR-021)."""

        manifest = AdapterManifest(
            source="fake",
            access_method="json",
            assets_accessed=["storyboard"],
            robots_reviewed=True,
            terms_reviewed=True,
            review_date="2026-08-15",
            rate_limit=RateLimitSpec(min_interval_ms=100, max_rps=1.0),
        )

        async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
            return DiscoverPage(external_ids=[], next_cursor=None)

        async def get_video(self, external_id: str) -> VideoSource | None:
            return None

        async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
            return []

        async def check_availability(self, video: VideoSource) -> VideoAvailability:
            raise MockAdapterRemovedError("mock adapter: video removed (404)")

    async def _scenario() -> None:
        adapter = _RaisingCheckAdapter()
        video = VideoSource(source="fake", external_id="v-1", page_url="http://mock.local/v-1")

        async def video_for(_job: Job) -> VideoSource:
            return video

        async def on_result(_job: Job, _availability: VideoAvailability) -> None:
            return None

        job = _make_job(JobType.CHECK_AVAILABILITY)
        handler = check_availability_handler(
            adapter_for=lambda _j: adapter,
            video_for=video_for,
            on_result=on_result,
        )
        with pytest.raises(MockAdapterRemovedError):
            await handler(job)  # se propaga al worker, que la clasifica terminal

    _run(_scenario())
