"""Tests de integración del repo de fuentes/vídeos-web/stats (PR-028 · FR-012/013/014
· SEC-002 · SC-003 · DATA-001/003 · data-model.md) contra Supabase local.

Cubren `xtrace_crawler/repo.py` (`CrawlerRepo`, psycopg async):

- CRUD de `sources` (manifest jsonb + `enabled`, DATA-001; `enabled=false` por defecto
  = gate SEC-002).
- Upsert de vídeos web por `(source_id, external_id)` **idempotente** (SC-003) con
  unicidad parcial y convivencia con vídeos locales del spike (DATA-003).
- Transiciones de estado de vídeo incl. `unavailable`/`removed` (FR-012).
- `exclude` (FR-013, paridad de semántica con el spike).
- `stats` (FR-014): jobs por estado/fuente, vídeos por estado y errores recientes
  con causa.

Se **skippean** si la BD local no es alcanzable (p. ej. CI sin Supabase), patrón del
spike (`test_pgvector_store.py`): comprobación en recolección vía `pytestmark`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec
from xtrace_crawler.adapters.models import VideoSource
from xtrace_crawler.repo import CrawlerRepo, RateLimitStatsRecord, resolve_dsn


def _db_available() -> bool:
    """¿Supabase local alcanzable? (DSN por defecto/env, migración PR-025)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): integration repo saltada",
)


@pytest.fixture(autouse=True)
def _clean_tables() -> None:
    """Estado DB limpio por test (jobs→videos→sources; cascade alcanza frames)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate table public.jobs, public.videos, public.sources cascade")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del repo (sin pytest-asyncio, estilo PR-025/spike)."""
    return asyncio.run(coro)


def _repo() -> CrawlerRepo:
    return CrawlerRepo()


def _make_manifest(**overrides: object) -> AdapterManifest:
    """Manifest de compliance completo de una fuente (SEC-002 pasable)."""
    defaults: dict[str, object] = {
        "source": "fake",
        "access_method": "html",
        "assets_accessed": ["storyboard", "thumbnail"],
        "robots_reviewed": True,
        "terms_reviewed": True,
        "review_date": "2026-08-15",
        "rate_limit": RateLimitSpec(min_interval_ms=1_000, max_rps=2.0),
    }
    defaults.update(overrides)
    return AdapterManifest(**defaults)


def _make_video(source: str, external_id: str, **overrides: object) -> VideoSource:
    """VideoSource normalizado de la fuente; los tests sobrescriben campos opcionales."""
    defaults: dict[str, object] = {
        "source": source,
        "external_id": external_id,
        "title": f"Vídeo {external_id}",
        "page_url": f"https://example.com/{source}/videos/{external_id}",
    }
    defaults.update(overrides)
    return VideoSource(**defaults)


def _upsert_source(name: str = "fuente-a", *, enabled: bool = False) -> Any:
    """Helper: crea una fuente y devuelve su `SourceRecord`."""
    manifest = _make_manifest(source=name)
    return _run(_repo().upsert_source(name=name, adapter=name, manifest=manifest, enabled=enabled))


def _upsert_video(source: Any, external_id: str, **overrides: object) -> Any:
    """Helper: crea un vídeo web de la fuente y devuelve su `VideoRecord`."""
    video = _make_video(source.name, external_id, **overrides)
    return _run(_repo().upsert_web_video(source.id, video))


def _insert_local_video(local_ref: str) -> None:
    """Helper: fila local del spike (source_id NULL, DATA-003) vía SQL directo."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.videos (local_ref, status) values (%s, 'indexed')",
                (local_ref,),
            )


def _insert_job(
    *,
    status: str,
    source_id: str | None = None,
    video_id: str | None = None,
    error: str | None = None,
    job_type: str = "FETCH_METADATA",
) -> None:
    """Helper: siembra una fila `jobs` (setup de `stats`, FR-014)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.jobs (job_type, status, source_id, video_id, error) "
                "values (%s, %s, %s, %s, %s)",
                (job_type, status, source_id, video_id, error),
            )


