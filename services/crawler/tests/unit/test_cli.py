"""Tests del CLI operativo (PR-032 · FR-006/007/013/014 · SEC-002 · NFR-004 · contracts §5).

Validan `xtrace_crawler/cli.py` (Typer) con `CliRunner` y **dependencias
inyectadas** (`ctx.obj` = `CliContext` con fakes): sin red y sin BD real
(NFR-003), determinista.

Cobertura por comando:
- `sources`: lista fuentes de la BD (DATA-001) con manifest + `enabled`;
  `--json` estable.
- `backfill`: valida la fuente (registry + BD + gate SEC-002) y encola el job
  DISCOVER con el payload del contrato de PR-030 (FR-006/FR-007); errores de
  usuario (fuente desconocida / no habilitada / límite inválido) con mensaje
  claro y exit code != 0.
- `run-worker`: `--once` procesa una pasada (FR-006): con worker inyectado
  (jobs fake) y sin inyección (cablea pipeline PR-030 + registry, todo fake).
- `stats`: estadísticas coherentes (FR-014) y JSON estable.
- `check-availability`: encola CHECK_AVAILABILITY por vídeo de la fuente
  (FR-013).
- `config.py`: defaults de worker/lease/límites y overrides por env (PR-032).

Trazabilidad (constitución §3): cada test indica el requisito que valida.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from xtrace_crawler.adapters.base import AdapterManifest
from xtrace_crawler.adapters.mock import MockAdapter
from xtrace_crawler.adapters.registry import AdapterRegistry
from xtrace_crawler.adapters.xvideos import XvideosAdapter
from xtrace_crawler.cli import CliContext, app
from xtrace_crawler.config import Settings
from xtrace_crawler.jobs.types import Job, JobStatus, JobType
from xtrace_crawler.jobs.worker import JobWorker
from xtrace_crawler.repo import (
    DEFAULT_RECENT_ERRORS_LIMIT,
    CrawlerStats,
    JobErrorRecord,
    SourceRecord,
    VideoRecord,
    VideoStatus,
)

runner = CliRunner()

#: Ids UUID estables para los registros fake (evitan parse_uuid).
SOURCE_ID = "00000000-0000-0000-0000-000000000001"
VIDEO_ID = "11111111-1111-1111-1111-111111111111"


def _now() -> datetime:
    return datetime.now(UTC)


def _mock_source_record(*, enabled: bool = False) -> SourceRecord:
    """Fila `sources` del mock (manifest del MockAdapter, FR-003)."""
    now = _now()
    return SourceRecord(
        id=SOURCE_ID,
        name="mock",
        adapter="mock",
        manifest=MockAdapter().manifest,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def _xvideos_source_record(*, enabled: bool = False) -> SourceRecord:
    """Fila `sources` del adapter real xvideos (manifest sin revisión legal, SEC-002)."""
    now = _now()
    return SourceRecord(
        id="00000000-0000-0000-0000-000000000002",
        name="xvideos",
        adapter="xvideos",
        manifest=XvideosAdapter().manifest,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


class FakeRepo:
    """Fake de `CliRepoProtocol` (+ contrato del pipeline): todo en memoria, sin BD.

    Fuentes/vídeos/stats configurables; registra upserts y transiciones para
    poder asertar la actividad del pipeline (PR-030) en los tests del CLI.
    """

    def __init__(
        self,
        sources: list[SourceRecord] | None = None,
        videos: list[VideoRecord] | None = None,
        stats: CrawlerStats | None = None,
    ) -> None:
        self.sources: dict[str, SourceRecord] = {record.name: record for record in (sources or [])}
        self.videos: dict[str, VideoRecord] = {
            record.external_id: record for record in (videos or [])
        }
        self.stats_result = stats
        self.upserted_sources: list[SourceRecord] = []
        self.upserted_videos: list[VideoRecord] = []
        self.status_changes: list[tuple[str, str]] = []
        self.excluded: list[str] = []

    async def get_source(self, name: str) -> SourceRecord | None:
        return self.sources.get(name)

    async def list_sources(self) -> list[SourceRecord]:
        return [self.sources[name] for name in sorted(self.sources)]

    async def upsert_source(
        self,
        *,
        name: str,
        adapter: str,
        manifest: AdapterManifest,
        enabled: bool = False,
    ) -> SourceRecord:
        record = SourceRecord(
            id=SOURCE_ID,
            name=name,
            adapter=adapter,
            manifest=manifest,
            enabled=enabled,
            created_at=_now(),
            updated_at=_now(),
        )
        self.sources[name] = record
        self.upserted_sources.append(record)
        return record

    async def upsert_web_video(self, source_id: str, video: Any) -> VideoRecord:
        record = VideoRecord(
            id=str(uuid.uuid4()),
            source_id=source_id,
            external_id=video.external_id,
            local_ref=f"web:{source_id}:{video.external_id}",
            status="discovered",
            excluded=False,
            error=None,
            frame_count=0,
            title=video.title,
            page_url=video.page_url,
            duration_ms=video.duration_ms,
            thumbnail_url=video.thumbnail_url,
            preview_url=video.preview_url,
            storyboard_urls=video.storyboard_urls,
            tags=video.tags,
            published_at=video.published_at,
            created_at=_now(),
            updated_at=_now(),
        )
        self.videos[record.external_id] = record
        self.upserted_videos.append(record)
        return record

    async def get_web_video(self, source_name: str, external_id: str) -> VideoRecord | None:
        return self.videos.get(external_id)

    async def set_video_status(self, video_id: str, status: VideoStatus) -> bool:
        for external_id, record in self.videos.items():
            if record.id == video_id:
                self.videos[external_id] = record.model_copy(update={"status": status})
                self.status_changes.append((video_id, status))
                return True
        return False

    async def exclude(self, video_id: str, *, excluded: bool = True) -> bool:
        for external_id, record in self.videos.items():
            if record.id == video_id:
                self.videos[external_id] = record.model_copy(update={"excluded": excluded})
                self.excluded.append(video_id)
                return True
        return False

    async def stats(
        self, *, recent_errors_limit: int = DEFAULT_RECENT_ERRORS_LIMIT
    ) -> CrawlerStats:
        if self.stats_result is None:
            return CrawlerStats(
                jobs_by_status={}, jobs_by_source={}, videos_by_status={}, recent_errors=[]
            )
        return self.stats_result


class FakeJobs:
    """Fake de `CliJobsProtocol`: cola de jobs en memoria (enqueue + ciclo de vida).

    Misma semántica que `JobsRepo` (PR-026) pero sin BD: `enqueue` deduplica
    por `dedupe_key` contra jobs activos; `claim_next` toma el pendiente más
    antiguo elegible y lo marca `running`; `fail` transitorio programa
    `not_before` futuro (backoff fake).
    """

    def __init__(self, jobs: list[Job] | None = None) -> None:
        self.jobs: dict[uuid.UUID, Job] = {job.id: job for job in (jobs or [])}
        self.enqueued: list[Job] = []
        self.completed: list[uuid.UUID] = []
        self.failed: list[tuple[uuid.UUID, str]] = []
        self.unavailables: list[tuple[uuid.UUID, str | None]] = []

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
        data = dict(payload or {})
        if dedupe_key is not None and "dedupe_key" not in data:
            data["dedupe_key"] = dedupe_key
        if dedupe_key is not None:
            for job in self.jobs.values():
                if (
                    job.job_type is job_type
                    and job.status in (JobStatus.PENDING, JobStatus.RUNNING)
                    and job.payload.get("dedupe_key") == dedupe_key
                ):
                    return job
        now = _now()
        job = Job(
            id=uuid.uuid4(),
            job_type=job_type,
            status=JobStatus.PENDING,
            source_id=source_id,
            video_id=video_id,
            payload=data,
            max_attempts=max_attempts,
            not_before=not_before or now,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.id] = job
        self.enqueued.append(job)
        return job

    async def claim_next(self, worker_id: str) -> Job | None:
        now = _now()
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
            update={"status": JobStatus.RUNNING, "locked_by": worker_id, "locked_at": now}
        )
        return self.jobs[job.id]

    async def complete(self, job_id: uuid.UUID) -> Job:
        self.completed.append(job_id)
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={"status": JobStatus.DONE, "error": None}
        )
        return self.jobs[job_id]

    async def fail(self, job_id: uuid.UUID, error: str, *, terminal: bool = False) -> Job:
        self.failed.append((job_id, error))
        job = self.jobs[job_id]
        attempts = job.attempts + 1
        if terminal or attempts >= job.max_attempts:
            status, not_before = JobStatus.FAILED, job.not_before
        else:
            status, not_before = JobStatus.PENDING, _now() + timedelta(seconds=10)
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
        return 0


class FakeVectorStore:
    """Fake del contrato `VectorStore` del spike (ADR-0007): in-memory, sin BD.

    PR-034: con el mock sirviendo sus assets in-process (`fetch_asset_bytes`,
    sin red), el pipeline SÍ produce frames e indexa en la pasada del CLI;
    este fake mantiene NFR-003 (sin BD real), igual que FakeRepo/FakeJobs, y
    deja los frames indexados visibles para poder asertarlos.
    """

    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def upsert_frames(self, frames: Sequence[Any]) -> int:
        self.frames.extend(frames)
        return len(frames)

    async def ann_search(
        self, embedding: Sequence[float], k: int, exclude_videos: bool = True
    ) -> list[Any]:
        return []

    async def delete_video(self, video_id: str) -> None:
        self.frames = [frame for frame in self.frames if frame["video_id"] != video_id]

    async def stats(self) -> dict[str, int]:
        return {
            "videos": len({frame["video_id"] for frame in self.frames}),
            "frames": len(self.frames),
            "vectors": len(self.frames),
        }


def _make_job(
    job_type: JobType = JobType.DISCOVER,
    *,
    payload: dict[str, Any] | None = None,
    status: JobStatus = JobStatus.PENDING,
) -> Job:
    now = _now()
    return Job(
        id=uuid.uuid4(),
        job_type=job_type,
        status=status,
        payload=payload or {},
        max_attempts=3,
        not_before=now,
        created_at=now,
        updated_at=now,
    )


def _registry_with_mock(*, catalog_size: int = 5) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(MockAdapter(catalog_size=catalog_size), real=False)
    return registry


def _registry_with_xvideos() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(XvideosAdapter(), real=True)
    return registry


async def _no_videos(source: str, limit: int) -> list[str]:
    return []


def _base_context(
    *,
    repo: FakeRepo | None = None,
    jobs: FakeJobs | None = None,
    registry: AdapterRegistry | None = None,
    settings: Settings | None = None,
    list_source_videos: Callable[[str, int], Awaitable[list[str]]] | None = None,
    worker: JobWorker | None = None,
    store: FakeVectorStore | None = None,
) -> CliContext:
    """Contexto CLI con fakes (NFR-003): sin BD y sin red."""
    return CliContext(
        settings=settings if settings is not None else Settings(),
        registry=registry if registry is not None else _registry_with_mock(),
        repo=repo if repo is not None else FakeRepo(),
        jobs=jobs if jobs is not None else FakeJobs(),
        list_source_videos=list_source_videos or _no_videos,
        worker=worker,
        store=store,
    )


def _invoke(args: list[str], context: CliContext) -> Any:
    """Invoca el CLI con el contexto inyectado (`ctx.obj`) y devuelve el Result."""
    return runner.invoke(app, args, obj=context)


def _stdout_json(result: Any) -> Any:
    """Parse del stdout como JSON: la salida de datos SIEMPRE es JSON estable."""
    assert result.exit_code == 0, f"exit={result.exit_code} stderr={result.stderr!r}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# sources (DATA-001 · SEC-002)
# ---------------------------------------------------------------------------


def test_sources_human_lists_sources_with_manifest_and_enabled() -> None:
    """`sources` (sin --json) lista nombre, adapter, enabled y compliance del manifest."""
    repo = FakeRepo(sources=[_xvideos_source_record(), _mock_source_record(enabled=True)])
    result = _invoke(["sources"], _base_context(repo=repo))
    assert result.exit_code == 0
    assert "mock" in result.stdout and "enabled=yes" in result.stdout
    assert "xvideos" in result.stdout and "enabled=no" in result.stdout
    assert "robots_reviewed=no" in result.stdout  # xvideos sin revisión legal (SEC-002)


def test_sources_json_is_stable() -> None:
    """`sources --json` emite JSON estable y ordenado (contratos §5)."""
    repo = FakeRepo(sources=[_xvideos_source_record(), _mock_source_record(enabled=True)])
    result = _invoke(["sources", "--json"], _base_context(repo=repo))
    data = _stdout_json(result)
    assert [entry["name"] for entry in data] == ["mock", "xvideos"]
    assert {"name", "adapter", "enabled", "manifest"} == set(data[0])
    assert data[0]["manifest"]["robots_reviewed"] is True
    assert data[1]["manifest"]["review_date"] is None
    again = _invoke(["sources", "--json"], _base_context(repo=repo))
    assert again.stdout == result.stdout  # estable entre invocaciones


def test_sources_json_empty_without_registered_sources() -> None:
    """`sources --json` con BD vacía → `[]` (JSON estable)."""
    result = _invoke(["sources", "--json"], _base_context(repo=FakeRepo()))
    assert _stdout_json(result) == []


# ---------------------------------------------------------------------------
# backfill (FR-006 · FR-007 · SEC-002 · contracts §5)
# ---------------------------------------------------------------------------


def test_backfill_enqueues_discover_with_contract_payload() -> None:
    """`backfill --source mock` encola DISCOVER con el payload del contrato PR-030.

    El mock está exento del gate (FR-003): no exige `enabled=true` en BD.
    """
    repo = FakeRepo(sources=[_mock_source_record(enabled=False)])
    jobs = FakeJobs()
    result = _invoke(
        ["backfill", "--source", "mock", "--limit", "5"], _base_context(repo=repo, jobs=jobs)
    )
    assert _stdout_json(result) == {
        "job_id": str(jobs.enqueued[0].id),
        "source": "mock",
        "mode": "backfill",
    }
    assert len(jobs.enqueued) == 1
    job = jobs.enqueued[0]
    assert job.job_type is JobType.DISCOVER
    assert job.payload == {
        "source": "mock",
        "cursor": None,
        "limit": 5,
        "mode": "backfill",
        "dedupe_key": "discover:mock:None:backfill",
    }
    assert job.source_id is not None and str(job.source_id) == SOURCE_ID


def test_backfill_incremental_mode_and_default_limit_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--incremental` fija `mode=incremental` (FR-007) y el límite por defecto viene de config."""
    monkeypatch.setenv("XTRACE_CRAWLER_BACKFILL_DEFAULT_LIMIT", "7")
    repo = FakeRepo(sources=[_mock_source_record()])
    jobs = FakeJobs()
    result = _invoke(
        ["backfill", "--source", "mock", "--incremental"],
        _base_context(repo=repo, jobs=jobs, settings=Settings()),
    )
    assert _stdout_json(result) == {
        "job_id": str(jobs.enqueued[0].id),
        "source": "mock",
        "mode": "incremental",
    }
    assert jobs.enqueued[0].payload["mode"] == "incremental"
    assert jobs.enqueued[0].payload["limit"] == 7


