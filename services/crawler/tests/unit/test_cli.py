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
  claro y exit code != 0. PR-049: `--section <path>` (obligatoriamente con
  '/' inicial; si no, error de uso) acota el discover a la sección del sitio
  (categoría/tag, p. ej. `/tags/xxx`): el payload lleva `section` (null sin
  flag), el dedupe distingue la cadena y el JSON de salida incluye `section`
  cuando se da.
- `run-worker`: `--once` procesa una pasada (FR-006): con worker inyectado
  (jobs fake) y sin inyección (cablea pipeline PR-030 + registry, todo fake).
- `stats`: estadísticas coherentes (FR-014) y JSON estable.
- `check-availability`: encola CHECK_AVAILABILITY por vídeo de la fuente
  (FR-013).
- `config.py`: defaults de worker/lease/límites y overrides por env (PR-032).
- PR-050/051 (FR-011 · contracts §6): proveedor de embeddings por env —
  default `fake` intacto; con `siglip`, el contexto por defecto construye el
  `SiglipLocalProvider` real (monkeypatch del import/constructor, sin torch);
  y si el extra `siglip` NO está instalado (o el import del módulo falla),
  el CLI falla en el arranque con error claro y accionable
  (`uv sync --extra siglip`), sin fallback silencioso a fake (PR-051).

Trazabilidad (constitución §3): cada test indica el requisito que valida.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import io
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from typer.testing import CliRunner
from xtrace_spike.sampling import AdaptiveSamplingPolicy

from xtrace_crawler.adapters.base import AdapterManifest
from xtrace_crawler.adapters.mock import MockAdapter
from xtrace_crawler.adapters.models import DiscoverPage, VisualAsset
from xtrace_crawler.adapters.registry import AdapterRegistry
from xtrace_crawler.adapters.xvideos import XvideosAdapter
from xtrace_crawler.cli import CliContext, _default_context, app
from xtrace_crawler.config import Settings
from xtrace_crawler.jobs.types import Job, JobStatus, JobType
from xtrace_crawler.jobs.worker import JobWorker
from xtrace_crawler.pipeline import CrawlerPipeline, reindex_dedupe_key
from xtrace_crawler.repo import (
    DEFAULT_RECENT_ERRORS_LIMIT,
    CrawlerRepo,
    CrawlerStats,
    JobErrorRecord,
    RateLimitStatsRecord,
    SourceRecord,
    VideoRecord,
    VideoStateSnapshot,
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
        reindex_status_result: dict[str, Any] | None = None,
    ) -> None:
        self.sources: dict[str, SourceRecord] = {record.name: record for record in (sources or [])}
        self.videos: dict[str, VideoRecord] = {
            record.external_id: record for record in (videos or [])
        }
        self.stats_result = stats
        self.upserted_sources: list[SourceRecord] = []
        self.upserted_videos: list[VideoRecord] = []
        self.status_changes: list[tuple[str, str]] = []
        self.reindex_results: list[tuple[uuid.UUID, str, int, str | None]] = []
        self.excluded: list[str] = []
        self.reindex_status_result = reindex_status_result

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

    async def list_reindex_candidates(self, source_name: str, limit: int) -> list[VideoRecord]:
        source = self.sources.get(source_name)
        if source is None or not source.enabled:
            return []
        return [
            record
            for record in sorted(self.videos.values(), key=lambda item: item.external_id or "")
            if record.source_id == source.id
            and record.external_id is not None
            and not record.excluded
            and record.status in {"indexed", "failed"}
        ][:limit]

    async def reindex_status(self, run_id: str) -> dict[str, Any]:
        if self.reindex_status_result is None:
            return {
                "run_id": run_id,
                "pending": 0,
                "completed": 0,
                "skipped": 0,
                "failed": 0,
                "frames": 0,
                "results": [],
            }
        return self.reindex_status_result

    async def set_reindex_result(
        self,
        job_id: uuid.UUID,
        *,
        outcome: str,
        frames: int,
        reason: str | None,
    ) -> bool:
        self.reindex_results.append((job_id, outcome, frames, reason))
        return True

    async def set_video_status(self, video_id: str, status: VideoStatus) -> bool:
        for external_id, record in self.videos.items():
            if record.id == video_id:
                self.videos[external_id] = record.model_copy(update={"status": status})
                self.status_changes.append((video_id, status))
                return True
        return False

    async def snapshot_video_state(self, video_id: str) -> VideoStateSnapshot | None:
        """Public state snapshot used by the non-atomic store test boundary."""
        record = next((record for record in self.videos.values() if record.id == video_id), None)
        if record is None:
            return None
        return record.status, record.frame_count, record.duration_ms, record.error

    async def restore_video_state(self, video_id: str, snapshot: VideoStateSnapshot | None) -> None:
        """Restore the complete fake row after a publication failure."""
        for external_id, record in list(self.videos.items()):
            if record.id == video_id:
                if snapshot is None:
                    del self.videos[external_id]
                else:
                    status, frame_count, duration_ms, error = snapshot
                    self.videos[external_id] = record.model_copy(
                        update={
                            "status": status,
                            "frame_count": frame_count,
                            "duration_ms": duration_ms,
                            "error": error,
                        }
                    )
                return

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
        self.replacements: list[tuple[str, list[Any], int | None]] = []
        self.fail_replacement = False

    async def upsert_frames(self, frames: Sequence[Any]) -> int:
        self.frames.extend(frames)
        return len(frames)

    async def replace_video_index(
        self, video_id: str, frames: Sequence[Any], *, duration_ms: int | None
    ) -> None:
        if self.fail_replacement:
            raise RuntimeError("fallo de reemplazo de prueba")
        self.frames = [frame for frame in self.frames if frame["video_id"] != video_id]
        self.frames.extend(frames)
        self.replacements.append((video_id, list(frames), duration_ms))

    async def snapshot_video_index(self) -> object:
        """Public snapshot used by the non-atomic store test boundary."""
        return list(self.frames)

    async def restore_video_index(self, snapshot: object) -> None:
        """Restore frames after a state-publication failure."""
        if not isinstance(snapshot, list):
            raise ValueError("snapshot de índice fake inválido")
        self.frames = list(snapshot)

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