def _count(table: str) -> int:
    """Helper: nº de filas de una tabla pública."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select count(*) from public.{table}")
            return int(cur.fetchone()[0])  # type: ignore[index]


# ---------------------------------------------------------------------------
# sources CRUD (DATA-001 · SEC-002)
# ---------------------------------------------------------------------------


def test_upsert_source_creates_and_roundtrips_manifest() -> None:
    """`upsert_source` persiste manifest jsonb + `enabled` y `get_source` los recupera
    (DATA-001; `enabled` default false = gate SEC-002)."""
    source = _upsert_source("xvideos")
    assert source.enabled is False
    assert source.adapter == "xvideos"

    fetched = _run(_repo().get_source("xvideos"))
    assert fetched is not None
    assert fetched.id == source.id
    assert fetched.name == "xvideos"
    assert fetched.manifest.source == "xvideos"
    assert fetched.manifest.robots_reviewed is True
    assert fetched.manifest.terms_reviewed is True
    assert fetched.manifest.review_date == "2026-08-15"
    assert fetched.manifest.rate_limit.min_interval_ms == 1_000
    assert fetched.manifest.rate_limit.max_rps == 2.0
    assert fetched.enabled is False


def test_upsert_source_idempotent_and_updates() -> None:
    """Repetir `upsert_source` sobre el mismo nombre actualiza y no duplica (DATA-001)."""
    first = _upsert_source("fuente-a")
    second = _upsert_source("fuente-a", enabled=True)
    assert second.id == first.id
    assert second.enabled is True
    assert _count("sources") == 1


def test_list_sources_returns_all() -> None:
    """`list_sources` devuelve todas las fuentes registradas (DATA-001)."""
    _upsert_source("fuente-a")
    _upsert_source("fuente-b")
    names = {source.name for source in _run(_repo().list_sources())}
    assert names == {"fuente-a", "fuente-b"}


def test_set_source_enabled_changes_flag() -> None:
    """`set_source_enabled` habilita/deshabilita y reporta si hubo cambio (SEC-002)."""
    _upsert_source("fuente-a")
    repo = _repo()
    assert _run(repo.set_source_enabled("fuente-a", True)) is True
    assert _run(repo.get_source("fuente-a")).enabled is True  # type: ignore[union-attr]
    assert _run(repo.set_source_enabled("fuente-a", True)) is False  # sin cambio
    assert _run(repo.set_source_enabled("fuente-a", False)) is True
    assert _run(repo.get_source("fuente-a")).enabled is False  # type: ignore[union-attr]


def test_delete_source_removes_row() -> None:
    """`delete_source` borra la fuente; borrar dos veces devuelve False (CRUD)."""
    source = _upsert_source("fuente-a")
    repo = _repo()
    assert _run(repo.delete_source("fuente-a")) is True
    assert _run(repo.get_source("fuente-a")) is None
    assert _run(repo.delete_source("fuente-a")) is False
    assert _run(repo.get_source("fuente-b")) is None  # inexistente
    assert source.id  # el helper devolvió un registro con id


# ---------------------------------------------------------------------------
# vídeos web: upsert idempotente (SC-003) y unicidad (FR-012 / DATA-001)
# ---------------------------------------------------------------------------


def test_upsert_web_video_creates_normalized_row() -> None:
    """El upsert crea el vídeo con estado `discovered` y todos los campos web (FR-012)."""
    source = _upsert_source("fuente-a")
    published = datetime(2026, 8, 15, tzinfo=UTC)
    record = _upsert_video(
        source,
        "vid-1",
        duration_ms=125_000,
        thumbnail_url="https://example.com/t.jpg",
        preview_url="https://example.com/p.mp4",
        storyboard_urls=["https://example.com/sb1.jpg"],
        tags=["tag-a", "tag-b"],
        published_at=published,
    )
    assert record.source_id == source.id
    assert record.external_id == "vid-1"
    assert record.status == "discovered"
    assert record.excluded is False
    assert record.frame_count == 0
    assert record.title == "Vídeo vid-1"
    assert record.page_url == "https://example.com/fuente-a/videos/vid-1"
    assert record.duration_ms == 125_000
    assert record.thumbnail_url == "https://example.com/t.jpg"
    assert record.preview_url == "https://example.com/p.mp4"
    assert record.storyboard_urls == ["https://example.com/sb1.jpg"]
    assert record.tags == ["tag-a", "tag-b"]
    assert record.published_at == published
    assert record.local_ref.startswith("web:")
    assert _count("videos") == 1


def test_upsert_web_video_idempotent_sc003() -> None:
    """Repetir el upsert sobre `(source_id, external_id)` NO duplica: misma fila, misma
    id (SC-003: INCREMENTAL sin duplicados)."""
    source = _upsert_source("fuente-a")
    first = _upsert_video(source, "vid-1")
    second = _upsert_video(source, "vid-1", title="Título actualizado")
    assert second.id == first.id
    assert _count("videos") == 1
    # El conflicto actualiza metadatos pero NO el estado ni la exclusión (decisión PR-028).
    assert second.title == "Título actualizado"
    assert second.status == "discovered"


def test_web_video_uniqueness_scoped_per_source() -> None:
    """La unicidad `(source_id, external_id)` es POR fuente: el mismo external_id en
    fuentes distintas son vídeos distintos (FR-012 · DATA-001)."""
    source_a = _upsert_source("fuente-a")
    source_b = _upsert_source("fuente-b")
    va = _upsert_video(source_a, "vid-1")
    vb = _upsert_video(source_b, "vid-1")
    vc = _upsert_video(source_a, "vid-2")
    assert va.id != vb.id
    assert va.id != vc.id
    assert _count("videos") == 3


def test_web_and_local_videos_coexist_data003() -> None:
    """Los vídeos locales del spike (source_id NULL, local_ref único) y los web
    conviven sin colisión de unicidad (DATA-003)."""
    source = _upsert_source("fuente-a")
    _insert_local_video("dataset/local-1")
    record = _upsert_video(source, "vid-1")
    assert _count("videos") == 2
    assert record.external_id == "vid-1"
    assert record.local_ref != "dataset/local-1"


def test_get_web_video_by_source_and_external_id() -> None:
    """`get_web_video(fuente, external_id)` localiza el vídeo; ausente → None (FR-012)."""
    source = _upsert_source("fuente-a")
    created = _upsert_video(source, "vid-1")
    fetched = _run(_repo().get_web_video("fuente-a", "vid-1"))
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.status == "discovered"
    assert _run(_repo().get_web_video("fuente-a", "vid-2")) is None
    assert _run(_repo().get_web_video("fuente-b", "vid-1")) is None


# ---------------------------------------------------------------------------
# Transiciones de estado (FR-012) y exclusión (FR-013)
# ---------------------------------------------------------------------------


def test_set_video_status_transitions_fr012() -> None:
    """El estado del vídeo transita por el ciclo completo FR-012, incluidos
    `unavailable` y `removed`; cada cambio devuelve True."""
    source = _upsert_source("fuente-a")
    video = _upsert_video(source, "vid-1")
    repo = _repo()
    for status in (
        "pending",
        "indexing",
        "indexed",
        "failed",
        "unavailable",
        "removed",
        "discovered",
    ):
        assert _run(repo.set_video_status(video.id, status)) is True  # type: ignore[arg-type]
        fetched = _run(repo.get_web_video("fuente-a", "vid-1"))
        assert fetched.status == status  # type: ignore[union-attr]


def test_set_video_status_noop_when_unchanged() -> None:
    """Fijar el mismo estado no es un cambio: devuelve False (FR-012, semántica spike)."""
    source = _upsert_source("fuente-a")
    video = _upsert_video(source, "vid-1")
    repo = _repo()
    assert _run(repo.set_video_status(video.id, "indexed")) is True
    assert _run(repo.set_video_status(video.id, "indexed")) is False


def test_set_video_status_rejects_invalid_status() -> None:
    """Un estado fuera del CHECK de FR-012 se rechaza en Python (error claro)."""
    source = _upsert_source("fuente-a")
    video = _upsert_video(source, "vid-1")
    with pytest.raises(ValueError):
        _run(_repo().set_video_status(video.id, "deleted"))  # type: ignore[arg-type]


def test_exclude_fr013() -> None:
    """`exclude` marca/desmarca el flag del spike: True solo si hubo cambio (FR-013)."""
    source = _upsert_source("fuente-a")
    video = _upsert_video(source, "vid-1")
    repo = _repo()
    assert _run(repo.exclude(video.id)) is True
    assert _run(repo.get_web_video("fuente-a", "vid-1")).excluded is True  # type: ignore[union-attr]
    assert _run(repo.exclude(video.id)) is False  # ya excluido
    assert _run(repo.exclude(video.id, excluded=False)) is True
    assert _run(repo.get_web_video("fuente-a", "vid-1")).excluded is False  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# stats (FR-014)
# ---------------------------------------------------------------------------


def test_stats_jobs_by_status_and_source_fr014() -> None:
    """`stats` cuenta jobs por estado y por fuente (incluidos sin fuente) (FR-014)."""
    source_a = _upsert_source("fuente-a")
    source_b = _upsert_source("fuente-b")
    _insert_job(status="done", source_id=source_a.id)
    _insert_job(status="done", source_id=source_a.id)
    _insert_job(status="failed", source_id=source_a.id, error="HTTP 429 rate limit")
    _insert_job(status="pending", source_id=source_b.id)
    _insert_job(status="pending")  # sin fuente (p. ej. DISCOVER global)

    stats = _run(_repo().stats())
    assert stats.jobs_by_status == {"done": 2, "failed": 1, "pending": 2}
    assert stats.jobs_by_source == {"fuente-a": 3, "fuente-b": 1, None: 1}
    # El único job fallido tiene causa: aparece en los errores recientes (FR-014).
    assert len(stats.recent_errors) == 1
    assert stats.recent_errors[0].job_type == "FETCH_METADATA"
    assert stats.recent_errors[0].error == "HTTP 429 rate limit"


def test_stats_videos_by_status_fr014() -> None:
    """`stats` cuenta vídeos por estado: descubiertos/indexados/fallidos/… (FR-014)."""
    source = _upsert_source("fuente-a")
    repo = _repo()
    discovered = _upsert_video(source, "vid-1")
    indexed = _upsert_video(source, "vid-2")
    failed = _upsert_video(source, "vid-3")
    removed = _upsert_video(source, "vid-4")
    _run(repo.set_video_status(indexed.id, "indexed"))
    _run(repo.set_video_status(failed.id, "failed"))
    _run(repo.set_video_status(removed.id, "removed"))

    stats = _run(repo.stats())
    assert stats.videos_by_status == {
        "discovered": 1,
        "indexed": 1,
        "failed": 1,
        "removed": 1,
    }
    assert discovered.id  # helper usado


def test_stats_recent_errors_with_cause_fr014() -> None:
    """`stats.recent_errors` expone los errores recientes (failed/unavailable) con su
    causa, fuente y tipo de job (FR-014)."""
    source = _upsert_source("fuente-a")
    video = _upsert_video(source, "vid-1")
    _insert_job(
        status="failed",
        source_id=source.id,
        video_id=video.id,
        error="HTTP 500",
        job_type="FETCH_METADATA",
    )
    _insert_job(
        status="unavailable",
        source_id=source.id,
        error="404 removed",
        job_type="CHECK_AVAILABILITY",
    )
    _insert_job(status="done", source_id=source.id)  # no es error

    stats = _run(_repo().stats())
    errors = stats.recent_errors
    assert len(errors) == 2
    by_type = {error.job_type: error for error in errors}
    assert by_type["FETCH_METADATA"].error == "HTTP 500"
    assert by_type["FETCH_METADATA"].source == "fuente-a"
    assert by_type["FETCH_METADATA"].video_id == video.id
    assert by_type["CHECK_AVAILABILITY"].error == "404 removed"


def test_stats_empty_database() -> None:
    """`stats` sobre BD vacía devuelve contadores vacíos y sin errores (FR-014)."""
    stats = _run(_repo().stats())
    assert stats.jobs_by_status == {}
    assert stats.jobs_by_source == {}
    assert stats.videos_by_status == {}
    assert stats.recent_errors == []


def test_stats_embeds_rate_limits_section() -> None:
    """PR-035 · SC-005/NFR-004: `stats(rate_limits=...)` incrusta la contabilidad del limiter.

    El pipeline agrega las métricas del `RateLimiter` por fuente y `repo.stats`
    las expone como sección `rate_limits` (nueva, requests/rate_limit_waits/
    total_wait_ms) sin tocar las secciones existentes de FR-014 (compatibilidad
    de JSON).
    """
    source = _upsert_source("fuente-a")
    _insert_job(status="done", source_id=source.id)
    record = RateLimitStatsRecord(requests=7, rate_limit_waits=6, total_wait_ms=300)
    stats = _run(_repo().stats(rate_limits={"fuente-a": record}))
    assert stats.rate_limits == {"fuente-a": record}
    assert stats.jobs_by_status == {"done": 1}  # secciones previas intactas (FR-014)
    assert stats.jobs_by_source == {"fuente-a": 1}
    assert stats.recent_errors == []
    # Sin `rate_limits` → sección vacía (llamadas previas intactas, FR-014).
    assert _run(_repo().stats()).rate_limits == {}