def test_backfill_unknown_source_fails_clearly() -> None:
    """Fuente no registrada en el registry → error claro y exit code != 0."""
    registry = AdapterRegistry()  # vacío: "ghost" no existe
    result = _invoke(["backfill", "--source", "ghost"], _base_context(registry=registry))
    assert result.exit_code != 0
    assert "fuente desconocida" in result.stderr
    assert result.stdout == ""


def test_backfill_source_missing_in_db_fails_clearly() -> None:
    """Adapter registrado pero sin fila en `sources` → error claro y exit != 0."""
    registry = _registry_with_mock()
    repo = FakeRepo()  # BD vacía
    result = _invoke(["backfill", "--source", "mock"], _base_context(repo=repo, registry=registry))
    assert result.exit_code != 0
    assert "no registrada en la BD" in result.stderr
    assert result.stdout == ""


def test_backfill_disabled_source_gate_sec_002() -> None:
    """Fuente real sin habilitar (SEC-002) → error con las razones del gate y exit != 0."""
    repo = FakeRepo(sources=[_xvideos_source_record(enabled=False)])
    result = _invoke(
        ["backfill", "--source", "xvideos"],
        _base_context(repo=repo, registry=_registry_with_xvideos()),
    )
    assert result.exit_code != 0
    assert "no habilitada" in result.stderr
    assert "SEC-002" in result.stderr
    assert "robots_reviewed=false" in result.stderr
    assert result.stdout == ""