class _StaticCursor:
    """Cursor async sin BD para verificar SQL parametrizado del repo."""

    def __init__(self, rows: list[tuple[Any, ...]], *, rowcount: int = 1) -> None:
        self.rows = rows
        self.rowcount = rowcount
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _StaticCursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.executed.append((query, params))

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _StaticConnection:
    def __init__(self, cursor: _StaticCursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> _StaticConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _StaticCursor:
        return self._cursor


class _StaticCrawlerRepo(CrawlerRepo):
    """CrawlerRepo con conexión estática para unitarios SQL no destructivos."""

    def __init__(self, rows: list[tuple[Any, ...]], *, rowcount: int = 1) -> None:
        self.cursor_double = _StaticCursor(rows, rowcount=rowcount)
        self.connection_double = _StaticConnection(self.cursor_double)

    async def connect(self) -> Any:
        return self.connection_double


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
    embeddings: Any | None = None,
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
        embeddings=embeddings,
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
    PR-049: el payload incluye `section` (null sin `--section`).
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
        "section": None,  # PR-049: null sin --section
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


def test_backfill_section_sin_barra_inicial_es_error_de_uso() -> None:
    """PR-049: `--section` sin '/' inicial (o vacío) es error de uso (exit 2).

    El contrato exige que la sección sea una ruta absoluta del sitio
    (categoría/tag, p. ej. `/tags/xxx`): sin la barra inicial el comando
    termina con error de uso (exit code 2, contracts §5) y stdout vacío.
    """
    repo = FakeRepo(sources=[_mock_source_record()])
    context = _base_context(repo=repo)
    result = _invoke(["backfill", "--source", "mock", "--section", "tags/x"], context)
    assert result.exit_code == 2
    assert "section" in result.stderr
    assert result.stdout == ""
    assert (
        _invoke(["backfill", "--source", "mock", "--section", ""], context).exit_code == 2
    )  # vacía también es inválida


def test_backfill_section_en_payload_json_y_dedupe() -> None:
    """PR-049: `--section /ruta` viaja en el payload del DISCOVER, en el JSON de
    salida y distingue la cadena en el dedupe; sin flag, el payload lleva
    `section: null` y el JSON NO incluye la clave.
    """
    repo = FakeRepo(sources=[_mock_source_record()])
    jobs = FakeJobs()
    data = _stdout_json(
        _invoke(
            ["backfill", "--source", "mock", "--section", "/tags/xxx"],
            _base_context(repo=repo, jobs=jobs),
        )
    )
    assert data["section"] == "/tags/xxx"
    job = jobs.enqueued[0]
    assert job.job_type is JobType.DISCOVER
    assert job.payload["section"] == "/tags/xxx"
    assert job.payload["dedupe_key"] == "discover:mock:None:backfill:/tags/xxx"

    jobs2 = FakeJobs()
    data2 = _stdout_json(
        _invoke(["backfill", "--source", "mock"], _base_context(repo=repo, jobs=jobs2))
    )
    assert "section" not in data2  # JSON sin section cuando no se da
    assert jobs2.enqueued[0].payload["section"] is None
    assert jobs2.enqueued[0].payload["dedupe_key"] == "discover:mock:None:backfill"


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


class _SectionRecordingAdapter(MockAdapter):
    """MockAdapter que registra `(cursor, section)` de cada discover (PR-049).

    Solo registra y delega en el catálogo sintético: la sección se **acepta y
    se ignora** (retrocompatible) — el flujo del pipeline es idéntico.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.discover_calls: list[tuple[str | None, str | None]] = []

    async def discover(
        self, *, cursor: str | None, limit: int, section: str | None = None
    ) -> DiscoverPage:
        self.discover_calls.append((cursor, section))
        return await super().discover(cursor=cursor, limit=limit, section=section)


def test_run_worker_pasa_section_al_discover_y_el_mock_la_ignora(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-049: el pipeline reenvía `section` del payload a `discover` del adapter.

    El `MockAdapter` la **acepta y la ignora** (retrocompatible): con una
    sección en el payload del DISCOVER, la pasada completa igual que sin ella
    (catálogo sintético plano, sin secciones), el adapter registra la sección
    recibida en su única llamada a discover y todos los vídeos se indexan.
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
                    "limit": 5,
                    "mode": "backfill",
                    "section": "/tags/xxx",
                },
            )
        ]
    )
    adapter = _SectionRecordingAdapter(catalog_size=5)
    registry = AdapterRegistry()
    registry.register(adapter, real=False)
    store = FakeVectorStore()
    result = _invoke(
        ["run-worker", "--once"],
        _base_context(repo=repo, jobs=jobs, registry=registry, settings=Settings(), store=store),
    )
    assert _stdout_json(result) == {"processed": 11}  # 1 DISCOVER + 5 FETCH + 5 INDEX
    assert adapter.discover_calls == [(None, "/tags/xxx")]
    assert all(record.status == "indexed" for record in repo.videos.values())
    assert jobs.failed == []


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


# ---------------------------------------------------------------------------
# PR-050/051 · Proveedor de embeddings por env (FR-011 · ADR-0011 · contracts §6)
# ---------------------------------------------------------------------------


def test_settings_embeddings_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-050 · FR-011: `XTRACE_CRAWLER_EMBEDDINGS` parsea fake/siglip y rechaza otros.

    El default es `fake` (igual que hoy, PR-030): los tests/CI siguen con el
    `FakeEmbeddingProvider` determinista del pipeline y nunca cargan torch.
    Un valor desconocido falla al construir `Settings` (fail-fast).
    """
    monkeypatch.delenv("XTRACE_CRAWLER_EMBEDDINGS", raising=False)
    assert Settings().embeddings == "fake"  # default intacto
    monkeypatch.setenv("XTRACE_CRAWLER_EMBEDDINGS", "siglip")
    assert Settings().embeddings == "siglip"
    monkeypatch.setenv("XTRACE_CRAWLER_EMBEDDINGS", "openai")
    with pytest.raises(ValidationError):
        Settings()


def test_default_context_embeddings_fake_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-050: sin env (default fake) el contexto por defecto NO inyecta proveedor.

    `CliContext.embeddings=None` → el pipeline (PR-030) usa su default, el
    `FakeEmbeddingProvider` determinista del spike (sin torch). El switch
    solo se evalúa en el contexto por defecto real: los tests inyectan su
    propio `CliContext` (NFR-003) y nunca pasan por aquí.
    """
    monkeypatch.delenv("XTRACE_CRAWLER_EMBEDDINGS", raising=False)
    context = _default_context()  # repos reales pero sin conexión (sin BD/red)
    assert context.embeddings is None


def test_default_context_siglip_builds_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-050 · FR-011: con `XTRACE_CRAWLER_EMBEDDINGS=siglip` el contexto por
    defecto construye el `SiglipLocalProvider` REAL de `xtrace_spike`.

    Se monkeypatchea el import dinámico del módulo del spike (y el
    constructor) para NO cargar torch de verdad: el switch importa
    `xtrace_spike.embeddings.siglip_local` e instancia `SiglipLocalProvider()`
    sin argumentos (paridad con el spike: PR-005 · `resolve_embedding_provider`
    del CLI del spike instancia la clase sin args; model_id/dimension L2).
    PR-051: además se stubbea `find_spec` (extra `siglip` instalado) porque el
    contexto por defecto exige el extra en el arranque (fail-fast).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_EMBEDDINGS", "siglip")
    imported: list[str] = []
    instantiated: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _find_spec_installed(name: str) -> Any:
        if name in ("open_clip", "torch"):
            return object()  # extra siglip instalado (stub)
        return importlib.util.find_spec(name)

    monkeypatch.setattr("xtrace_crawler.cli.find_spec", _find_spec_installed)

    class _FakeSiglipProvider:
        """Contrato `EmbeddingProvider` del spike (model_id/dimension L2)."""

        model_id = "openclip-ViT-B-16-SigLIP-webli"
        dimension = 768

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            instantiated.append((args, kwargs))

        def embed_images(self, images: Sequence[Any]) -> Any:
            raise AssertionError("no se deben calcular embeddings en este test")

    class _FakeSiglipModule:
        SiglipLocalProvider = _FakeSiglipProvider

    real_import_module = importlib.import_module

    def _import_spy(name: str) -> Any:
        if name == "xtrace_spike.embeddings.siglip_local":
            imported.append(name)
            return _FakeSiglipModule()
        return real_import_module(name)

    monkeypatch.setattr(importlib, "import_module", _import_spy)
    context = _default_context()
    assert imported == ["xtrace_spike.embeddings.siglip_local"]
    assert isinstance(context.embeddings, _FakeSiglipProvider)
    assert instantiated == [((), {})]  # SiglipLocalProvider() sin argumentos (PR-005)


def test_default_context_siglip_sin_extra_error_claro(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-051 · FR-011 · contracts §6: con `XTRACE_CRAWLER_EMBEDDINGS=siglip` y el
    extra `siglip` NO instalado, el CLI falla en el arranque con error claro.

    Regresión del hallazgo de la 1a ejecución real con SigLIP (2026-08-16):
    el proveedor del spike importa torch/open_clip de forma LAZY (`_ensure_loaded`,
    PR-005) y el `run-worker` fallaba job a job con `ModuleNotFoundError: No
    module named 'open_clip'` — opaco y tardío. Ahora el contexto por defecto
    **exige el extra al arrancar** (`_require_siglip_extra`, fail-fast): el
    CLI termina con **exit 1** (contracts §5) y un mensaje que menciona el
    extra `siglip` y el comando de instalación (`uv sync --extra siglip` en
    services/crawler o equivalente) — **sin fallback silencioso a fake** (un
    backfill con fake cuando se pidió SigLIP corrompería la validación). El
    default `fake` sigue intacto (cubierto por
    `test_default_context_embeddings_fake_by_default`).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_EMBEDDINGS", "siglip")

    def _find_spec_missing(name: str) -> Any:
        if name in ("open_clip", "torch"):
            return None  # extra siglip NO instalado
        return importlib.util.find_spec(name)

    monkeypatch.setattr("xtrace_crawler.cli.find_spec", _find_spec_missing)
    result = runner.invoke(app, ["sources"])  # obj=None → contexto por defecto real
    assert result.exit_code == 1  # error claro, no salida JSON con fake
    assert "siglip" in result.stderr
    assert "uv sync --extra siglip" in result.stderr
    assert "services/crawler" in result.stderr
    assert "open_clip" in result.stderr  # detalla qué falta
    assert "fake" in result.stderr  # menciona explícitamente que no hay fallback


def test_default_context_siglip_import_module_fallido_error_claro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-051: si el import del módulo del spike falla (con el extra presente),
    el mensaje sigue siendo claro y accionable (except ModuleNotFoundError).

    Camino defensivo del helper: `_require_siglip_extra` ya verifica el extra
    (aquí stubbeado como instalado), pero el import/instancia de
    `xtrace_spike.embeddings.siglip_local` puede fallar igualmente (módulo
    roto/renombrado) y NO debe propagar un traceback opaco: `CliUserError`
    con el comando de instalación y sin fallback a fake (exit 1).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_EMBEDDINGS", "siglip")

    def _find_spec_installed(name: str) -> Any:
        if name in ("open_clip", "torch"):
            return object()  # extra siglip instalado (stub)
        return importlib.util.find_spec(name)

    monkeypatch.setattr("xtrace_crawler.cli.find_spec", _find_spec_installed)
    real_import_module = importlib.import_module

    def _import_fail(name: str) -> Any:
        if name == "xtrace_spike.embeddings.siglip_local":
            raise ModuleNotFoundError("No module named 'open_clip'")
        return real_import_module(name)

    monkeypatch.setattr(importlib, "import_module", _import_fail)
    result = runner.invoke(app, ["sources"])  # obj=None → contexto por defecto real
    assert result.exit_code == 1
    assert "siglip" in result.stderr
    assert "uv sync --extra siglip" in result.stderr
    assert "fake" in result.stderr  # sin fallback silencioso a fake


def test_run_worker_uses_injected_embeddings_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-050: la inyección de `deps.embeddings` para tests se mantiene intacta.

    Los tests inyectan su propio proveedor (aquí un fake contador) vía
    `CliContext.embeddings` y el pipeline (PR-030) lo usa en INDEX_VIDEO —
    sin torch y sin pasar por el switch del contexto por defecto (NFR-003).
    """
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    jobs = FakeJobs(
        jobs=[
            _make_job(
                JobType.DISCOVER,
                payload={"source": "mock", "cursor": None, "limit": 1, "mode": "backfill"},
            )
        ]
    )
    registry = _registry_with_mock(catalog_size=1)
    store = FakeVectorStore()

    class _CountingProvider:
        model_id = "test-counting"
        dimension = 768

        def __init__(self) -> None:
            self.embedded_images = 0

        def embed_images(self, images: Sequence[Any]) -> Any:
            self.embedded_images += len(images)
            return np.zeros((len(images), self.dimension), dtype=np.float32)

    provider = _CountingProvider()
    result = _invoke(
        ["run-worker", "--once"],
        _base_context(
            repo=repo,
            jobs=jobs,
            registry=registry,
            settings=Settings(),
            store=store,
            embeddings=provider,
        ),
    )
    assert _stdout_json(result) == {"processed": 3}  # DISCOVER + FETCH + INDEX
    assert all(record.status == "indexed" for record in repo.videos.values())
    assert provider.embedded_images > 0  # el proveedor inyectado fue usado por el pipeline


# ---------------------------------------------------------------------------
# TASK-005-003 · REINDEX reproducible y estado agregado (FR-009/010/011)
# ---------------------------------------------------------------------------


def _seed_reindex_video(
    repo: FakeRepo,
    adapter: MockAdapter,
    external_id: str,
    *,
    status: VideoStatus = "indexed",
    excluded: bool = False,
    frame_count: int | None = None,
) -> VideoRecord:
    """Siembra un vídeo elegible/no elegible sin BD ni corpus real."""
    record = asyncio.run(repo.upsert_web_video(SOURCE_ID, adapter.catalog_snapshot()[external_id]))
    update: dict[str, Any] = {"status": status, "excluded": excluded}
    if frame_count is not None:
        update["frame_count"] = frame_count
    updated = record.model_copy(update=update)
    repo.videos[external_id] = updated
    return updated


def _reindex_payload(
    *, run_id: str, source: str = "mock", external_id: str = "mock-vid-0000"
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source": source,
        "external_id": external_id,
        "sampling": {
            "mode": "adaptive",
            "target_interval_seconds": 120,
            "max_frames": 8,
        },
    }


def test_reindex_cli_filters_eligible_videos_and_emits_canonical_profile() -> None:
    """FR-009/SEC-005: solo indexed/failed, no excluidos, con perfil explícito."""
    adapter = MockAdapter(seed=42, catalog_size=3)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    eligible = _seed_reindex_video(repo, adapter, "mock-vid-0000", status="indexed")
    failed = _seed_reindex_video(repo, adapter, "mock-vid-0001", status="failed")
    _seed_reindex_video(repo, adapter, "mock-vid-0002", status="discovered")
    excluded = _seed_reindex_video(repo, adapter, "mock-vid-0002", status="indexed", excluded=True)
    assert eligible.excluded is False and failed.status == "failed" and excluded.excluded is True
    jobs = FakeJobs()

    result = _invoke(
        [
            "reindex",
            "--source",
            "mock",
            "--limit",
            "10",
            "--sampling",
            "adaptive",
        ],
        _base_context(repo=repo, jobs=jobs, registry=_registry_with_mock(catalog_size=3)),
    )

    data = _stdout_json(result)
    assert data["source"] == "mock"
    assert data["selected"] == 2
    assert data["enqueued"] == 2
    assert data["sampling"] == {
        "mode": "adaptive",
        "target_interval_seconds": 120,
        "max_frames": 8,
    }
    assert uuid.UUID(data["run_id"])
    assert len({job.payload["run_id"] for job in jobs.enqueued}) == 1
    assert {job.payload["external_id"] for job in jobs.enqueued} == {
        "mock-vid-0000",
        "mock-vid-0001",
    }
    dedupe_keys = {job.payload["dedupe_key"] for job in jobs.enqueued}
    assert len(dedupe_keys) == 2
    assert all(key.startswith("reindex:mock:mock-vid-") for key in dedupe_keys)
    assert all(len(key.rsplit(":", 1)[-1]) == 64 for key in dedupe_keys)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--sampling", "legacy_fixed"), ("--max-frames", "7"), ("--target-interval-seconds", "60")],
)
def test_reindex_cli_rejects_non_explicit_adaptive_profile(flag: str, value: str) -> None:
    """FR-009: REINDEX no acepta perfiles distintos de adaptive/120s/8."""
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    result = _invoke(
        ["reindex", "--source", "mock", flag, value],
        _base_context(repo=repo, registry=_registry_with_mock()),
    )
    assert result.exit_code == 1
    assert "adaptive" in result.stderr or "120" in result.stderr or "8" in result.stderr


