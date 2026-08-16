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

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError
from typer.testing import CliRunner

from xtrace_crawler.adapters.base import AdapterManifest
from xtrace_crawler.adapters.mock import MockAdapter
from xtrace_crawler.adapters.models import VisualAsset
from xtrace_crawler.adapters.registry import AdapterRegistry
from xtrace_crawler.adapters.xvideos import XvideosAdapter
from xtrace_crawler.cli import CliContext, app
from xtrace_crawler.config import Settings
from xtrace_crawler.jobs.types import Job, JobStatus, JobType
from xtrace_crawler.jobs.worker import JobWorker
from xtrace_crawler.pipeline import CrawlerPipeline
from xtrace_crawler.repo import (
    DEFAULT_RECENT_ERRORS_LIMIT,
    CrawlerStats,
    JobErrorRecord,
    RateLimitStatsRecord,
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
    """Fila `sources` del adapter real xvideos (manifest revisado, SEC-002 · PR-042)."""
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
        self,
        *,
        recent_errors_limit: int = DEFAULT_RECENT_ERRORS_LIMIT,
        rate_limits: dict[str, RateLimitStatsRecord] | None = None,
    ) -> CrawlerStats:
        """`stats` con la sección `rate_limits` (PR-035): el repo la incrusta tal cual."""
        if self.stats_result is None:
            base = CrawlerStats(
                jobs_by_status={}, jobs_by_source={}, videos_by_status={}, recent_errors=[]
            )
        else:
            base = self.stats_result
        return base.model_copy(update={"rate_limits": rate_limits or {}})


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
    assert "robots_reviewed=yes" in result.stdout  # xvideos: manifest revisado (SEC-002, PR-042)
    assert "review_date=2026-08-16" in result.stdout  # aprobación del operador (PR-042)


def test_sources_json_is_stable() -> None:
    """`sources --json` emite JSON estable y ordenado (contratos §5)."""
    repo = FakeRepo(sources=[_xvideos_source_record(), _mock_source_record(enabled=True)])
    result = _invoke(["sources", "--json"], _base_context(repo=repo))
    data = _stdout_json(result)
    assert [entry["name"] for entry in data] == ["mock", "xvideos"]
    assert {"name", "adapter", "enabled", "manifest"} == set(data[0])
    assert data[0]["manifest"]["robots_reviewed"] is True
    assert data[1]["manifest"]["review_date"] == "2026-08-16"  # PR-042: aprobación del operador
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
    PR-036: el payload incluye la cota global `max_videos` (default de config).
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
        "max_videos": 100,
    }
    assert len(jobs.enqueued) == 1
    job = jobs.enqueued[0]
    assert job.job_type is JobType.DISCOVER
    assert job.payload == {
        "source": "mock",
        "cursor": None,
        "limit": 5,
        "mode": "backfill",
        "max_videos": 100,
        "dedupe_key": "discover:mock:None:backfill",
    }
    assert job.source_id is not None and str(job.source_id) == SOURCE_ID