def test_backfill_invalid_limit_is_usage_error() -> None:
    """`--limit 0` y `--limit abc` son errores de uso (exit code 2, typer)."""
    repo = FakeRepo(sources=[_mock_source_record()])
    context = _base_context(repo=repo)
    assert _invoke(["backfill", "--source", "mock", "--limit", "0"], context).exit_code == 2
    assert _invoke(["backfill", "--source", "mock", "--limit", "abc"], context).exit_code == 2


# ---------------------------------------------------------------------------
# run-worker (FR-006 · contracts §5)
# ---------------------------------------------------------------------------


def test_run_worker_once_dispatches_fake_jobs() -> None:
    """`run-worker --once` con worker inyectado procesa una pasada y emite JSON.

    El worker real (PR-027) reclama los jobs del repo fake y ejecuta el handler
    registrado: la pasada despacha exactamente los jobs elegibles (FR-006).
    """
    job = _make_job(JobType.DISCOVER, payload={"source": "mock"})
    jobs = FakeJobs(jobs=[job])
    dispatched: list[uuid.UUID] = []

    async def handler(claimed: Job) -> None:
        dispatched.append(claimed.id)

    worker = JobWorker(jobs, concurrency=1)
    worker.register_handler(JobType.DISCOVER, handler)
    result = _invoke(["run-worker", "--once"], _base_context(jobs=jobs, worker=worker))
    assert _stdout_json(result) == {"processed": 1}
    assert dispatched == [job.id]
    assert jobs.jobs[job.id].status is JobStatus.DONE