def test_reindex_handler_revalidates_source_and_replaces_complete_index() -> None:
    """FR-010/SEC-005: handler valida elegibilidad y publica un reemplazo completo."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(repo, adapter, "mock-vid-0000")
    jobs = FakeJobs()
    store = FakeVectorStore()
    pipeline = CrawlerPipeline(
        repo=repo,
        jobs=jobs,
        adapter_for=lambda _job: adapter,
        store=store,
        sampling_policy=AdaptiveSamplingPolicy(),
    )
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)
    run_id = str(uuid.uuid4())

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=run_id, external_id="mock-vid-0000"),
            video_id=uuid.UUID(record.id),
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert all(job.status is JobStatus.DONE for job in jobs.jobs.values())
    assert repo.videos["mock-vid-0000"].status == "indexed"
    assert len(store.replacements) == 1
    video_id, frames, duration_ms = store.replacements[0]
    assert video_id == record.id
    assert 1 <= len(frames) <= 8
    assert duration_ms == record.duration_ms
    assert (record.id, "indexing") not in repo.status_changes
    job = next(iter(jobs.jobs.values()))
    assert repo.reindex_results == [(job.id, "completed", len(frames), None)]


@pytest.mark.parametrize(
    ("initial_status", "frame_count", "expected_status", "marks_failed"),
    [
        ("indexed", 3, "indexed", False),
        ("indexed", 0, "failed", True),
        ("failed", 0, "failed", True),
    ],
)
def test_reindex_failure_preserves_previous_index_and_video_status(
    initial_status: VideoStatus,
    frame_count: int,
    expected_status: VideoStatus,
    marks_failed: bool,
) -> None:
    """FR-010/FR-011: falla conserva estado con índice o marca failed sin índice."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(
        repo,
        adapter,
        "mock-vid-0000",
        status=initial_status,
        frame_count=frame_count,
    )
    jobs = FakeJobs()
    store = FakeVectorStore()
    previous = [{"video_id": record.id, "frame_id": "old", "timestamp_ms": 1}]
    store.frames = list(previous) if frame_count else []
    store.fail_replacement = True
    pipeline = CrawlerPipeline(
        repo=repo,
        jobs=jobs,
        adapter_for=lambda _job: adapter,
        store=store,
    )
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)
    run_id = str(uuid.uuid4())

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=run_id),
            video_id=uuid.UUID(record.id),
            max_attempts=1,
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert store.frames == (previous if frame_count else [])
    assert repo.videos["mock-vid-0000"].status == expected_status
    assert repo.status_changes == ([(record.id, "failed")] if marks_failed else [])
    assert repo.reindex_results == []
    assert (record.id, "indexing") not in repo.status_changes
    assert next(iter(jobs.jobs.values())).status is JobStatus.FAILED


