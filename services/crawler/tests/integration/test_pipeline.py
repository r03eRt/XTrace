"""Tests de integración del pipeline crawler → índice (PR-030 · FR-007/010/011/013/015
· SC-002/003/004/005 · ADR-0011 · contracts §6) contra Supabase local.

Cubren los handlers concretos de `xtrace_crawler/pipeline.py` con el MockAdapter
(PR-021 · FR-003) y el índice real del spike (`PgVectorStore`, ADR-0011):

- **SC-002**: un BACKFILL acotado produce vídeos `indexed`, frames con timestamp
  y embeddings **consultables** vía el VectorStore del spike (pHash real
  persistido, FIX-phash · FR-011).
- **SC-003**: una segunda ejecución INCREMENTAL no duplica vídeos, jobs ni frames.
- **SC-004**: un fallo transitorio agotado termina `failed` (sin temporales);
  un fallo terminal (`removed`) termina `unavailable` sin reintentos; ningún job
  queda colgado y no quedan artefactos temporales (`xtrace-crawler-asset-*`,
  FR-015).
- **FR-009/SC-005**: cada llamada al adapter pasa por el rate limiter de la
  fuente (limiter con reloj/sleeper fakes: nº de requests y esperas exactos).
- **PR-036 · cota global `--max_videos`** (analyze hallazgo 2 · SC-002): el
  payload del DISCOVER lleva `max_videos` y el pipeline corta la cadena de
  paginación al alcanzarla, acumulando vídeos conocidos y nuevos
  (`videos_counted`), sin perder trazabilidad.
- **PR-045 (3a validación real)**: durante DISCOVER el pipeline reenvía a
  `get_video` el `page_url` del listado (`DiscoverPage.page_urls`, href
  completo con slug) — la URL canónica que la fuente exige (404 sin slug).
- **PR-046 (4a validación real)**: FETCH_METADATA ya no reconstruye la URL a
  ciegas — lee `videos.page_url` persistido por DISCOVER y lo reenvía a
  `get_video(..., page_url=...)`; sin fila previa (o con `page_url` vacío)
  sigue llamando sin él (retrocompatible).
- **PR-049 (discover acotado por sección)**: el pipeline reenvía `section`
  del payload a `adapter.discover` en CADA página y lo propaga al siguiente
  DISCOVER (la cadena sigue acotada a la sección; el mock la acepta y la
  ignora — retrocompatible).
- **FR-013**: CHECK_AVAILABILITY `unavailable`/`removed` → estado del vídeo +
  exclusión del índice (frames eliminados y ocultos en `ann_search`).
- **Nota revisión Ola B** (tasks.md PR-030): `sync_source` conserva
  `sources.enabled` al refrescar el manifest (no revoca una habilitación humana
  previa).

Sin red real (NFR-003): los assets se sirven con `httpx.MockTransport` desde
imágenes sintéticas (PR-029) y previews generados con FFmpeg si está disponible
(si no, el preview degrada y los conteos se ajustan). Se **skippean** si la BD
local no es alcanzable (patrón del spike); cada test limpia
`jobs`/`videos`/`sources` al inicio (constitución §6).

**PR-034 (hallazgo del quickstart, PR-033)**: con el cableado REAL (worker +
repos + pipeline + `MockAdapter`, sin transporte HTTP inyectado), los jobs
`INDEX_VIDEO` degradaban TODOS los assets (`http://mock.local/...` bloqueado por
el `SafeHTTPClient` real) → 0 vídeos indexados. El mock ahora sirve sus assets
**in-process** (`fetch_asset_bytes`, imágenes sintéticas deterministas) y el
pipeline las prefiere: `test_real_wiring_mock_indexes_videos_without_network`
reproduce el cableado real SIN fakes de fetch y verifica vídeos `indexed` con
frames+embeddings consultables sin red (FR-003 · SC-001/SC-002).
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from PIL import Image
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.hashing.phash import compute_phash
from xtrace_spike.vectorstore.pgvector import EMBEDDING_DIMENSION, PgVectorStore, phash_to_db

from tests.fixtures.assets.preview_factory import ffmpeg_available, make_preview_mp4
from tests.fixtures.harness import MockHarness
from xtrace_crawler.adapters.base import RateLimitSpec
from xtrace_crawler.adapters.mock import (
    MOCK_BASE_URL,
    MockAdapter,
    MockAdapterRemovedError,
    MockAdapterTransientError,
    MockFaults,
    synthetic_asset_bytes,
)
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.crawling.http import SafeHTTPClient
from xtrace_crawler.crawling.ratelimit import RateLimiter
from xtrace_crawler.jobs.repo import JobsRepo
from xtrace_crawler.jobs.types import JobStatus, JobType
from xtrace_crawler.jobs.worker import JobWorker
from xtrace_crawler.pipeline import CrawlerPipeline
from xtrace_crawler.repo import CrawlerRepo, RateLimitStatsRecord, resolve_dsn

#: Duración de los previews sintéticos (FFmpeg lavfi, tests sin red).
_PREVIEW_DURATION_S = 2.0
#: Tiles de storyboard del mock (paridad con `_STORYBOARD_TILES`, PR-021).
_MOCK_STORYBOARD_TILES = 6
#: Thumbnails del mock (paridad con `_THUMBNAILS`, PR-021).
_MOCK_THUMBNAILS = 3


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
    "integration pipeline saltada",
)


@pytest.fixture(autouse=True)
def _clean_tables() -> None:
    """Estado DB limpio por test (jobs→videos→sources; cascade alcanza frames)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate table public.jobs, public.videos, public.sources cascade")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del pipeline (sin pytest-asyncio, patrón del repo)."""
    return asyncio.run(coro)