def test_run_worker_once_no_jobs_reports_zero() -> None:
    """`run-worker --once` con cola vacía → `{"processed": 0}` (JSON estable)."""
    worker = JobWorker(FakeJobs(), concurrency=1)
    result = _invoke(["run-worker", "--once"], _base_context(worker=worker))
    assert _stdout_json(result) == {"processed": 0}


def test_run_worker_once_builds_pipeline_and_dispatches_discover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin worker inyectado, `run-worker --once` cablea pipeline (PR-030) + registry.

    Un DISCOVER inicial (mock, catálogo de 5) produce 5 vídeos `discovered`, 5
    jobs FETCH_METADATA y 5 INDEX_VIDEO. Con el fix PR-034 el mock sirve sus
    assets **in-process** (`adapter.fetch_asset_bytes`, sin red): el pipeline
    indexa los 5 vídeos (status `indexed`) en la misma pasada — 11 jobs `done`,
    0 fallos transitorios. Total procesado = 1 + 5 + 5 = 11 (determinista con
    fakes; el `VectorStore` es el fake in-memory, NFR-003: sin BD real).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")  # sin esperas reales
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    jobs = FakeJobs(
        jobs=[
            _make_job(
                JobType.DISCOVER,
                payload={"source": "mock", "cursor": None, "limit": 5, "mode": "backfill"},
            )
        ]
    )
    discover_job_id = next(iter(jobs.jobs))
    registry = _registry_with_mock(catalog_size=5)
    store = FakeVectorStore()
    result = _invoke(
        ["run-worker", "--once"],
        _base_context(repo=repo, jobs=jobs, registry=registry, settings=Settings(), store=store),
    )
    assert _stdout_json(result) == {"processed": 11}
    assert len(repo.videos) == 5  # vídeos únicos (DISCOVER + FETCH upsertan la misma fila)
    # PR-034: los assets del mock se sirven in-process (sin red) → el pipeline
    # indexa los 5 vídeos en la pasada (antes del fix: 0 frames → transitorio
    # con backoff y vídeos sin indexar).
    assert all(record.status == "indexed" for record in repo.videos.values())
    assert store.frames  # los frames indexados llegan al VectorStore (fake, NFR-003)
    assert [job.job_type for job in jobs.enqueued].count(JobType.FETCH_METADATA) == 5
    assert [job.job_type for job in jobs.enqueued].count(JobType.INDEX_VIDEO) == 5
    assert [record.name for record in repo.upserted_sources] == ["mock"]
    assert jobs.jobs[discover_job_id].status is JobStatus.DONE
    assert jobs.failed == []  # INDEX_VIDEO ya no falla transitoriamente (PR-034)