class _StateAwareVectorStore(FakeVectorStore):
    """Store double que confirma que frames y estado se publican juntos."""

    handles_video_state = True


class _IndexedStatusFailureRepo(FakeRepo):
    """Repo double que publica indexed y falla para probar rollback coordinado."""

    async def set_video_status(self, video_id: str, status: VideoStatus) -> bool:
        changed = await super().set_video_status(video_id, status)
        if status == "indexed":
            for external_id, record in self.videos.items():
                if record.id == video_id:
                    self.videos[external_id] = record.model_copy(update={"duration_ms": 999_999})
            raise RuntimeError("fallo posterior a publicar estado indexed")
        return changed


class _ReindexResultFailureRepo(FakeRepo):
    """Repo double que falla al persistir el outcome tras el commit del índice."""

    async def set_reindex_result(
        self,
        job_id: uuid.UUID,
        *,
        outcome: str,
        frames: int,
        reason: str | None,
    ) -> bool:
        raise RuntimeError("fallo al persistir resultado durable")


class _PostCommitFailureVectorStore(FakeVectorStore):
    """Store double que falla después de confirmar frames para probar la frontera."""

    handles_video_state = True

    async def replace_video_index(
        self, video_id: str, frames: Sequence[Any], *, duration_ms: int | None
    ) -> None:
        await super().replace_video_index(video_id, frames, duration_ms=duration_ms)
        raise RuntimeError("fallo posterior al commit de prueba")