def _fast_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overrides D5: el limiter por defecto del mock no duerme en tests (SC-005 se
    valida con limiter inyectado en `test_backfill_respects_rate_limiter`)."""
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_MOCK_MAX_RPS", "1000")


# ---------------------------------------------------------------------------
# Assets sintéticos sin red (NFR-003 · SEC-004): imágenes PIL → bytes JPEG/MP4
# ---------------------------------------------------------------------------


def _jpeg_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _served_storyboard(external_id: str) -> Image.Image:
    """La imagen EXACTA que el MockAdapter sirve in-process (PR-034): JPEG decodificado.

    El proveedor fake hashea los bytes de píxeles (no es perceptual): la consulta
    SC-002 debe usar la imagen tal y como se indexó (con los artefactos JPEG),
    no la imagen original sin codificar. Los bytes vienen de
    `synthetic_asset_bytes` (adapters/mock.py), la fuente ÚNICA de las imágenes
    del catálogo — con o sin transporte HTTP, el pipeline indexa lo mismo.
    """
    data = synthetic_asset_bytes(f"{MOCK_BASE_URL}/assets/{external_id}/storyboard.jpg")
    assert data is not None
    return Image.open(io.BytesIO(data)).convert("RGB")


def _preview_bytes(url: str) -> bytes:
    """Preview mp4 sintético (FFmpeg lavfi, PR-029); b"" si ffmpeg no está (degradará)."""
    if not ffmpeg_available():
        return b""
    tmp = Path(tempfile.gettempdir()) / f"xtrace-test-preview-{abs(zlib.crc32(url.encode()))}.mp4"
    try:
        make_preview_mp4(tmp, duration_s=_PREVIEW_DURATION_S)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _asset_bytes(url: str) -> bytes:
    """Bytes del asset: las imágenes del MockAdapter (PR-034) o el preview FFmpeg."""
    in_process = synthetic_asset_bytes(url)
    if in_process is not None:
        return in_process
    if url.endswith("/preview.mp4"):
        return _preview_bytes(url)
    raise AssertionError(f"URL de asset no contemplada por el router de tests: {url}")


def _asset_router() -> Callable[[httpx.Request], httpx.Response]:
    """Router de MockTransport: sirve los assets del mock desde bytes sintéticos."""
    cache: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in cache:
            cache[url] = _asset_bytes(url)
        return httpx.Response(200, content=cache[url], request=request)

    return handler


def _client() -> SafeHTTPClient:
    """Cliente seguro del mock: `mock.local` (http) en allowlist + transporte fake."""
    return SafeHTTPClient(
        allowed_hosts={"mock.local"},
        allow_http=True,
        transport=httpx.MockTransport(_asset_router()),
    )


# ---------------------------------------------------------------------------
# Helpers de escenario (harness PR-021 + worker PR-027 + pipeline PR-030)
# ---------------------------------------------------------------------------


def _build(
    adapter: Any,
    *,
    repo: CrawlerRepo | None = None,
    jobs: JobsRepo | None = None,
    client: SafeHTTPClient | None = None,
    limiter_factory: Callable[[Any], RateLimiter] | None = None,
    worker_id: str = "it-pipeline",
) -> tuple[CrawlerPipeline, JobWorker, JobsRepo]:
    """Pipeline + worker con handlers registrados sobre la BD real."""
    jobs = jobs if jobs is not None else JobsRepo()
    pipeline = CrawlerPipeline(
        repo=repo if repo is not None else CrawlerRepo(),
        jobs=jobs,
        adapter_for=lambda _job: adapter,
        client=client,
        limiter_factory=limiter_factory,
    )
    worker = JobWorker(jobs, concurrency=2, worker_id=worker_id)
    pipeline.register_handlers(worker)
    return pipeline, worker, jobs


def _run_backfill(
    pipeline: CrawlerPipeline,
    worker: JobWorker,
    jobs: JobsRepo,
    *,
    limit: int = 2,
    mode: str = "backfill",
) -> int:
    """Encola el DISCOVER inicial y procesa la cola hasta agotarla (una pasada)."""
    return _run(_backfill_scenario(pipeline, worker, jobs, limit=limit, mode=mode))


async def _backfill_scenario(
    pipeline: CrawlerPipeline,
    worker: JobWorker,
    jobs: JobsRepo,
    *,
    limit: int,
    mode: str,
) -> int:
    await jobs.enqueue(
        JobType.DISCOVER,
        payload={"source": "mock", "cursor": None, "limit": limit, "mode": mode},
    )
    return await worker.run_once()


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Filas de la BD (lectura directa, sin pasar por el repo)."""
    with psycopg.connect(resolve_dsn(), row_factory=psycopg.rows.dict_row, autocommit=True) as conn:
        return list(conn.execute(sql, params).fetchall())


def _videos() -> list[dict[str, Any]]:
    return _rows("select * from public.videos order by external_id")


def _jobs() -> list[dict[str, Any]]:
    return _rows("select * from public.jobs order by created_at, id")


def _frames_for(video_id: str) -> int:
    row = _rows("select count(*) as n from public.frames where video_id = %s", (video_id,))[0]
    return int(row["n"])


def _total_frames() -> int:
    return int(_rows("select count(*) as n from public.frames")[0]["n"])


def _leftover_asset_dirs() -> list[Path]:
    return [
        p
        for p in Path(tempfile.gettempdir()).iterdir()
        if p.is_dir() and p.name.startswith("xtrace-crawler-asset-")
    ]


def _expected_frames(*, storyboard: int, thumbnails: int) -> set[int]:
    """Conteos de frames posibles para el catálogo del mock.

    thumbnails + tiles de storyboard + 2-3 frames del preview (si hay FFmpeg),
    menos 1 si hay storyboard: el primer frame del preview (t=0) comparte clave
    `(video_id, frame_seq=0)` con el tile 0 del storyboard — el upsert del
    VectorStore del spike reemplaza, no duplica (idempotencia SC-003).
    """
    base = storyboard + thumbnails
    if not ffmpeg_available():
        return {base}
    collision = 1 if storyboard else 0
    return {base + 2 - collision, base + 3 - collision}