def test_run_worker_invalid_concurrency_is_usage_error() -> None:
    """`--concurrency 0` es error de uso (exit code 2)."""
    result = _invoke(["run-worker", "--concurrency", "0"], _base_context())
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# stats (FR-014 · contracts §5)
# ---------------------------------------------------------------------------


def _stats_fixture() -> CrawlerStats:
    return CrawlerStats(
        jobs_by_status={"done": 5, "pending": 2},
        jobs_by_source={"mock": 6, None: 1},
        videos_by_status={"discovered": 3, "indexed": 3},
        recent_errors=[
            JobErrorRecord(
                job_id=str(uuid.uuid4()),
                job_type="INDEX_VIDEO",
                source="mock",
                video_id=VIDEO_ID,
                error="MockAdapterTimeoutError: timeout inyectado",
                updated_at=datetime(2026, 8, 15, tzinfo=UTC),
            )
        ],
    )


def test_stats_human_and_json_coherent() -> None:
    """`stats` humano y `--json` reflejan la misma información (FR-014)."""
    repo = FakeRepo(stats=_stats_fixture())
    context = _base_context(repo=repo)
    result = _invoke(["stats"], context)
    assert result.exit_code == 0
    assert "jobs por estado: done=5 pending=2" in result.stdout
    assert "jobs por fuente: mock=6 null=1" in result.stdout
    assert "vídeos por estado: discovered=3 indexed=3" in result.stdout
    assert "MockAdapterTimeoutError: timeout inyectado" in result.stdout

    data = _stdout_json(_invoke(["stats", "--json"], context))
    assert data["jobs_by_status"] == {"done": 5, "pending": 2}
    assert data["jobs_by_source"] == {"mock": 6, "null": 1}  # fuente NULL normalizada
    assert data["videos_by_status"] == {"discovered": 3, "indexed": 3}
    assert data["recent_errors"][0]["error"] == "MockAdapterTimeoutError: timeout inyectado"
    assert data["recent_errors"][0]["updated_at"] == "2026-08-15T00:00:00+00:00"