def test_reindex_store_owned_state_does_not_call_repo_after_commit() -> None:
    """FR-010: un store que maneja estado no recibe una segunda transición."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(repo, adapter, "mock-vid-0000")
    jobs = FakeJobs()
    store = _StateAwareVectorStore()
    pipeline = CrawlerPipeline(repo=repo, jobs=jobs, adapter_for=lambda _job: adapter, store=store)
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=str(uuid.uuid4())),
            video_id=uuid.UUID(record.id),
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert repo.status_changes == []
    assert store.replacements


def test_reindex_result_failure_preserves_confirmed_atomic_replacement() -> None:
    """FR-010/SC-007: fallo del outcome no invalida un reemplazo ya confirmado."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = _ReindexResultFailureRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(repo, adapter, "mock-vid-0000", frame_count=0)
    jobs = FakeJobs()
    store = _StateAwareVectorStore()
    pipeline = CrawlerPipeline(repo=repo, jobs=jobs, adapter_for=lambda _job: adapter, store=store)
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=str(uuid.uuid4())),
            video_id=uuid.UUID(record.id),
            max_attempts=1,
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert next(iter(jobs.jobs.values())).status is JobStatus.FAILED
    assert store.frames
    assert store.replacements
    assert repo.videos["mock-vid-0000"].status == "indexed"
    assert repo.status_changes == []