# ---------------------------------------------------------------------------
# SC-002 · Flujo completo BACKFILL → índice consultable
# ---------------------------------------------------------------------------


def test_backfill_full_flow_indexes_frames_and_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-002: BACKFILL acotado → vídeos `indexed`, frames y embeddings consultables.

    El flujo completo (DISCOVER ×2 → FETCH_METADATA ×3 → INDEX_VIDEO ×3) se
    procesa en una pasada del worker; los frames del mock (storyboard con
    timestamp, thumbnails sin timestamp, preview corto) acaban en el índice del
    spike con pHash real (FIX-phash) y son consultables por `ann_search`.
    """
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        harness.adapter, client=_client(), worker_id="it-pipeline-sc002"
    )
    assert _run_backfill(pipeline, worker, jobs, limit=2) == 8  # 2 DISCOVER + 3 + 3

    videos = _videos()
    assert len(videos) == 3
    assert all(video["status"] == "indexed" for video in videos)

    jobs_rows = _jobs()
    assert len(jobs_rows) == 8
    assert all(job["status"] == JobStatus.DONE.value for job in jobs_rows)  # nada colgado

    by_external_id = {video["external_id"]: video for video in videos}
    # mock-vid-0000 (índice 0 del catálogo) no tiene storyboard (degradación, PR-021).
    assert _frames_for(by_external_id["mock-vid-0000"]["id"]) in _expected_frames(
        storyboard=0, thumbnails=_MOCK_THUMBNAILS
    )
    for external_id in ("mock-vid-0001", "mock-vid-0002"):
        assert _frames_for(by_external_id[external_id]["id"]) in _expected_frames(
            storyboard=_MOCK_STORYBOARD_TILES, thumbnails=_MOCK_THUMBNAILS
        )

    # Embeddings CONSULTABLES vía el VectorStore del spike (SC-002 · ADR-0011):
    # el embedding del sprite storyboard de 0001 devuelve sus frames como top hit.
    store = PgVectorStore()
    provider = FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION)
    query = [
        float(value) for value in provider.embed_images([_served_storyboard("mock-vid-0001")])[0]
    ]
    hits = _run(store.ann_search(query, k=5))
    # `ann_search` devuelve video_id como texto; la fila de BD como UUID (psycopg).
    assert str(hits[0]["video_id"]) == str(by_external_id["mock-vid-0001"]["id"])
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)

    # pHash real persistido en frames.phash (FIX-phash · FR-011): el del sprite.
    expected_phash_db = phash_to_db(compute_phash(_served_storyboard("mock-vid-0001")))
    phash_rows = _rows(
        "select 1 from public.frames where video_id = %s and phash = %s limit 1",
        (by_external_id["mock-vid-0001"]["id"], expected_phash_db),
    )
    assert phash_rows, "el pHash real del sprite storyboard debe estar en frames.phash"

    assert _leftover_asset_dirs() == []  # SC-004: sin temporales tras el flujo


# ---------------------------------------------------------------------------
# PR-045 · DISCOVER reenvía `page_url` (href del listado) a get_video
# ---------------------------------------------------------------------------


class _PageUrlsRecordingAdapter(MockAdapter):
    """MockAdapter que (1) rellena `page_urls` en discover (PR-045) y (2) registra
    cada llamada a `get_video` con su `page_url` (test-only, sin red)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.get_video_calls: list[tuple[str, str | None]] = []

    async def discover(
        self, *, cursor: str | None, limit: int, section: str | None = None
    ) -> DiscoverPage:
        page = await super().discover(cursor=cursor, limit=limit, section=section)
        return page.model_copy(
            update={"page_urls": {vid: f"/videos/{vid}/1/2/slug" for vid in page.external_ids}}
        )

    async def get_video(
        self, external_id: str, *, page_url: str | None = None
    ) -> VideoSource | None:
        self.get_video_calls.append((external_id, page_url))
        return await super().get_video(external_id, page_url=page_url)


def test_discover_pasa_page_urls_a_get_video(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-045 + PR-046: DISCOVER pasa el href del listado; FETCH_METADATA el persistido.

    Hallazgo de la 3a validación real (2026-08-16): la URL canónica del vídeo
    exige el slug del listado (reconstruir `/video.<id>/` sin slug → 404). El
    pipeline reenvía `page.page_urls[external_id]` en las llamadas de DISCOVER.
    **PR-046 (4a validación real)**: las de FETCH_METADATA ya no van a ciegas —
    leen `videos.page_url` persistido por DISCOVER y lo reenvían (el mock lo
    persiste como `MOCK_BASE_URL/videos/<id>`). El flujo no cambia: mismo nº
    de jobs y vídeos que el BACKFILL estándar.
    """
    _fast_rate(monkeypatch)
    adapter = _PageUrlsRecordingAdapter(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(adapter, client=_client(), worker_id="it-pipeline-pr045")
    assert _run_backfill(pipeline, worker, jobs, limit=2) == 8  # 2 DISCOVER + 3 + 3

    calls = adapter.get_video_calls
    assert len(calls) == 6  # 3 del DISCOVER + 3 de FETCH_METADATA
    assert all(page_url is not None for _, page_url in calls)  # PR-046: ninguna a ciegas
    listing = [call for call in calls if call[1] is not None and call[1].endswith("/slug")]
    persisted = [call for call in calls if call[1] is not None and not call[1].endswith("/slug")]
    assert len(listing) == 3  # las del DISCOVER llevan el href del listado
    assert len(persisted) == 3  # las de FETCH_METADATA llevan el page_url persistido
    for external_id, page_url in listing:
        assert page_url == f"/videos/{external_id}/1/2/slug"  # = page.page_urls[external_id]
    for external_id, page_url in persisted:
        assert page_url == f"{MOCK_BASE_URL}/videos/{external_id}"  # = videos.page_url

    assert len(_videos()) == 3  # el flujo no cambia (SC-003: sin duplicados)
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# PR-046 · FETCH_METADATA reenvía el page_url persistido (o no, retrocompatible)
# ---------------------------------------------------------------------------


def _insert_video_row(external_id: str, *, page_url: str | None = None) -> None:
    """Fila `videos` previa (estado `discovered`) con `page_url` opcional."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.videos (source_id, external_id, local_ref, status, page_url) "
                "select id, %s, 'web:' || id::text || ':' || %s, 'discovered', %s "
                "from public.sources where name = 'mock'",
                (external_id, external_id, page_url),
            )