def test_stats_json_is_stable_between_invocations() -> None:
    """`stats --json` emite exactamente el mismo documento en dos invocaciones."""
    repo = FakeRepo(stats=_stats_fixture())
    first = _invoke(["stats", "--json"], _base_context(repo=repo))
    second = _invoke(["stats", "--json"], _base_context(repo=repo))
    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout


def test_stats_empty_counts() -> None:
    """`stats` sin actividad → conteos vacíos y `[]` de errores (JSON válido)."""
    data = _stdout_json(_invoke(["stats", "--json"], _base_context(repo=FakeRepo())))
    assert data == {
        "jobs_by_status": {},
        "jobs_by_source": {},
        "videos_by_status": {},
        "recent_errors": [],
    }


# ---------------------------------------------------------------------------
# check-availability (FR-013 · SEC-002 · contracts §5)
# ---------------------------------------------------------------------------


def test_check_availability_enqueues_jobs_per_video() -> None:
    """`check-availability --source mock` encola CHECK_AVAILABILITY por vídeo (FR-013)."""
    repo = FakeRepo(sources=[_mock_source_record(enabled=False)])

    async def list_videos(source: str, limit: int) -> list[str]:
        assert source == "mock"
        return ["mock-vid-0001", "mock-vid-0002"]

    jobs = FakeJobs()
    result = _invoke(
        ["check-availability", "--source", "mock"],
        _base_context(repo=repo, jobs=jobs, list_source_videos=list_videos),
    )
    data = _stdout_json(result)
    assert data["source"] == "mock"
    assert data["limit"] == 100  # default de config
    assert data["enqueued"] == 2
    assert len(data["job_ids"]) == 2
    types = [job.job_type for job in jobs.enqueued]
    assert types == [JobType.CHECK_AVAILABILITY, JobType.CHECK_AVAILABILITY]
    first = jobs.enqueued[0]
    assert first.payload == {
        "source": "mock",
        "external_id": "mock-vid-0001",
        "dedupe_key": "check_availability:mock:mock-vid-0001",
    }