def test_reindex_non_state_store_rolls_back_when_indexed_status_fails() -> None:
    """FR-010: falla de estado posterior al reemplazo restaura índice y estado."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = _IndexedStatusFailureRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(
        repo,
        adapter,
        "mock-vid-0000",
        status="failed",
        frame_count=3,
    )
    jobs = FakeJobs()
    store = FakeVectorStore()
    previous = [{"video_id": record.id, "frame_id": "old", "timestamp_ms": 1}]
    store.frames = list(previous)
    pipeline = CrawlerPipeline(repo=repo, jobs=jobs, adapter_for=lambda _job: adapter, store=store)
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=str(uuid.uuid4())),
            video_id=uuid.UUID(record.id),
            max_attempts=1,
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert store.frames == previous
    assert repo.videos["mock-vid-0000"].status == "failed"
    assert repo.videos["mock-vid-0000"].duration_ms == record.duration_ms
    assert next(iter(jobs.jobs.values())).status is JobStatus.FAILED


def test_reindex_post_commit_failure_does_not_mark_video_failed() -> None:
    """FR-010: un fallo reportado tras commit no deja estado failed con frames nuevos."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(repo, adapter, "mock-vid-0000", frame_count=3)
    jobs = FakeJobs()
    store = _PostCommitFailureVectorStore()
    pipeline = CrawlerPipeline(repo=repo, jobs=jobs, adapter_for=lambda _job: adapter, store=store)
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=str(uuid.uuid4())),
            video_id=uuid.UUID(record.id),
            max_attempts=1,
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert repo.videos["mock-vid-0000"].status == "indexed"
    assert store.frames
    assert repo.status_changes == []