def test_fetch_metadata_reenvia_page_url_persistido_o_retrocompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-046: FETCH_METADATA usa `videos.page_url` persistido si existe; si no, retrocompatible.

    Hallazgo de la 4a validación real (2026-08-16): FETCH_METADATA reconstruía
    `/video.<id>/` sin slug → 404 → falso `unavailable` terminal. Ahora el
    handler lee la fila por `(source, external_id)` y reenvía su `page_url` a
    `get_video` (caso 1: fila previa CON page_url). Sin fila previa (caso 2) o
    con `page_url` NULL en BD (caso 3) llama como hasta ahora, sin `page_url`
    (retrocompatible) y el flujo no cambia (INDEX_VIDEO se encola).
    """
    _fast_rate(monkeypatch)
    adapter = _PageUrlsRecordingAdapter(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(adapter, client=_client(), worker_id="it-pipeline-pr046")
    # Fuente registrada con su manifest real, sin DISCOVER previo (el CLI encola
    # FETCH_METADATA directo, PR-032).
    _run(pipeline.sync_source(adapter))

    async def _enqueue_fetch(external_id: str) -> None:
        await jobs.enqueue(
            JobType.FETCH_METADATA,
            payload={"source": "mock", "external_id": external_id},
        )
        assert await worker.run_once() == 2  # FETCH_METADATA + INDEX_VIDEO

    # Caso 1: fila previa CON page_url (href con slug) → get_video lo recibe tal cual.
    persisted_url = "https://fuente.test/videos/mock-vid-0000/slug-del-titulo"
    _insert_video_row("mock-vid-0000", page_url=persisted_url)
    _run(_enqueue_fetch("mock-vid-0000"))
    assert adapter.get_video_calls[-1] == ("mock-vid-0000", persisted_url)

    # Caso 2: SIN fila previa → get_video sin page_url (la fuente reconstruye, retrocompatible).
    _run(_enqueue_fetch("mock-vid-0001"))
    assert adapter.get_video_calls[-1] == ("mock-vid-0001", None)

    # Caso 3: fila previa con page_url NULL → get_video sin page_url (retrocompatible).
    _insert_video_row("mock-vid-0002", page_url=None)
    _run(_enqueue_fetch("mock-vid-0002"))
    assert adapter.get_video_calls[-1] == ("mock-vid-0002", None)

    # El flujo no cambia: los 3 vídeos se actualizaron (nunca `unavailable`) y
    # sus INDEX_VIDEO se completaron (nada colgado).
    videos = {video["external_id"]: video for video in _videos()}
    assert set(videos) == {"mock-vid-0000", "mock-vid-0001", "mock-vid-0002"}
    assert all(video["status"] == "indexed" for video in videos.values())
    assert all(job["status"] == JobStatus.DONE.value for job in _jobs())
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# PR-049 · Discover acotado por sección (FR-007 · pruebas del operador)
# ---------------------------------------------------------------------------


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


def test_discover_propaga_section_al_adapter_y_a_la_siguiente_pagina(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-049: `section` del payload llega a `discover` y al siguiente DISCOVER.

    El pipeline reenvía la sección al adapter en CADA página de la cadena y el
    siguiente DISCOVER (cursor) la conserva en su payload: el discover queda
    acotado a la sección de principio a fin. El mock la ignora
    (retrocompatible) y el flujo completa igual (mismo nº de jobs y vídeos
    que el BACKFILL estándar, sin duplicados).
    """
    _fast_rate(monkeypatch)
    adapter = _SectionRecordingAdapter(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(adapter, client=_client(), worker_id="it-pipeline-pr049")

    async def _scenario() -> int:
        await jobs.enqueue(
            JobType.DISCOVER,
            payload={
                "source": "mock",
                "cursor": None,
                "limit": 2,
                "mode": "backfill",
                "section": "/tags/xxx",
            },
        )
        return await worker.run_once()

    assert _run(_scenario()) == 8  # 2 DISCOVER + 3 FETCH_METADATA + 3 INDEX_VIDEO

    # Cada página recibió la sección (primera: cursor None; segunda: cursor "2").
    assert adapter.discover_calls == [(None, "/tags/xxx"), ("2", "/tags/xxx")]

    discovers = [job for job in _jobs() if job["job_type"] == JobType.DISCOVER.value]
    assert len(discovers) == 2
    assert discovers[0]["payload"]["section"] == "/tags/xxx"  # el encolado inicial
    assert discovers[1]["payload"]["cursor"] == "2"
    assert discovers[1]["payload"]["section"] == "/tags/xxx"  # propagada a la siguiente página

    videos = _videos()
    assert len(videos) == 3
    assert all(video["status"] == "indexed" for video in videos)
    assert all(job["status"] == JobStatus.DONE.value for job in _jobs())
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# SC-003 · INCREMENTAL no duplica
# ---------------------------------------------------------------------------


def test_incremental_does_not_duplicate_videos_jobs_nor_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-003: INCREMENTAL sobre la misma fuente no duplica vídeos, jobs ni frames."""
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        harness.adapter, client=_client(), worker_id="it-pipeline-sc003"
    )
    _run_backfill(pipeline, worker, jobs, limit=2)

    frames_before = _total_frames()
    jobs_before = {
        row["job_type"]: row["n"]
        for row in _rows("select job_type, count(*) as n from public.jobs group by job_type")
    }

    assert _run_backfill(pipeline, worker, jobs, limit=2, mode="incremental") == 2  # 2 DISCOVER

    assert len(_videos()) == 3  # sin vídeos nuevos
    assert all(video["status"] == "indexed" for video in _videos())
    assert _total_frames() == frames_before  # sin frames duplicados

    jobs_after = {
        row["job_type"]: row["n"]
        for row in _rows("select job_type, count(*) as n from public.jobs group by job_type")
    }
    assert jobs_after["FETCH_METADATA"] == jobs_before["FETCH_METADATA"]  # solo IDs nuevos
    assert jobs_after["INDEX_VIDEO"] == jobs_before["INDEX_VIDEO"]
    assert jobs_after["DISCOVER"] == jobs_before["DISCOVER"] + 2  # solo la paginación

    assert all(job["status"] != JobStatus.PENDING.value for job in _jobs())  # terminales
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# SC-004 · Fallos del job: estados terminales + sin temporales
# ---------------------------------------------------------------------------


class _FailingAssetsAdapter(MockAdapter):
    """MockAdapter que falla SOLO `get_visual_assets` para unos external_ids.

    `MockFaults.by_external_id` (PR-021) inyecta el fallo en get_video Y en
    get_visual_assets; para que el fallo ocurra exactamente en el handler
    INDEX_VIDEO (sin romper DISCOVER/FETCH_METADATA del mismo vídeo) se
    especializa solo ese método (test-only, sin red).
    """

    def __init__(self, *, fail_on: set[str], error: Exception, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fail_on = fail_on
        self._error = error

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        if video.external_id in self._fail_on:
            raise self._error
        return await super().get_visual_assets(video)


def test_transient_failure_exhausts_attempts_and_leaves_no_temp_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-004: un fallo transitorio en INDEX_VIDEO termina `failed`; nada colgado ni temporal."""
    _fast_rate(monkeypatch)
    adapter = _FailingAssetsAdapter(
        seed=42,
        catalog_size=3,
        fail_on={"mock-vid-0001"},
        error=MockAdapterTransientError("fallo transitorio inyectado en get_visual_assets"),
    )
    jobs = JobsRepo(delay_fn=lambda _attempts: 0.0)  # backoff 0: agota intentos en la pasada
    pipeline, worker, _ = _build(
        adapter, client=_client(), jobs=jobs, worker_id="it-pipeline-sc004"
    )
    _run_backfill(pipeline, worker, jobs, limit=2)

    jobs_rows = _jobs()
    assert all(job["status"] != JobStatus.RUNNING.value for job in jobs_rows)
    failed = [job for job in jobs_rows if job["status"] == JobStatus.FAILED.value]
    assert len(failed) == 1  # solo el INDEX_VIDEO de 0001
    assert failed[0]["job_type"] == JobType.INDEX_VIDEO.value
    assert failed[0]["attempts"] == 3  # reintentos acotados por max_attempts (FR-008)
    assert "fallo transitorio inyectado en get_visual_assets" in failed[0]["error"]

    # El vídeo fallido queda `discovered` (el fallo ocurrió antes del estado indexing).
    by_external_id = {video["external_id"]: video for video in _videos()}
    assert by_external_id["mock-vid-0001"]["status"] == "discovered"
    assert by_external_id["mock-vid-0000"]["status"] == "indexed"  # aislamiento por vídeo
    assert by_external_id["mock-vid-0001"]["frame_count"] == 0  # nada indexado

    assert _leftover_asset_dirs() == []  # FR-015: sin temporales tras el fallo


def test_terminal_removed_fault_marks_job_unavailable_without_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec edge case: `removed` del adapter → job `unavailable` definitivo (sin reintentos)."""
    _fast_rate(monkeypatch)
    adapter = _FailingAssetsAdapter(
        seed=42,
        catalog_size=3,
        fail_on={"mock-vid-0002"},
        error=MockAdapterRemovedError("mock adapter: video removed (404)"),
    )
    jobs = JobsRepo(delay_fn=lambda _attempts: 0.0)
    pipeline, worker, _ = _build(adapter, client=_client(), jobs=jobs, worker_id="it-pipeline-trm")
    _run_backfill(pipeline, worker, jobs, limit=2)

    unavailable = [
        job
        for job in _jobs()
        if job["job_type"] == JobType.INDEX_VIDEO.value
        and job["status"] == JobStatus.UNAVAILABLE.value
    ]
    assert len(unavailable) == 1
    assert unavailable[0]["attempts"] == 0  # `unavailable` no consume intentos (FR-008)
    assert "removed" in unavailable[0]["error"]
    assert all(job["status"] != JobStatus.RUNNING.value for job in _jobs())  # nada colgado

    by_external_id = {video["external_id"]: video for video in _videos()}
    assert by_external_id["mock-vid-0002"]["status"] == "discovered"  # falló antes de indexing
    assert by_external_id["mock-vid-0000"]["status"] == "indexed"  # el resto sigue
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# PR-036 · Cota global --max-videos (analyze hallazgo 2 · SC-002)
# ---------------------------------------------------------------------------


def test_backfill_max_videos_stops_discover_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-036: `max_videos` corta la cadena de DISCOVER al alcanzar la cota.

    Catálogo de 5 con página `limit=2` y cota 3: la 1ª página procesa 2 vídeos
    y encola el siguiente DISCOVER con `videos_counted=2`; la 2ª procesa el 3er
    vídeo y NO encola más DISCOVER (el catálogo queda a medias, sin jobs
    colgados ni temporales). Trazabilidad: 3 vídeos únicos `indexed`, jobs
    `done` con payload `source`/`external_id`.
    """
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=5)
    pipeline, worker, jobs = _build(
        harness.adapter, client=_client(), worker_id="it-pipeline-maxvideos"
    )

    async def _scenario() -> int:
        await jobs.enqueue(
            JobType.DISCOVER,
            payload={
                "source": "mock",
                "cursor": None,
                "limit": 2,
                "mode": "backfill",
                "max_videos": 3,
            },
        )
        return await worker.run_once()

    assert _run(_scenario()) == 8  # 2 DISCOVER + 3 FETCH_METADATA + 3 INDEX_VIDEO

    videos = _videos()
    assert len(videos) == 3  # la cota cortó el catálogo de 5
    assert sorted(video["external_id"] for video in videos) == [
        "mock-vid-0000",
        "mock-vid-0001",
        "mock-vid-0002",
    ]
    assert all(video["status"] == "indexed" for video in videos)

    jobs_rows = _jobs()
    assert all(job["status"] == JobStatus.DONE.value for job in jobs_rows)  # nada colgado
    discovers = [job for job in jobs_rows if job["job_type"] == JobType.DISCOVER.value]
    assert len(discovers) == 2  # el DISCOVER tras la cota NO se encoló
    next_discover = discovers[1]
    assert next_discover["payload"]["cursor"] == "2"
    assert next_discover["payload"]["videos_counted"] == 2
    assert next_discover["payload"]["max_videos"] == 3
    assert _leftover_asset_dirs() == []