def test_check_availability_respects_limit() -> None:
    """`--limit N` acota los vídeos consultados y encolados."""
    repo = FakeRepo(sources=[_mock_source_record()])
    calls: list[tuple[str, int]] = []

    async def list_videos(source: str, limit: int) -> list[str]:
        calls.append((source, limit))
        return ["mock-vid-0001", "mock-vid-0002", "mock-vid-0003"][:limit]

    jobs = FakeJobs()
    result = _invoke(
        ["check-availability", "--source", "mock", "--limit", "2"],
        _base_context(repo=repo, jobs=jobs, list_source_videos=list_videos),
    )
    data = _stdout_json(result)
    assert calls == [("mock", 2)]
    assert data["enqueued"] == 2
    assert (
        _invoke(
            ["check-availability", "--source", "mock", "--limit", "0"], _base_context(repo=repo)
        ).exit_code
        == 2
    )  # límite inválido → error de uso


def test_check_availability_no_videos_reports_zero() -> None:
    """Fuente sin vídeos → `enqueued: 0` (muestra el estado, contracts §5)."""
    repo = FakeRepo(sources=[_mock_source_record()])
    data = _stdout_json(
        _invoke(["check-availability", "--source", "mock"], _base_context(repo=repo))
    )
    assert data == {"source": "mock", "limit": 100, "enqueued": 0, "job_ids": []}


def test_check_availability_unknown_or_disabled_source_fails() -> None:
    """Fuente desconocida / no habilitada (SEC-002) → error claro y exit != 0."""
    result = _invoke(
        ["check-availability", "--source", "ghost"], _base_context(registry=AdapterRegistry())
    )
    assert result.exit_code != 0
    assert "fuente desconocida" in result.stderr

    repo = FakeRepo(sources=[_xvideos_source_record(enabled=False)])
    result = _invoke(
        ["check-availability", "--source", "xvideos"],
        _base_context(repo=repo, registry=_registry_with_xvideos()),
    )
    assert result.exit_code != 0
    assert "SEC-002" in result.stderr


# ---------------------------------------------------------------------------
# config.py (PR-032 · contracts §5)
# ---------------------------------------------------------------------------


def test_settings_worker_and_limit_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults de config: concurrency, lease timeout y límites de backfill/check."""
    for key in (
        "XTRACE_CRAWLER_WORKER_CONCURRENCY",
        "XTRACE_CRAWLER_JOB_LEASE_TIMEOUT_SECONDS",
        "XTRACE_CRAWLER_BACKFILL_DEFAULT_LIMIT",
        "XTRACE_CRAWLER_CHECK_AVAILABILITY_DEFAULT_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.worker_concurrency == 4
    assert settings.job_lease_timeout_seconds == 300.0
    assert settings.backfill_default_limit == 50
    assert settings.check_availability_default_limit == 100


def test_settings_worker_and_limit_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overrides por env (D5): los valores de entorno ganan a los defaults."""
    monkeypatch.setenv("XTRACE_CRAWLER_WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("XTRACE_CRAWLER_JOB_LEASE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("XTRACE_CRAWLER_BACKFILL_DEFAULT_LIMIT", "25")
    monkeypatch.setenv("XTRACE_CRAWLER_CHECK_AVAILABILITY_DEFAULT_LIMIT", "10")
    settings = Settings()
    assert settings.worker_concurrency == 8
    assert settings.job_lease_timeout_seconds == 120.0
    assert settings.backfill_default_limit == 25
    assert settings.check_availability_default_limit == 10


def test_settings_invalid_concurrency_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrency 0 por env → error de validación al construir `Settings`."""
    monkeypatch.setenv("XTRACE_CRAWLER_WORKER_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        Settings()