def test_reindex_handler_skips_video_that_becomes_ineligible_after_enqueue() -> None:
    """SEC-005/FR-011: el handler falla cerrado si la fuente se deshabilita."""
    adapter = MockAdapter(seed=42, catalog_size=1)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(repo, adapter, "mock-vid-0000")
    jobs = FakeJobs()
    store = FakeVectorStore()
    pipeline = CrawlerPipeline(
        repo=repo,
        jobs=jobs,
        adapter_for=lambda _job: adapter,
        store=store,
    )
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)
    run_id = str(uuid.uuid4())

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.REINDEX,
            payload=_reindex_payload(run_id=run_id),
            video_id=uuid.UUID(record.id),
        )
        repo.sources["mock"] = repo.sources["mock"].model_copy(update={"enabled": False})
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert next(iter(jobs.jobs.values())).status is JobStatus.DONE
    assert store.replacements == []
    assert repo.videos["mock-vid-0000"].status == "indexed"
    job = next(iter(jobs.jobs.values()))
    assert repo.reindex_results == [(job.id, "skipped", 0, "source_disabled")]


def test_reindex_status_returns_aggregated_counts_and_results() -> None:
    """FR-011/SC-007: `reindex-status --run-id` emite el agregado estable."""
    run_id = str(uuid.uuid4())
    expected = {
        "run_id": run_id,
        "pending": 1,
        "completed": 2,
        "skipped": 1,
        "failed": 1,
        "frames": 9,
        "results": [
            {"external_id": "mock-vid-0000", "status": "completed", "frames": 4},
        ],
    }
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)], reindex_status_result=expected)
    result = _invoke(
        ["reindex-status", "--run-id", run_id],
        _base_context(repo=repo),
    )
    assert _stdout_json(result) == expected