def test_incremental_max_videos_counts_known_videos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-036: en INCREMENTAL los vídeos YA CONOCIDOS también cuentan para la cota.

    Tras un backfill completo (3 vídeos), un INCREMENTAL con `max_videos=2` y
    página `limit=2` examina 2 vídeos conocidos (los omite, SC-003) y alcanza
    la cota: no encola el siguiente DISCOVER. Sin duplicados ni jobs nuevos.
    """
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        harness.adapter, client=_client(), worker_id="it-pipeline-maxvideos-incr"
    )
    _run_backfill(pipeline, worker, jobs, limit=2)
    assert len(_videos()) == 3

    async def _scenario() -> int:
        await jobs.enqueue(
            JobType.DISCOVER,
            payload={
                "source": "mock",
                "cursor": None,
                "limit": 2,
                "mode": "incremental",
                "max_videos": 2,
            },
        )
        return await worker.run_once()

    assert _run(_scenario()) == 1  # solo el DISCOVER (todos conocidos, cota alcanzada)

    assert len(_videos()) == 3  # sin vídeos nuevos
    assert all(video["status"] == "indexed" for video in _videos())
    discovers = [job for job in _jobs() if job["job_type"] == JobType.DISCOVER.value]
    assert len(discovers) == 3  # 2 del backfill + 1 del incremental: la cota cortó la cadena
    assert all(job["status"] != JobStatus.PENDING.value for job in _jobs())
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# FR-009/SC-005 · Rate limiter respetado en cada llamada al adapter
# ---------------------------------------------------------------------------


class _FakeClock:
    """Reloj fake: solo avanza cuando el sleeper fake duerme (patrón PR-022)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _FakeSleeper:
    """Sleeper fake: registra cada espera y avanza el reloj fake."""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds


def test_backfill_respects_rate_limiter() -> None:
    """FR-009/SC-005: cada llamada al adapter pasa por el rate limiter de la fuente.

    Limiter inyectado con reloj/sleeper fakes (determinista, sin dormir): el
    BACKFILL de 3 vídeos hace exactamente 11 acquires (2 discover + 3 get_video
    del DISCOVER + 3 get_video de FETCH_METADATA + 3 get_visual_assets de
    INDEX_VIDEO) y espera 10 × 50 ms = 0.5 s — el límite declarado se respeta y
    las esperas son medibles (SC-005).
    """
    clock = _FakeClock()
    sleeper = _FakeSleeper(clock)
    limiter = RateLimiter(
        RateLimitSpec(min_interval_ms=50, max_rps=1000.0),
        clock=clock,
        sleeper=sleeper,
        jitter_factor=0.0,
        source="mock",
    )
    harness = MockHarness(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        harness.adapter,
        client=_client(),
        limiter_factory=lambda _adapter: limiter,
        worker_id="it-pipeline-ratelimit",
    )
    _run_backfill(pipeline, worker, jobs, limit=2)

    assert limiter.stats.requests == 11
    assert limiter.stats.waits == 10  # la primera request es inmediata
    assert limiter.stats.total_wait_seconds == pytest.approx(10 * 0.05)
    assert sleeper.sleeps == [0.05] * 10
    assert _leftover_asset_dirs() == []


def test_stats_rate_limits_section_after_backfill() -> None:
    """PR-035 · SC-005/NFR-004: `stats` con sección `rate_limits` coherente tras procesar jobs.

    Con el limiter falso acelerado (reloj/sleeper fakes, determinista), el
    BACKFILL de 3 vídeos hace exactamente 11 requests con 10 esperas de 50 ms:
    el pipeline agrega la contabilidad por fuente
    (`CrawlerPipeline.rate_limit_stats`) y `repo.stats(rate_limits=...)` la
    expone (requests/rate_limit_waits/total_wait_ms) junto a las secciones
    existentes de FR-014 — 0 violaciones, esperas medibles (SC-005 · NFR-004).
    """
    clock = _FakeClock()
    sleeper = _FakeSleeper(clock)
    limiter = RateLimiter(
        RateLimitSpec(min_interval_ms=50, max_rps=1000.0),
        clock=clock,
        sleeper=sleeper,
        jitter_factor=0.0,
        source="mock",
    )
    harness = MockHarness(seed=42, catalog_size=3)
    repo = CrawlerRepo()
    pipeline, worker, jobs = _build(
        harness.adapter,
        repo=repo,
        client=_client(),
        limiter_factory=lambda _adapter: limiter,
        worker_id="it-pipeline-stats-rl",
    )
    _run_backfill(pipeline, worker, jobs, limit=2)

    stats = _run(repo.stats(rate_limits=pipeline.rate_limit_stats()))
    assert stats.rate_limits == {
        "mock": RateLimitStatsRecord(requests=11, rate_limit_waits=10, total_wait_ms=500)
    }
    assert stats.jobs_by_status == {"done": 8}  # secciones existentes intactas (FR-014)
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# FR-013 · CHECK_AVAILABILITY: unavailable/removed + exclusión del índice
# ---------------------------------------------------------------------------