def test_backfill_max_videos_flag_and_default_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-036: `--max-videos N` fija la cota global del backfill (analyze hallazgo 2).

    El flag gana al default; sin flag se usa `XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS`
    (config). El payload del DISCOVER lo incluye y el JSON de salida lo muestra.
    """
    monkeypatch.setenv("XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS", "7")
    repo = FakeRepo(sources=[_mock_source_record()])
    jobs = FakeJobs()
    result = _invoke(
        ["backfill", "--source", "mock"],
        _base_context(repo=repo, jobs=jobs, settings=Settings()),
    )
    data = _stdout_json(result)
    assert data["max_videos"] == 7
    assert jobs.enqueued[0].payload["max_videos"] == 7

    jobs2 = FakeJobs()
    _invoke(
        ["backfill", "--source", "mock", "--max-videos", "3"],
        _base_context(repo=repo, jobs=jobs2, settings=Settings()),
    )
    assert jobs2.enqueued[0].payload["max_videos"] == 3  # el flag gana al env
    assert (
        _invoke(
            ["backfill", "--source", "mock", "--max-videos", "0"],
            _base_context(repo=repo, jobs=FakeJobs()),
        ).exit_code
        == 2
    )  # cota inválida → error de uso


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
        "max_videos": 100,  # PR-036: cota global por defecto (config)
    }
    assert jobs.enqueued[0].payload["mode"] == "incremental"
    assert jobs.enqueued[0].payload["limit"] == 7
    assert jobs.enqueued[0].payload["max_videos"] == 100


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
    assert "sources.enabled=false" in result.stderr  # manifest revisado; falta enabled (PR-042)
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
    """`stats` sin actividad → conteos vacíos, `[]` de errores y `rate_limits` vacío."""
    data = _stdout_json(_invoke(["stats", "--json"], _base_context(repo=FakeRepo())))
    assert data == {
        "jobs_by_status": {},
        "jobs_by_source": {},
        "videos_by_status": {},
        "recent_errors": [],
        "rate_limits": {},
    }


def test_stats_json_includes_rate_limits_section() -> None:
    """PR-035 · SC-005/NFR-004: `stats --json` incluye `rate_limits` por fuente.

    Las métricas acumuladas del `RateLimiter` (requests/rate_limit_waits/
    total_wait_ms) llegan al JSON vía el provider del contexto; los campos
    existentes de FR-014 permanecen intactos (compatibilidad de JSON, contracts §5).
    """
    repo = FakeRepo(stats=_stats_fixture())
    context = _base_context(repo=repo)
    context.rate_limits_provider = lambda: {
        "mock": RateLimitStatsRecord(requests=11, rate_limit_waits=10, total_wait_ms=500)
    }
    data = _stdout_json(_invoke(["stats", "--json"], context))
    assert data["rate_limits"] == {
        "mock": {"requests": 11, "rate_limit_waits": 10, "total_wait_ms": 500}
    }
    # Campos existentes intactos (FR-014 · contracts §5).
    assert data["jobs_by_status"] == {"done": 5, "pending": 2}
    assert data["jobs_by_source"] == {"mock": 6, "null": 1}
    assert data["videos_by_status"] == {"discovered": 3, "indexed": 3}
    assert data["recent_errors"][0]["error"] == "MockAdapterTimeoutError: timeout inyectado"


def test_stats_human_shows_rate_limits() -> None:
    """`stats` (sin --json) muestra la contabilidad de rate limits por fuente (PR-035)."""
    repo = FakeRepo(stats=_stats_fixture())
    context = _base_context(repo=repo)
    context.rate_limits_provider = lambda: {
        "mock": RateLimitStatsRecord(requests=11, rate_limit_waits=10, total_wait_ms=500)
    }
    result = _invoke(["stats"], context)
    assert result.exit_code == 0
    assert "rate limits por fuente: mock=requests=11 waits=10 wait_ms=500" in result.stdout


def test_run_worker_once_logs_stage_durations(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-035 · plan §Observability: logs del pipeline con duración por etapa (caplog).

    Con el cableado real (pipeline PR-030 + registry + fakes, sin BD/red —
    NFR-003), la pasada registra `etapa=discover|metadata|assets|embed` con
    `duration_ms` y el worker `intento N/M` por job (PR-035).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    jobs = FakeJobs(
        jobs=[
            _make_job(
                JobType.DISCOVER,
                payload={"source": "mock", "cursor": None, "limit": 2, "mode": "backfill"},
            )
        ]
    )
    registry = _registry_with_mock(catalog_size=2)
    store = FakeVectorStore()
    with caplog.at_level(logging.INFO):
        result = _invoke(
            ["run-worker", "--once"],
            _base_context(
                repo=repo, jobs=jobs, registry=registry, settings=Settings(), store=store
            ),
        )
    assert _stdout_json(result) == {"processed": 5}  # 1 DISCOVER + 2 FETCH_METADATA + 2 INDEX_VIDEO
    assert "etapa=discover" in caplog.text
    assert "etapa=metadata" in caplog.text
    assert "etapa=assets" in caplog.text
    assert "etapa=embed" in caplog.text
    assert "duration_ms=" in caplog.text
    assert "intento 1/3" in caplog.text  # worker: intento actual/max por job


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
        "XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS",
        "XTRACE_CRAWLER_MAX_IMAGE_PIXELS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.worker_concurrency == 4
    assert settings.job_lease_timeout_seconds == 300.0
    assert settings.backfill_default_limit == 50
    assert settings.check_availability_default_limit == 100
    # PR-036: cota global de backfill y límite de píxeles de imágenes.
    assert settings.backfill_max_videos == 100
    assert settings.max_image_pixels == 50_000_000


def test_settings_worker_and_limit_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overrides por env (D5): los valores de entorno ganan a los defaults."""
    monkeypatch.setenv("XTRACE_CRAWLER_WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("XTRACE_CRAWLER_JOB_LEASE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("XTRACE_CRAWLER_BACKFILL_DEFAULT_LIMIT", "25")
    monkeypatch.setenv("XTRACE_CRAWLER_CHECK_AVAILABILITY_DEFAULT_LIMIT", "10")
    monkeypatch.setenv("XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS", "250")
    monkeypatch.setenv("XTRACE_CRAWLER_MAX_IMAGE_PIXELS", "1000000")
    settings = Settings()
    assert settings.worker_concurrency == 8
    assert settings.job_lease_timeout_seconds == 120.0
    assert settings.backfill_default_limit == 25
    assert settings.check_availability_default_limit == 10
    assert settings.backfill_max_videos == 250
    assert settings.max_image_pixels == 1_000_000


def test_settings_invalid_new_limits_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-036: cotas inválidas por env (0) fallan al construir `Settings`."""
    monkeypatch.setenv("XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS", "0")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.delenv("XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS")
    monkeypatch.setenv("XTRACE_CRAWLER_MAX_IMAGE_PIXELS", "0")
    with pytest.raises(ValidationError):
        Settings()


# ---------------------------------------------------------------------------
# PR-036 · Cota global --max-videos en el pipeline (analyze hallazgo 2)
# ---------------------------------------------------------------------------


def test_run_worker_max_videos_caps_discover_with_traceability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-036 (analyze hallazgo 2 · SC-002): `max_videos` corta el discover SIN
    perder trazabilidad.

    Catálogo de 5 con página `limit=2` y cota 3: la 1ª página procesa 2 vídeos
    y encola el siguiente DISCOVER (`videos_counted=2`); la 2ª página procesa
    el 3er vídeo, alcanza la cota y NO encola más DISCOVER (el resto del
    catálogo queda fuera). Quedan 3 vídeos únicos `indexed`, 3 FETCH_METADATA
    y 3 INDEX_VIDEO con payload `source`/`external_id` y `dedupe_key`
    (trazabilidad FR-007), 0 fallos.
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    jobs = FakeJobs(
        jobs=[
            _make_job(
                JobType.DISCOVER,
                payload={
                    "source": "mock",
                    "cursor": None,
                    "limit": 2,
                    "mode": "backfill",
                    "max_videos": 3,
                },
            )
        ]
    )
    registry = _registry_with_mock(catalog_size=5)
    store = FakeVectorStore()
    result = _invoke(
        ["run-worker", "--once"],
        _base_context(repo=repo, jobs=jobs, registry=registry, settings=Settings(), store=store),
    )
    assert _stdout_json(result) == {"processed": 8}  # 2 DISCOVER + 3 FETCH + 3 INDEX

    # La cota cortó el catálogo: solo 3 vídeos, todos indexados (FR-007 · SC-003).
    assert sorted(repo.videos) == ["mock-vid-0000", "mock-vid-0001", "mock-vid-0002"]
    assert all(record.status == "indexed" for record in repo.videos.values())

    # Sin más DISCOVER tras la cota: el único encolado lleva el cursor y el
    # contador acumulado (trazabilidad de la cadena de paginación).
    discovers = [job for job in jobs.enqueued if job.job_type is JobType.DISCOVER]
    assert len(discovers) == 1
    assert discovers[0].payload["cursor"] == "2"
    assert discovers[0].payload["videos_counted"] == 2
    assert discovers[0].payload["max_videos"] == 3

    # Trazabilidad de los vídeos procesados: jobs con source/external_id y dedupe_key.
    fetch_jobs = [job for job in jobs.enqueued if job.job_type is JobType.FETCH_METADATA]
    index_jobs = [job for job in jobs.enqueued if job.job_type is JobType.INDEX_VIDEO]
    assert len(fetch_jobs) == 3 and len(index_jobs) == 3
    assert all(job.payload["external_id"] in repo.videos for job in fetch_jobs)
    assert all(
        str(job.payload.get("dedupe_key", "")).startswith("fetch_metadata:mock:")
        for job in fetch_jobs
    )
    assert jobs.failed == []
    assert all(job.status is not JobStatus.RUNNING for job in jobs.jobs.values())


# ---------------------------------------------------------------------------
# PR-036 · SSRF: allowlist por fuente en el pipeline (SEC-001)
# ---------------------------------------------------------------------------


class _HttpOnlyAssetsAdapter(MockAdapter):
    """MockAdapter SIN `fetch_asset_bytes` (PR-034): todo asset va por HTTP."""

    fetch_asset_bytes: Callable[[str], Any] | None = None


class _ForeignHostAssetsAdapter(_HttpOnlyAssetsAdapter):
    """Adapter cuyos assets viven en un host ajeno a la allowlist DECLARADA.

    `asset_hosts` (PR-036) declara `allowed.example.com`; el asset parseado
    apunta a `parsed.example.com` — la allowlist NO se deriva de las URLs.
    """

    asset_hosts: frozenset[str] = frozenset({"allowed.example.com"})

    async def get_visual_assets(self, video: Any) -> list[VisualAsset]:
        return [VisualAsset(kind="thumbnail", url="https://parsed.example.com/thumb.jpg")]


class _BigInProcessAssetsAdapter(MockAdapter):
    """MockAdapter que sirve imágenes IN-PROCESS sobre el límite de píxeles (PR-036)."""

    asset_hosts: frozenset[str] = frozenset()  # no aplica: todo in-process

    async def fetch_asset_bytes(self, url: str) -> bytes | None:
        buffer = io.BytesIO()
        Image.new("RGB", (200, 200), (1, 2, 3)).save(buffer, format="JPEG")
        return buffer.getvalue()


def _pipeline_with_adapter(
    adapter: Any,
    *,
    repo: FakeRepo | None = None,
    jobs: FakeJobs | None = None,
    max_image_pixels: int | None = None,
) -> tuple[CrawlerPipeline, JobWorker, FakeJobs, FakeRepo]:
    """Pipeline + worker directos sobre fakes (sin CLI), para tests de handlers."""
    repo = repo if repo is not None else FakeRepo(sources=[_mock_source_record()])
    jobs = jobs if jobs is not None else FakeJobs()
    pipeline = CrawlerPipeline(
        repo=repo,
        jobs=jobs,
        adapter_for=lambda _job: adapter,
        store=FakeVectorStore(),
        max_image_pixels=max_image_pixels,
    )
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)
    return pipeline, worker, jobs, repo


def _seed_and_index(
    pipeline: CrawlerPipeline,
    worker: JobWorker,
    jobs: FakeJobs,
    repo: FakeRepo,
    adapter: Any,
    *,
    external_id: str,
) -> int:
    """Siembra el vídeo del catálogo y procesa un INDEX_VIDEO (pasada única)."""

    async def scenario() -> int:
        video = adapter.catalog_snapshot()[external_id]
        await repo.upsert_web_video(SOURCE_ID, video)
        await jobs.enqueue(
            JobType.INDEX_VIDEO,
            payload={"source": "mock", "external_id": external_id},
        )
        return await worker.run_once()

    return asyncio.run(scenario())


def test_asset_host_outside_source_allowlist_rejected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """PR-036 · SEC-001: la allowlist por fuente NO se deriva de las URLs parseadas.

    El adapter declara `asset_hosts={"allowed.example.com"}` pero el asset
    apunta a `parsed.example.com` (URL bien formada y parseable): el host
    parseado NO concede acceso — `HostNotAllowedError` antes de tocar la red y
    el asset degrada (el vídeo reintenta, sin fallo silencioso).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    adapter = _ForeignHostAssetsAdapter(seed=42, catalog_size=1)
    pipeline, worker, jobs, repo = _pipeline_with_adapter(adapter)

    with caplog.at_level(logging.INFO):
        processed = _seed_and_index(
            pipeline, worker, jobs, repo, adapter, external_id="mock-vid-0000"
        )

    assert processed == 1
    assert "omitido por degradación" in caplog.text
    assert "host 'parsed.example.com' no está en la allowlist" in caplog.text
    # Sin frames → el vídeo no se indexa; el job queda pendiente (transitorio).
    assert repo.videos["mock-vid-0000"].status == "indexing"
    job = next(iter(jobs.jobs.values()))
    assert job.status is JobStatus.PENDING
    assert "no se obtuvieron frames" in (job.error or "")


def test_adapter_without_asset_allowlist_blocks_http_download(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """PR-036 · SEC-001: un adapter real SIN allowlist declarada NO descarga assets.

    Sin `asset_hosts` en el adapter, el pipeline rehúsa construir el cliente
    HTTP con `NoAssetHostsError` (error tipado): los assets degradan con
    warning y nunca se abre un socket (fail-closed).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    adapter = _HttpOnlyAssetsAdapter(seed=42, catalog_size=1)
    pipeline, worker, jobs, repo = _pipeline_with_adapter(adapter)

    with caplog.at_level(logging.INFO):
        processed = _seed_and_index(
            pipeline, worker, jobs, repo, adapter, external_id="mock-vid-0000"
        )

    assert processed == 1
    assert "omitido por degradación" in caplog.text
    assert "asset_hosts" in caplog.text  # NoAssetHostsError: allowlist no declarada
    job = next(iter(jobs.jobs.values()))
    assert job.status is JobStatus.PENDING


def test_in_process_asset_over_pixel_limit_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """PR-036 · decompression bomb: una imagen in-process sobre el límite de
    píxeles se rechaza con error tipado y degrada por asset.

    El límite del pipeline (aquí 10 000 px; default 50 MP vía config) aplica
    también a los bytes servidos in-process por el adapter (PR-034).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    adapter = _BigInProcessAssetsAdapter(seed=42, catalog_size=1)
    pipeline, worker, jobs, repo = _pipeline_with_adapter(adapter, max_image_pixels=10_000)

    with caplog.at_level(logging.INFO):
        processed = _seed_and_index(
            pipeline, worker, jobs, repo, adapter, external_id="mock-vid-0000"
        )

    assert processed == 1
    assert "omitido por degradación" in caplog.text
    assert "supera el límite de píxeles" in caplog.text
    assert repo.videos["mock-vid-0000"].status == "indexing"  # sin frames indexados
    job = next(iter(jobs.jobs.values()))
    assert job.status is JobStatus.PENDING


def test_mock_offline_still_indexes_without_asset_hosts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """PR-034 (regresión) + PR-036: el mock indexa OFFLINE sin allowlist de hosts.

    El mock sirve storyboard/thumbnails in-process; el `preview.mp4` (sin
    representación in-process) cae a la ruta HTTP y, como el mock no declara
    `asset_hosts`, se degrada con `NoAssetHostsError` — el vídeo se indexa
    igual con 0 superficie de red (FR-003 · SC-001 · NFR-003).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    jobs = FakeJobs(
        jobs=[
            _make_job(
                JobType.DISCOVER,
                payload={
                    "source": "mock",
                    "cursor": None,
                    "limit": 1,
                    "mode": "backfill",
                    "max_videos": 10,
                },
            )
        ]
    )
    registry = _registry_with_mock(catalog_size=1)
    store = FakeVectorStore()
    with caplog.at_level(logging.INFO):
        result = _invoke(
            ["run-worker", "--once"],
            _base_context(
                repo=repo, jobs=jobs, registry=registry, settings=Settings(), store=store
            ),
        )
    assert _stdout_json(result) == {"processed": 3}  # DISCOVER + FETCH + INDEX
    assert all(record.status == "indexed" for record in repo.videos.values())
    assert store.frames  # frames indexados sin red (PR-034)
    assert "omitido por degradación" in caplog.text
    assert "asset_hosts" in caplog.text  # el preview degrada con NoAssetHostsError
    assert jobs.failed == []


def test_settings_invalid_concurrency_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrency 0 por env → error de validación al construir `Settings`."""
    monkeypatch.setenv("XTRACE_CRAWLER_WORKER_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        Settings()