def test_crawler_repo_reindex_status_uses_durable_results_and_fails_closed() -> None:
    """FR-011/SC-007: status usa payload durable y no el estado actual del vídeo."""
    run_id = str(uuid.uuid4())
    rows = [
        (str(uuid.uuid4()), "pending", None, None, None, None, "mock", "pending"),
        (str(uuid.uuid4()), "done", None, "completed", "3", None, "mock", "completed"),
        (
            str(uuid.uuid4()),
            "done",
            None,
            "skipped",
            "0",
            "source_disabled",
            "mock",
            "skipped",
        ),
        (str(uuid.uuid4()), "failed", "asset corrupto", None, None, None, "mock", "failed"),
        (str(uuid.uuid4()), "done", None, None, None, None, "mock", "legacy"),
        (str(uuid.uuid4()), "done", None, "completed", "-1", None, "mock", "invalid"),
    ]
    repo = _StaticCrawlerRepo(rows)

    result = asyncio.run(repo.reindex_status(run_id))

    assert result["pending"] == 1
    assert result["completed"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 3
    assert result["frames"] == 3
    by_external_id = {row["external_id"]: row for row in result["results"]}
    assert by_external_id["skipped"]["reason"] == "source_disabled"
    assert by_external_id["legacy"]["status"] == "failed"
    assert "durable" in by_external_id["legacy"]["error"]
    query, params = repo.cursor_double.executed[0]
    assert "payload ->> 'result_outcome'" in query
    assert "v.status" not in query
    assert params == (run_id,)


def test_crawler_repo_set_reindex_result_uses_parameterized_jsonb_update() -> None:
    """FR-011: el outcome durable se escribe parametrizado en jobs.payload."""
    repo = _StaticCrawlerRepo([])
    job_id = uuid.uuid4()

    updated = asyncio.run(
        repo.set_reindex_result(
            job_id,
            outcome="completed",
            frames=4,
            reason=None,
        )
    )

    assert updated is True
    query, params = repo.cursor_double.executed[0]
    assert "jsonb_build_object" in query
    assert params == ("completed", 4, None, job_id)


def test_reindex_cli_reports_reused_jobs_and_excludes_them_from_new_run() -> None:
    """FR-009/SC-003: jobs activos de otro run no cuentan como enqueued nuevos."""
    adapter = MockAdapter(seed=42, catalog_size=2)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    records = [
        _seed_reindex_video(repo, adapter, "mock-vid-0000"),
        _seed_reindex_video(repo, adapter, "mock-vid-0001", status="failed"),
    ]
    jobs = FakeJobs()
    old_run_id = str(uuid.uuid4())

    async def seed_active_jobs() -> None:
        for record in records:
            assert record.external_id is not None
            await jobs.enqueue(
                JobType.REINDEX,
                source_id=uuid.UUID(SOURCE_ID),
                video_id=uuid.UUID(record.id),
                payload=_reindex_payload(run_id=old_run_id, external_id=record.external_id),
                dedupe_key=reindex_dedupe_key(
                    "mock", record.external_id, _reindex_payload(run_id=old_run_id)["sampling"]
                ),
            )

    asyncio.run(seed_active_jobs())
    jobs.enqueued.clear()
    result = _invoke(
        ["reindex", "--source", "mock"],
        _base_context(repo=repo, jobs=jobs, registry=_registry_with_mock(catalog_size=2)),
    )

    data = _stdout_json(result)
    assert data["selected"] == 2
    assert data["enqueued"] == 0
    assert data["job_ids"] == []
    assert len(data["reused_job_ids"]) == 2
    assert data["reused_run_ids"] == [old_run_id]
    assert len(jobs.enqueued) == 0


def test_index_video_remains_legacy_when_pipeline_has_adaptive_policy() -> None:
    """Compatibilidad: solo REINDEX activa el perfil adaptativo explícito."""
    adapter = MockAdapter(seed=42, catalog_size=2)
    repo = FakeRepo(sources=[_mock_source_record(enabled=True)])
    record = _seed_reindex_video(repo, adapter, "mock-vid-0001", status="discovered")
    jobs = FakeJobs()
    store = FakeVectorStore()
    pipeline = CrawlerPipeline(
        repo=repo,
        jobs=jobs,
        adapter_for=lambda _job: adapter,
        store=store,
        sampling_policy=AdaptiveSamplingPolicy(),
    )
    worker = JobWorker(jobs, concurrency=1)
    pipeline.register_handlers(worker)

    async def scenario() -> int:
        await jobs.enqueue(
            JobType.INDEX_VIDEO,
            payload={"source": "mock", "external_id": "mock-vid-0001"},
            video_id=uuid.UUID(record.id),
        )
        return await worker.run_once()

    assert asyncio.run(scenario()) == 1
    assert len(store.frames) > 8