def test_check_availability_marks_unavailable_removed_and_excludes_from_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-013: `unavailable`/`removed` aplican estado + exclusión del índice del spike.

    Los frames del vídeo se eliminan (`VectorStore.delete_video`, FR-014 del
    spike) y el vídeo deja de aparecer en `ann_search` (filtro `exclude_videos`).
    El job se completa: la comprobación se hizo (PR-027).
    """
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        harness.adapter, client=_client(), worker_id="it-pipeline-avail"
    )
    _run_backfill(pipeline, worker, jobs, limit=2)

    videos = {video["external_id"]: video for video in _videos()}
    assert _frames_for(videos["mock-vid-0001"]["id"]) in _expected_frames(
        storyboard=_MOCK_STORYBOARD_TILES, thumbnails=_MOCK_THUMBNAILS
    )

    # El mismo catálogo con estados configurables por external_id (PR-021).
    harness_with_states = harness.with_faults(
        MockFaults(
            availability={
                "mock-vid-0001": VideoAvailability.UNAVAILABLE,
                "mock-vid-0002": VideoAvailability.REMOVED,
            }
        )
    )
    pipeline2, worker2, jobs2 = _build(
        harness_with_states.adapter, client=_client(), worker_id="it-pipeline-avail2"
    )

    async def _scenario() -> None:
        for external_id in ("mock-vid-0001", "mock-vid-0002"):
            video = videos[external_id]
            await jobs2.enqueue(
                JobType.CHECK_AVAILABILITY,
                source_id=video["source_id"],  # psycopg devuelve uuid.UUID nativo
                video_id=video["id"],
                payload={"source": "mock", "external_id": external_id},
            )
        assert await worker2.run_once() == 2

    _run(_scenario())

    refreshed = {video["external_id"]: video for video in _videos()}
    assert refreshed["mock-vid-0001"]["status"] == "unavailable"
    assert refreshed["mock-vid-0001"]["excluded"] is True
    assert refreshed["mock-vid-0002"]["status"] == "removed"
    assert refreshed["mock-vid-0002"]["excluded"] is True
    assert _frames_for(videos["mock-vid-0001"]["id"]) == 0  # frames eliminados del índice
    assert _frames_for(videos["mock-vid-0002"]["id"]) == 0

    # El vídeo excluido deja de ser consultable (FR-013 · mecanismo del spike).
    store = PgVectorStore()
    provider = FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION)
    query = [
        float(value) for value in provider.embed_images([_served_storyboard("mock-vid-0001")])[0]
    ]
    hits = _run(store.ann_search(query, k=10))
    assert all(str(hit["video_id"]) != str(videos["mock-vid-0001"]["id"]) for hit in hits)
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# Nota revisión Ola B · sync_source conserva `enabled` humano
# ---------------------------------------------------------------------------


def test_sync_source_preserves_human_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nota Ola B: refrescar el manifest no revoca `sources.enabled=true` previo."""
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=1)
    repo = CrawlerRepo()
    _run(
        repo.upsert_source(
            name="mock", adapter="mock", manifest=harness.adapter.manifest, enabled=True
        )
    )

    pipeline, worker, jobs = _build(
        harness.adapter, repo=repo, client=_client(), worker_id="it-pipeline-en"
    )
    _run_backfill(pipeline, worker, jobs, limit=1)

    source = _run(repo.get_source("mock"))
    assert source is not None
    assert source.enabled is True  # la habilitación humana se conserva
    assert source.manifest.rate_limit == harness.adapter.manifest.rate_limit  # manifest refrescado
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# FETCH_METADATA: vídeo ausente en la fuente → terminal `unavailable`
# ---------------------------------------------------------------------------


def test_fetch_metadata_missing_video_is_terminal_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec edge case: un vídeo que ya no existe en la fuente termina `unavailable`.

    `adapter.get_video` devuelve `None` → `VideoUnavailableError` (terminal,
    contracts §3): el job no reintenta y no deja temporales.
    """
    _fast_rate(monkeypatch)
    harness = MockHarness(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        harness.adapter, client=_client(), worker_id="it-pipeline-missing"
    )

    async def _scenario() -> None:
        await jobs.enqueue(
            JobType.FETCH_METADATA,
            payload={"source": "mock", "external_id": "mock-vid-9999"},
        )
        assert await worker.run_once() == 1

    _run(_scenario())

    jobs_rows = _jobs()
    assert len(jobs_rows) == 1
    assert jobs_rows[0]["status"] == JobStatus.UNAVAILABLE.value
    assert jobs_rows[0]["attempts"] == 0  # terminal: `unavailable` no consume intentos (FR-008)
    assert "ya no existe en la fuente" in jobs_rows[0]["error"]
    assert _videos() == []  # no se creó ningún vídeo
    assert _leftover_asset_dirs() == []


# ---------------------------------------------------------------------------
# Rate limit spec canónico (unificación Ola A) — smoke de integración
# ---------------------------------------------------------------------------


def test_pipeline_uses_canonical_rate_limit_spec() -> None:
    """La unificación de la Ola A (contracts §1) llega al pipeline vía el manifest."""
    harness = MockHarness(seed=42, catalog_size=1)
    spec = harness.adapter.manifest.rate_limit
    assert spec.min_interval_ms == 100  # default declarado por el MockAdapter (PR-021)
    assert spec.max_rps == 10.0


# ---------------------------------------------------------------------------
# PR-034 · Assets del mock servidos in-process (hallazgo del quickstart, PR-033)
# ---------------------------------------------------------------------------


class _NoInProcessAdapter(MockAdapter):
    """MockAdapter SIN `fetch_asset_bytes` (PR-034): la descarga debe ir por HTTP.

    `fetch_asset_bytes` es **opcional** en el contrato (contracts §1): sin él,
    el pipeline cae a `AssetFetcher`/`SafeHTTPClient` (la ruta de las fuentes
    reales, p. ej. xvideos). El cliente de tests (MockTransport) sirve los
    mismos bytes sintéticos del catálogo.
    """

    fetch_asset_bytes: Callable[[str], Any] | None = None


class _InProcessPreviewsAdapter(MockAdapter):
    """MockAdapter que además sirve el preview **in-process** (PR-034).

    Cubre la ruta del pipeline para bytes in-process de assets `preview`
    (mp4 → archivo temporal → FFmpeg), sin red, como el resto del mock.
    """

    async def fetch_asset_bytes(self, url: str) -> bytes | None:
        data = await super().fetch_asset_bytes(url)
        if data is not None:
            return data
        if url.endswith("/preview.mp4"):
            return _preview_bytes(url)
        return None


def test_real_wiring_mock_indexes_videos_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hallazgo PR-034 (regresión): el cableado REAL indexa el mock SIN red.

    Reproduce el fallo de la validación del quickstart: worker + `JobsRepo` +
    `CrawlerRepo` + pipeline + `MockAdapter` **sin fakes de fetch** (sin
    `client` inyectado, sin MockTransport). Antes del fix, los jobs
    `INDEX_VIDEO` degradaban TODOS los assets (`http://mock.local/...` pasa por
    el `SafeHTTPClient` real: allowlist/esquema lo bloquean) → 0 vídeos
    indexados y 45 jobs failed. Ahora el mock sirve sus imágenes **in-process**
    (`fetch_asset_bytes`, FR-003 · SC-001) → vídeos `indexed` con
    frames+embeddings **consultables** (SC-002); el preview (sin mp4 sintético)
    degrada con warning y nunca se abre un socket (NFR-003).
    """
    _fast_rate(monkeypatch)
    adapter = MockAdapter(seed=42, catalog_size=3)
    jobs = JobsRepo(delay_fn=lambda _attempts: 0.0)
    pipeline, worker, _ = _build(adapter, jobs=jobs, worker_id="it-pipeline-pr034")
    assert _run_backfill(pipeline, worker, jobs, limit=2) == 8  # 2 DISCOVER + 3 + 3

    videos = _videos()
    assert len(videos) == 3
    assert all(video["status"] == "indexed" for video in videos)  # 0 degradados (SC-002)

    jobs_rows = _jobs()
    assert all(job["status"] != JobStatus.RUNNING.value for job in jobs_rows)
    assert all(job["status"] == JobStatus.DONE.value for job in jobs_rows)  # 0 failed (hallazgo)

    by_external_id = {video["external_id"]: video for video in videos}
    # Determinista SIN ffmpeg: storyboard+thumbnails in-process; preview degrada.
    assert _frames_for(by_external_id["mock-vid-0000"]["id"]) == _MOCK_THUMBNAILS
    for external_id in ("mock-vid-0001", "mock-vid-0002"):
        assert _frames_for(by_external_id[external_id]["id"]) == (
            _MOCK_STORYBOARD_TILES + _MOCK_THUMBNAILS
        )

    # Embeddings CONSULTABLES vía el VectorStore del spike (SC-002 · ADR-0011),
    # con la imagen exacta que el mock sirve in-process (PR-034).
    store = PgVectorStore()
    provider = FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION)
    query = [
        float(value) for value in provider.embed_images([_served_storyboard("mock-vid-0001")])[0]
    ]
    hits = _run(store.ann_search(query, k=5))
    assert str(hits[0]["video_id"]) == str(by_external_id["mock-vid-0001"]["id"])
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)

    assert _leftover_asset_dirs() == []  # SC-004: sin temporales tras el flujo


def test_adapter_without_fetch_asset_bytes_uses_http_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-034: un adapter sin `fetch_asset_bytes` → `None` → ruta HTTP real.

    El contrato de las fuentes reales NO cambia: sin el método opcional, el
    pipeline construye su `AssetFetcher` con el cliente seguro (aquí con
    MockTransport sirviendo los mismos bytes sintéticos del catálogo) y el
    flujo indexa igual (regresión del contrato de xvideos).
    """
    _fast_rate(monkeypatch)
    adapter = _NoInProcessAdapter(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(
        adapter, client=_client(), worker_id="it-pipeline-http-fallback"
    )
    assert _run_backfill(pipeline, worker, jobs, limit=2) == 8

    videos = _videos()
    assert len(videos) == 3
    assert all(video["status"] == "indexed" for video in videos)
    assert all(job["status"] == JobStatus.DONE.value for job in _jobs())
    assert _leftover_asset_dirs() == []


def test_in_process_preview_bytes_are_extracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-034: bytes `preview` servidos in-process se extraen con FFmpeg (sin red).

    Si ffmpeg no está disponible el preview degrada (bytes vacíos) y el vídeo
    sigue indexándose con storyboard+thumbnails (jerarquía de assets, FR-005);
    con ffmpeg, el preview aporta frames adicionales al total.
    """
    _fast_rate(monkeypatch)
    adapter = _InProcessPreviewsAdapter(seed=42, catalog_size=3)
    pipeline, worker, jobs = _build(adapter, worker_id="it-pipeline-ip-preview")
    assert _run_backfill(pipeline, worker, jobs, limit=2) == 8

    videos = _videos()
    assert len(videos) == 3
    assert all(video["status"] == "indexed" for video in videos)
    assert all(job["status"] == JobStatus.DONE.value for job in _jobs())
    if ffmpeg_available():
        assert _total_frames() > 3 * _MOCK_THUMBNAILS + 2 * _MOCK_STORYBOARD_TILES
    assert _leftover_asset_dirs() == []
