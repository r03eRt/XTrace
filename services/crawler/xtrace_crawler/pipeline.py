"""Pipeline crawler → índice (PR-030 · FR-007/FR-010/FR-011/FR-013/FR-015 · SC-002/003/004/005
· ADR-0011 · contracts §6 · PR-036 SEC-001/FR-007).

**PR-036 (hardening + cota global)**: (1) la allowlist de hosts de assets se
lee del adapter (`adapter.asset_hosts`, nunca de las URLs parseadas) — sin
allowlist declarada no hay descarga HTTP (`NoAssetHostsError`); (2) el cliente
de assets valida la IP resuelta (anti-DNS-rebinding, `PrivateIPError`); (3)
toda imagen se abre con límite de píxeles (`ImageTooManyPixelsError`, config
`XTRACE_CRAWLER_MAX_IMAGE_PIXELS`); (4) `max_videos`/`videos_counted` en el
payload DISCOVER cortan la cadena de paginación al alcanzar la cota global del
backfill (analyze hallazgo 2 · SC-002).

Handlers concretos para el `JobWorker` (PR-027) que cierran el flujo
discover → get_video → get_visual_assets → frames → pHash + embedding → índice,
**reutilizando el pipeline del spike** (ADR-0011: `xtrace_spike.hashing.phash`,
`xtrace_spike.embeddings`, `xtrace_spike.indexing.pipeline.frame_id_for`,
`xtrace_spike.vectorstore`; el spike permanece intocado):

- **DISCOVER** (FR-007 · SC-003): una página de `adapter.discover(cursor, limit)`
  → `CrawlerRepo.upsert_web_video` (estado `discovered`, FR-012) → encola
  `FETCH_METADATA` por vídeo y el siguiente `DISCOVER` (paginación por cursor).
  Modos **BACKFILL** e **INCREMENTAL** (payload `mode`): INCREMENTAL solo procesa
  **IDs nuevos** (los ya existentes se omiten) y el dedupe por `dedupe_key` del
  repo de jobs evita duplicar jobs activos — SC-003: no se duplican vídeos ni
  frames.
- **FETCH_METADATA**: `adapter.get_video(external_id)` → `upsert_web_video`
  (actualiza **solo metadatos**: title/duration/tags/published_at/urls; el
  estado del vídeo no se toca, decisión PR-028) → encola `INDEX_VIDEO`. Un vídeo
  que ya no existe en la fuente es **terminal** (`VideoUnavailableError`):
  vídeo `unavailable` + `exclude` y el job termina en `unavailable` sin
  reintentos (contracts §3 · spec edge cases).
- **INDEX_VIDEO** (FR-011 · SC-002): `adapter.get_visual_assets` → bytes de
  assets permitidos (storyboard/thumbnail/preview; **nunca** vídeo completo,
  SC-006) → frames con timestamp (storyboard con `timestamp_ms`/`position` como
  frame; previews cortos vía FFmpeg con `PreviewFrameExtractor`, PR-029) →
  pHash (`compute_phash`) + embedding por lotes (`EmbeddingProvider`; el FAKE
  determinista de `xtrace_spike.embeddings.fake` es el default de tests/CI, el
  real queda para ejecución local vía inyección) → `VectorStore.upsert_frames`
  (idempotente: `UNIQUE(video_id, frame_seq)`, SC-003) → vídeo `indexed`
  (FR-012).
  **PR-034 (hallazgo del quickstart, PR-033)**: cada asset se sirve primero con
  el método **opcional** del contrato `adapter.fetch_asset_bytes(url)`
  (`adapters/base.py`) — bytes in-process, sin red (el `MockAdapter` devuelve
  imágenes sintéticas deterministas, FR-003/SC-001); si devuelve `None` (o el
  adapter no lo implementa) se descarga por HTTP con
  `AssetFetcher`/`SafeHTTPClient` (ruta actual de las fuentes reales, p. ej.
  xvideos: contrato funcional sin cambios).
- **CHECK_AVAILABILITY** (FR-013): `adapter.check_availability` → `unavailable`/
  `removed` aplica el estado del vídeo + **exclusión del índice** (`repo.exclude`
  + `VectorStore.delete_video`, mecanismo del spike FR-014); `available` no
  cambia nada. El job se completa aunque el resultado no sea `available` (la
  comprobación SÍ se hizo; PR-027).
- **Rate limit por fuente** (FR-009 · SC-005): **cada llamada al adapter**
  (discover/get_video/get_visual_assets/check_availability) pasa por el
  `RateLimiter` (PR-022) con spec efectivo `Settings.rate_limit_for(source,
  manifest.rate_limit)` — defaults del manifest (contracts §1, D5) con override
  por env. `limiter_factory` permite inyectar limiters deterministas en tests
  (sin dormir de verdad). Por eso estos handlers son **concretos** y no usan los
  factories base de PR-027 (`discover_handler`/`check_availability_handler`): el
  rate limit debe envolver cada llamada individual, no el job.
- **Cleanup garantizado** (FR-015 · SC-004): cada asset se descarga en un
  directorio temporal dedicado (`AssetFetcher.fetch`, PR-029) que se elimina en
  `finally` pase lo que pase; los frames de preview se extraen dentro de ese
  mismo directorio (se borran con él); las imágenes PIL se cierran en `finally`
  del handler. Los fallos de **procesado de asset** (descarga, preview, crop)
  degradan por asset con warning (spec edge case: la jerarquía de assets nunca
  tumba el vídeo entero); los fallos del **adapter** se propagan al worker, que
  los clasifica (transitorio → backoff; terminal → `unavailable`).

Payloads de jobs (contrato interno; PR-032 los encola desde el CLI):
- DISCOVER: `{"source": str, "cursor": str|null, "limit": int,
  "mode": "backfill"|"incremental", "max_videos": int|null (PR-036),
  "videos_counted": int (PR-036; acumulado de páginas previas)}`
- FETCH_METADATA / INDEX_VIDEO / CHECK_AVAILABILITY: `{"source": str, "external_id": str}`

El módulo depende solo de contratos (protocols locales + `SourceAdapter` y
`Settings`): `repo` (CrawlerRepoProtocol), `jobs` (JobsRepoProtocol) y `store`
(contrato `VectorStore` del spike) se inyectan; `adapter_for` resuelve el adapter
del job (p. ej. el registry de PR-028). Añadir una fuente no toca este módulo
(SC-007).
"""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

import httpx
import numpy as np
from PIL import Image
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider  # type: ignore[import-untyped]
from xtrace_spike.hashing.phash import compute_phash  # type: ignore[import-untyped]
from xtrace_spike.indexing.pipeline import frame_id_for  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.base import VectorStore  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.pgvector import (  # type: ignore[import-untyped]
    EMBEDDING_DIMENSION,
    PgVectorStore,
)

from xtrace_crawler.adapters.base import AdapterManifest, SourceAdapter
from xtrace_crawler.adapters.models import VideoAvailability, VideoSource, VisualAsset
from xtrace_crawler.assets.fetch import (
    AssetFetcher,
    ImageTooManyPixelsError,
    open_image_limited,
)
from xtrace_crawler.assets.preview import (
    PreviewExtractionError,
    PreviewFrameExtractor,
    PreviewTooLongError,
)
from xtrace_crawler.assets.storyboard import StoryboardError, split_storyboard
from xtrace_crawler.config import Settings
from xtrace_crawler.crawling.http import (
    DownloadTooLargeError,
    HostNotAllowedError,
    NoAssetHostsError,
    PrivateIPError,
    SafeHTTPClient,
    SchemeNotAllowedError,
)
from xtrace_crawler.crawling.ratelimit import RateLimiter
from xtrace_crawler.jobs.types import Job, JobType
from xtrace_crawler.jobs.worker import DEFAULT_DISCOVER_LIMIT, JobHandler, JobWorker
from xtrace_crawler.repo import (
    RateLimitStatsRecord,
    SourceRecord,
    VideoRecord,
    VideoStatus,
    parse_uuid,
    rate_limit_stats_record,
)

logger = logging.getLogger(__name__)

#: Tamaño de lote del embedding (paridad con el pipeline del spike, PR-010).
DEFAULT_BATCH_SIZE: int = 64
#: Intervalo de extracción de frames de previews cortos (PR-029).
DEFAULT_PREVIEW_INTERVAL_S: float = 1.0
#: Paridad con `PgVectorStore` (spike PR-007): los frames sin timestamp usan un
#: ordinal en [1e9, 2^31) para no colisionar con los `frame_seq = timestamp_ms`.
_NULL_TS_SEQ_OFFSET: int = 1_000_000_000
#: Fallos de PROCESADO de un asset que degradan por asset (nunca tumban el vídeo):
#: descarga (HTTP/límite/política de hosts/IPs, PR-036), apertura de imagen
#: (límite de píxeles, PR-036), preview (FFmpeg/duración) y crop. Incluye los
#: errores tipados PR-036: `NoAssetHostsError` (adapter sin allowlist de hosts),
#: `PrivateIPError` (IP resuelta interna) e `ImageTooManyPixelsError`
#: (decompression bomb). Los fallos del ADAPTER no están aquí: se propagan al
#: worker (PR-027).
_DEGRADABLE_ASSET_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPStatusError,
    DownloadTooLargeError,
    HostNotAllowedError,
    NoAssetHostsError,
    PrivateIPError,
    SchemeNotAllowedError,
    ImageTooManyPixelsError,
    PreviewExtractionError,
    PreviewTooLongError,
    StoryboardError,
    OSError,
)


class VideoUnavailableError(Exception):
    """El vídeo ya no existe en la fuente: fallo terminal (contracts §3 · FR-008).

    Convención del worker (PR-027 · `jobs/backoff.classify_error`): el atributo
    `terminal=True` clasifica el error como terminal → el job termina en
    `unavailable` definitivo, sin reintentos.
    """

    terminal = True


class CrawlerRepoProtocol(Protocol):
    """Contrato mínimo de `CrawlerRepo` (PR-028) que el pipeline necesita."""

    async def get_source(self, name: str) -> SourceRecord | None: ...

    async def upsert_source(
        self,
        *,
        name: str,
        adapter: str,
        manifest: AdapterManifest,
        enabled: bool = False,
    ) -> SourceRecord: ...

    async def upsert_web_video(self, source_id: str, video: VideoSource) -> VideoRecord: ...

    async def get_web_video(self, source_name: str, external_id: str) -> VideoRecord | None: ...

    async def set_video_status(self, video_id: str, status: VideoStatus) -> bool: ...

    async def exclude(self, video_id: str, *, excluded: bool = True) -> bool: ...


class JobsRepoProtocol(Protocol):
    """Contrato mínimo de `JobsRepo` (PR-026) que el pipeline necesita (encolado)."""

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
    ) -> Job: ...


class EmbeddingProviderProtocol(Protocol):
    """Contrato `EmbeddingProvider` del spike (ADR-0007 · contracts §6), tipado localmente.

    El spike no declara `py.typed` (ADR-0011); este protocolo es la vista tipada
    del contrato de `xtrace_spike.embeddings.provider` que el pipeline consume.
    """

    model_id: str
    dimension: int

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray[Any, Any]: ...


class FrameRecord(TypedDict):
    """Registro de frame para `VectorStore.upsert_frames` (contrato del spike, contracts §6).

    Vista tipada local de `xtrace_spike.vectorstore.base.FrameRecord`: frame_id y
    video_id estables (idempotencia SC-003), timestamp opcional, pHash de 64 bits
    (FIX-phash · FR-011) y embedding L2-normalizado de `EMBEDDING_DIMENSION`.
    """

    frame_id: str
    video_id: str
    timestamp_ms: int | None
    phash: int
    embedding: Sequence[float]


@dataclass(frozen=True)
class IndexedFrame:
    """Frame en memoria (imagen PIL abierta) con su timestamp aproximado (ms)."""

    image: Image.Image
    timestamp_ms: int | None


def video_source_from_record(record: VideoRecord, *, source: str) -> VideoSource:
    """Reconstruye el `VideoSource` normalizado (contracts §2) desde la fila de BD.

    Los campos web de una fila creada por `upsert_web_video` nunca son nulos
    (`external_id`/`page_url`); si la fila está corrupta se falla temprano.
    """
    if record.external_id is None or record.page_url is None:
        raise ValueError(f"fila de vídeo web incompleta (external_id/page_url nulos): {record.id}")
    return VideoSource(
        source=source,
        external_id=record.external_id,
        title=record.title,
        page_url=record.page_url,
        duration_ms=record.duration_ms,
        thumbnail_url=record.thumbnail_url,
        preview_url=record.preview_url,
        storyboard_urls=record.storyboard_urls or [],
        tags=record.tags or [],
        published_at=record.published_at,
    )


class CrawlerPipeline:
    """Pipeline crawler → índice con handlers concretos para el `JobWorker` (PR-027).

    Uso típico (PR-032 lo cablea al CLI):

        pipeline = CrawlerPipeline(
            repo=CrawlerRepo(),
            jobs=JobsRepo(),
            store=PgVectorStore(),
            adapter_for=lambda job: registry.get_enabled(...),
            embeddings=SiglipLocalProvider(),   # local; el FAKE es el default
        )
        worker = JobWorker(JobsRepo(), concurrency=4)
        pipeline.register_handlers(worker)
        await worker.run_forever(stop)
    """

    def __init__(
        self,
        *,
        repo: CrawlerRepoProtocol,
        jobs: JobsRepoProtocol,
        adapter_for: Callable[[Job], SourceAdapter],
        store: VectorStore | None = None,
        embeddings: EmbeddingProviderProtocol | None = None,
        client: SafeHTTPClient | None = None,
        settings: Settings | None = None,
        limiter_factory: Callable[[SourceAdapter], RateLimiter] | None = None,
        storyboard_grid: Callable[[VisualAsset], tuple[int, int] | None] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        preview_interval_s: float = DEFAULT_PREVIEW_INTERVAL_S,
        max_image_pixels: int | None = None,
    ) -> None:
        """Crea el pipeline sobre los repos/índice inyectados.

        Args:
            repo: repo de fuentes/vídeos-web (contrato `CrawlerRepoProtocol`).
            jobs: repo de la cola (contrato `JobsRepoProtocol`; solo encola).
            adapter_for: resuelve el adapter de la fuente de un job (registry PR-028).
            store: índice vectorial del spike (default `PgVectorStore`, ADR-0011).
            embeddings: proveedor de embeddings (default: FAKE determinista de
                `xtrace_spike.embeddings.fake`, dimensión del esquema PR-006; el
                real — SigLIP — se inyecta para ejecución local).
            client: cliente HTTP seguro para descargas de assets; sin él se
                construye uno por fuente con la **allowlist declarada por el
                adapter** (`adapter.asset_hosts`, PR-036 — nunca derivada de
                las URLs parseadas) y validación de IP resuelta activa
                (anti-DNS-rebinding). Sin `asset_hosts` declarado → error
                tipado `NoAssetHostsError` (fail-closed, SEC-001).
            settings: configuración para los overrides de rate limit (D5) y
                los límites PR-036 (`max_image_pixels`, `backfill_max_videos`);
                default `Settings()` (env).
            limiter_factory: construye el `RateLimiter` de una fuente (default:
                `RateLimiter(Settings.rate_limit_for(source, manifest.rate_limit),
                source=...)`); en tests permite inyectar reloj/sleeper fakes.
            storyboard_grid: resuelve el grid (cols, rows) de un asset storyboard
                sin `position`/`timestamp_ms` (sprites de un solo asset, p. ej.
                xvideos); `None` (default) indexa el sprite como un único frame
                (degradación, nunca falla el vídeo).
            batch_size: lote del embedding (paridad spike).
            preview_interval_s: intervalo de frames de previews (PR-029).
            max_image_pixels: presupuesto de píxeles al abrir imágenes
                (decompression bomb, PR-036); `None` → `settings.max_image_pixels`
                (env `XTRACE_CRAWLER_MAX_IMAGE_PIXELS`, default 50 MP).

        Raises:
            ValueError: parámetros fuera de rango (uso incorrecto).
        """
        if batch_size < 1:
            raise ValueError(f"batch_size debe ser >= 1; recibido {batch_size}")
        if preview_interval_s <= 0:
            raise ValueError(f"preview_interval_s debe ser > 0; recibido {preview_interval_s}")
        if max_image_pixels is not None and max_image_pixels < 1:
            raise ValueError(f"max_image_pixels debe ser >= 1; recibido {max_image_pixels}")
        self._repo = repo
        self._jobs = jobs
        self._adapter_for = adapter_for
        self._store: VectorStore = store if store is not None else PgVectorStore()
        self._embeddings: EmbeddingProviderProtocol = (
            embeddings
            if embeddings is not None
            else FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION)
        )
        self._client = client
        self._settings = settings if settings is not None else Settings()
        self._limiter_factory = limiter_factory
        self._storyboard_grid = storyboard_grid
        self._batch_size = batch_size
        self._preview_interval_s = preview_interval_s
        self._preview_extractor = PreviewFrameExtractor()
        self._limiters: dict[str, RateLimiter] = {}
        self._max_image_pixels = (
            max_image_pixels if max_image_pixels is not None else self._settings.max_image_pixels
        )

    # -- Registro en el worker (PR-027) ---------------------------------------

    def make_handlers(self) -> dict[JobType, JobHandler]:
        """Handlers concretos por `JobType` (DATA-002 · contracts §3)."""
        return {
            JobType.DISCOVER: self._discover,
            JobType.FETCH_METADATA: self._fetch_metadata,
            JobType.INDEX_VIDEO: self._index_video,
            JobType.CHECK_AVAILABILITY: self._check_availability,
        }

    def register_handlers(self, worker: JobWorker) -> None:
        """Registra todos los handlers en un `JobWorker` (dispatcher PR-027)."""
        for job_type, handler in self.make_handlers().items():
            worker.register_handler(job_type, handler)

    async def sync_source(self, adapter: SourceAdapter) -> SourceRecord:
        """Registra/actualiza la fuente con el manifest del adapter (SEC-002 · DATA-001).

        Nota de la revisión de la Ola B (tasks.md PR-030): `upsert_source` (PR-028)
        sobrescribe `enabled`; aquí se conserva la habilitación humana previa
        (`sources.enabled`) para no revocar una aprobación al refrescar el manifest.
        """
        name = adapter.manifest.source
        existing = await self._repo.get_source(name)
        return await self._repo.upsert_source(
            name=name,
            adapter=name,
            manifest=adapter.manifest,
            enabled=existing.enabled if existing is not None else False,
        )

    # -- Handlers --------------------------------------------------------------

    async def _discover(self, job: Job) -> None:
        """DISCOVER (FR-007): una página del catálogo → vídeos `discovered` + FETCH_METADATA.

        BACKFILL procesa todos los IDs de la página; INCREMENTAL (SC-003) solo los
        **nuevos** (los ya existentes en BD se omiten). El siguiente `DISCOVER`
        (cursor) se encola con `dedupe_key` para no duplicar cadenas activas.

        **Cota global `max_videos` (PR-036 · analyze hallazgo 2 · SC-002)**: el
        payload opcional `max_videos` (>= 1) corta la cadena de paginación al
        alcanzar la cota, **acumulando vídeos ya conocidos y nuevos**
        (`videos_counted`, contador global del backfill que fluye por payload).
        Al alcanzarla: no se procesan más IDs de la página y NO se encola el
        siguiente DISCOVER (log claro). Obligatoria para el backfill real de
        xvideos (contracts §5); el CLI la inyecta siempre (`--max-videos` o el
        default de config).
        """
        source = self._source_name(job)
        mode = job.payload.get("mode", "backfill")
        if mode not in ("backfill", "incremental"):
            raise ValueError(
                f"payload['mode'] debe ser 'backfill' o 'incremental'; recibido {mode!r}"
            )
        cursor = job.payload.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError(
                f"payload['cursor'] debe ser str o null; recibido {type(cursor).__name__}"
            )
        limit_raw = job.payload.get("limit", DEFAULT_DISCOVER_LIMIT)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"payload['limit'] debe ser un entero; recibido {limit_raw!r}"
            ) from None
        if limit < 1:
            raise ValueError(f"payload['limit'] debe ser >= 1; recibido {limit}")

        max_videos: int | None = None
        max_videos_raw = job.payload.get("max_videos")
        if max_videos_raw is not None:
            try:
                max_videos = int(max_videos_raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"payload['max_videos'] debe ser un entero; recibido {max_videos_raw!r}"
                ) from None
            if max_videos < 1:
                raise ValueError(f"payload['max_videos'] debe ser >= 1; recibido {max_videos}")
        counted_raw = job.payload.get("videos_counted", 0)
        try:
            counted_so_far = int(counted_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"payload['videos_counted'] debe ser un entero; recibido {counted_raw!r}"
            ) from None
        if counted_so_far < 0:
            raise ValueError(f"payload['videos_counted'] debe ser >= 0; recibido {counted_so_far}")

        adapter = self._adapter_for(job)
        source_record = await self.sync_source(adapter)
        await self._acquire(adapter)
        started = time.perf_counter()
        page = await adapter.discover(cursor=cursor, limit=limit)
        logger.info(
            "etapa=discover source=%s job=%s duration_ms=%.1f ids=%d",
            source,
            job.id,
            (time.perf_counter() - started) * 1000.0,
            len(page.external_ids),
        )

        counted = counted_so_far
        capped = False
        for external_id in page.external_ids:
            if max_videos is not None and counted >= max_videos:
                capped = True
                break
            counted += 1
            if mode == "incremental":
                existing = await self._repo.get_web_video(source, external_id)
                if existing is not None:
                    logger.info(
                        "INCREMENTAL: %s de %s ya existe; se omite (SC-003)", external_id, source
                    )
                    continue
            await self._acquire(adapter)
            # PR-045 (3a validación real): la URL canónica de un vídeo exige el
            # slug del listado (reconstruir `/video.<id>/` sin él → 404). El
            # href completo viaja en `page.page_urls[external_id]` y se reenvía
            # a get_video; `None` (fuentes sin page_urls, p. ej. el mock) → la
            # fuente reconstruye su URL como antes (retrocompatible).
            video = await adapter.get_video(external_id, page_url=page.page_urls.get(external_id))
            if video is None:
                logger.warning(
                    "DISCOVER: %s listado pero sin metadatos en la fuente; se omite", external_id
                )
                continue
            record = await self._repo.upsert_web_video(source_record.id, video)
            await self._jobs.enqueue(
                JobType.FETCH_METADATA,
                source_id=parse_uuid(source_record.id, "source_id"),
                video_id=parse_uuid(record.id, "video_id"),
                payload={"source": source, "external_id": external_id},
                dedupe_key=f"fetch_metadata:{source}:{external_id}",
            )
            logger.info("DISCOVER (%s): vídeo %s descubierto (%s)", mode, external_id, record.id)

        # Cota alcanzada (a mitad de página o justo al final): la cadena de
        # paginación se detiene aquí — sin siguiente DISCOVER (PR-036).
        if not capped and max_videos is not None and counted >= max_videos:
            capped = True
        if capped:
            logger.info(
                "cota global de backfill alcanzada source=%s max_videos=%d "
                "(contados=%d incl. conocidos) job=%s: se detiene el discover y "
                "no se encola la siguiente página",
                source,
                max_videos,
                counted,
                job.id,
            )

        if page.next_cursor is not None and not capped:
            next_payload: dict[str, Any] = {
                "source": source,
                "cursor": page.next_cursor,
                "limit": limit,
                "mode": mode,
                "videos_counted": counted,
            }
            if max_videos is not None:
                next_payload["max_videos"] = max_videos
            await self._jobs.enqueue(
                JobType.DISCOVER,
                source_id=parse_uuid(source_record.id, "source_id"),
                payload=next_payload,
                dedupe_key=f"discover:{source}:{page.next_cursor}:{mode}",
            )

    async def _fetch_metadata(self, job: Job) -> None:
        """FETCH_METADATA: metadatos frescos de la fuente → upsert (solo metadatos) → INDEX_VIDEO.

        Un vídeo que ya no existe en la fuente es terminal (spec edge case
        404/removed): vídeo `unavailable` + exclusión y el job termina en
        `unavailable` sin reintentos (contracts §3).
        """
        source = self._source_name(job)
        external_id = self._external_id(job)
        adapter = self._adapter_for(job)
        await self._acquire(adapter)
        started = time.perf_counter()
        video = await adapter.get_video(external_id)
        logger.info(
            "etapa=metadata source=%s external_id=%s job=%s duration_ms=%.1f",
            source,
            external_id,
            job.id,
            (time.perf_counter() - started) * 1000.0,
        )
        if video is None:
            await self._mark_unavailable(source, external_id)
            raise VideoUnavailableError(
                f"vídeo {external_id!r} ya no existe en la fuente {source!r}"
            )
        source_record = await self._repo.get_source(source)
        if source_record is None:
            raise ValueError(
                f"fuente {source!r} no registrada en BD; ejecuta DISCOVER antes (job {job.id})"
            )
        record = await self._repo.upsert_web_video(source_record.id, video)
        await self._jobs.enqueue(
            JobType.INDEX_VIDEO,
            source_id=parse_uuid(source_record.id, "source_id"),
            video_id=parse_uuid(record.id, "video_id"),
            payload={"source": source, "external_id": external_id},
            dedupe_key=f"index_video:{record.id}",
        )
        logger.info("FETCH_METADATA: metadatos actualizados de %s (%s)", external_id, record.id)

    async def _index_video(self, job: Job) -> None:
        """INDEX_VIDEO (FR-011 · SC-002): assets → frames → pHash + embedding → índice → `indexed`.

        El vídeo pasa por `indexing` (FR-012) antes de descargar; los temporales
        de cada asset se eliminan en `finally` (FR-015) y las imágenes PIL se
        cierran siempre. Un vídeo sin ningún frame útil (todos los assets
        degradados) falla transitoriamente: se reintenta con backoff.
        """
        source = self._source_name(job)
        external_id = self._external_id(job)
        record = await self._repo.get_web_video(source, external_id)
        if record is None:
            raise ValueError(
                f"vídeo {external_id!r} de {source!r} no encontrado en BD (job {job.id})"
            )
        video = video_source_from_record(record, source=source)
        adapter = self._adapter_for(job)
        await self._acquire(adapter)
        started = time.perf_counter()
        assets = await adapter.get_visual_assets(video)
        await self._repo.set_video_status(record.id, "indexing")

        frames = await self._collect_frames(assets, video, adapter)
        logger.info(
            "etapa=assets source=%s external_id=%s job=%s duration_ms=%.1f assets=%d frames=%d",
            source,
            external_id,
            job.id,
            (time.perf_counter() - started) * 1000.0,
            len(assets),
            len(frames),
        )
        try:
            if not frames:
                raise ValueError(
                    f"no se obtuvieron frames de ningún asset de {external_id!r} "
                    "(todos degradados o lista vacía)"
                )
            embed_started = time.perf_counter()
            records = self._embed_frames(video_id=record.id, frames=frames)
            await self._store.upsert_frames(records)
            logger.info(
                "etapa=embed source=%s external_id=%s job=%s duration_ms=%.1f frames=%d",
                source,
                external_id,
                job.id,
                (time.perf_counter() - embed_started) * 1000.0,
                len(records),
            )
            await self._repo.set_video_status(record.id, "indexed")
            logger.info(
                "INDEX_VIDEO: vídeo indexado source=%s external_id=%s frames=%d",
                source,
                external_id,
                len(records),
            )
        finally:
            for frame in frames:
                frame.image.close()

    async def _check_availability(self, job: Job) -> None:
        """CHECK_AVAILABILITY (FR-013): estado del vídeo + exclusión del índice.

        `unavailable`/`removed` aplican el estado del vídeo (FR-012), el flag de
        exclusión (`repo.exclude`, mecanismo del spike) y la eliminación de sus
        frames del índice (`VectorStore.delete_video`, FR-014 del spike).
        `available` no cambia nada. El job se completa en todos los casos (la
        comprobación se hizo; PR-027).
        """
        source = self._source_name(job)
        external_id = self._external_id(job)
        record = await self._repo.get_web_video(source, external_id)
        if record is None:
            raise ValueError(
                f"vídeo {external_id!r} de {source!r} no encontrado en BD (job {job.id})"
            )
        video = video_source_from_record(record, source=source)
        adapter = self._adapter_for(job)
        await self._acquire(adapter)
        availability = await adapter.check_availability(video)
        if availability is VideoAvailability.AVAILABLE:
            logger.info("CHECK_AVAILABILITY: %s sigue disponible en %s", external_id, source)
            return
        status: VideoStatus = (
            "unavailable" if availability is VideoAvailability.UNAVAILABLE else "removed"
        )
        logger.warning(
            "CHECK_AVAILABILITY: %s → %s: exclusión del índice (FR-013)", external_id, status
        )
        await self._repo.set_video_status(record.id, status)
        await self._repo.exclude(record.id, excluded=True)
        await self._store.delete_video(record.id)

    # -- Assets → frames (FR-005/FR-011/FR-015) ---------------------------------

    async def _collect_frames(
        self, assets: list[VisualAsset], video: VideoSource, adapter: SourceAdapter
    ) -> list[IndexedFrame]:
        """Obtiene los bytes de los assets y los convierte en frames; degradación por asset.

        **PR-034**: cada asset se sirve primero con el método **opcional** del
        contrato `adapter.fetch_asset_bytes(url)` (`adapters/base.py`) — bytes
        in-process, sin red (el `MockAdapter` devuelve imágenes sintéticas
        deterministas, FR-003/SC-001). Si devuelve `None` —o el adapter no lo
        implementa— se descarga por HTTP con `AssetFetcher`/`SafeHTTPClient`
        (ruta actual de las fuentes reales; el cliente solo se construye si
        algún asset necesita la red).

        **PR-036 · SSRF**: el cliente HTTP por defecto se construye con la
        **allowlist de hosts declarada por el adapter** (`adapter.asset_hosts`,
        nunca derivada de las URLs parseadas) y validación de IP resuelta
        activa; un adapter real sin `asset_hosts` NO descarga assets
        (`NoAssetHostsError`). **Decompression bomb**: toda imagen se abre con
        `open_image_limited` (presupuesto `max_image_pixels` verificado por
        header antes de decodificar).

        Cada asset se procesa en su propio `try/except` de `_DEGRADABLE_ASSET_ERRORS`
        (bytes in-process inválidos, descarga/preview/crop/límites PR-036): un
        asset fallido se omite con warning y el resto del vídeo continúa (spec
        edge case: la jerarquía de assets degrada sin fallar todo el vídeo).
        """
        fetcher: AssetFetcher | None = None
        frames: list[IndexedFrame] = []
        for asset in assets:
            try:
                in_process = await self._in_process_bytes(adapter, asset)
                if in_process is not None:
                    frames.extend(await self._frames_from_bytes(in_process, asset, video))
                else:
                    if fetcher is None:
                        fetcher = self._fetcher_for(adapter)
                    frames.extend(await self._frames_from_asset(fetcher, asset, video))
            except _DEGRADABLE_ASSET_ERRORS as exc:
                logger.warning(
                    "asset %s (%s) omitido por degradación: %s", asset.url, asset.kind, exc
                )
        return frames

    async def _in_process_bytes(self, adapter: SourceAdapter, asset: VisualAsset) -> bytes | None:
        """Bytes del asset servidos por el propio adapter (PR-034 · contracts §1).

        El método `fetch_asset_bytes` es **opcional** en el contrato (los
        adapters reales no lo implementan): se descubre con `getattr` y el
        valor `None` —ausente o devolviendo `None`— significa descargar por
        HTTP (`AssetFetcher`/`SafeHTTPClient`).
        """
        provider: Callable[[str], Awaitable[bytes | None]] | None = getattr(
            adapter, "fetch_asset_bytes", None
        )
        if provider is None:
            return None
        return await provider(asset.url)

    def _fetcher_for(self, adapter: SourceAdapter) -> AssetFetcher:
        """`AssetFetcher` con el cliente seguro de los assets (SEC-001 · PR-036).

        Usa el `client` inyectado si existe (operador/tests); si no, construye
        el cliente por fuente con la allowlist DECLARADA por el adapter
        (`_client_for_assets`). Solo se construye si algún asset necesita la
        ruta HTTP (PR-034): el mock in-process no crea ningún
        `SafeHTTPClient` (0 superficie de red).
        """
        client = self._client if self._client is not None else self._client_for_assets(adapter)
        return AssetFetcher(client)

    def _client_for_assets(self, adapter: SourceAdapter) -> SafeHTTPClient:
        """Cliente por defecto de assets con la **allowlist por fuente** (PR-036 · SEC-001).

        La allowlist NO se deriva de las URLs parseadas de los assets: se lee
        del contrato del adapter (`adapter.asset_hosts`, hosts revisados:
        dominio canónico + CDNs documentados). Un adapter real sin
        `asset_hosts` → `NoAssetHostsError` (fail-closed: sin allowlist
        revisada no se descarga nada por HTTP). El cliente activa la
        **validación de IP resuelta** (anti-DNS-rebinding, PR-036).

        El mock (FR-003) declara `asset_hosts = []` (lista vacía, fail-closed
        igualmente): sirve sus assets in-process (`fetch_asset_bytes`,
        PR-034) y el preview (sin representación) degrada con
        `NoAssetHostsError` — 0 superficie de red.
        """
        hosts: Any = getattr(adapter, "asset_hosts", None)
        if not hosts:
            raise NoAssetHostsError(
                f"el adapter {adapter.manifest.source!r} no declara 'asset_hosts' "
                "(allowlist de hosts de assets revisada): no se descargan assets "
                "por HTTP (SEC-001 · PR-036)"
            )
        return SafeHTTPClient(allowed_hosts=set(hosts), validate_resolved_ip=True)

    async def _frames_from_bytes(
        self, data: bytes, asset: VisualAsset, video: VideoSource
    ) -> list[IndexedFrame]:
        """Frames desde los bytes IN-PROCESS de un asset (PR-034 · FR-011).

        - `preview`: bytes → archivo temporal dedicado → `PreviewFrameExtractor`
          (FFmpeg sobre previews cortos, PR-029); el temporal se elimina en
          `finally` (FR-015).
        - `storyboard`/`thumbnail`: bytes → imagen en memoria (`BytesIO`), sin
          archivos temporales (FR-015). **PR-036**: se abre con
          `open_image_limited` (presupuesto de píxeles verificado por header
          antes de decodificar — decompression bomb).
        """
        if asset.kind == "preview":
            return await self._preview_frames_from_bytes(data, asset)
        with open_image_limited(io.BytesIO(data), max_pixels=self._max_image_pixels) as image:
            return self._image_frames(image, asset, video)

    async def _preview_frames_from_bytes(
        self, data: bytes, asset: VisualAsset
    ) -> list[IndexedFrame]:
        """Frames de un preview servido IN-PROCESS (PR-034): bytes → temporal FFmpeg.

        El mp4 se escribe en un directorio temporal dedicado que se elimina en
        `finally` pase lo que pase (FR-015); los JPEGs extraídos viven dentro
        de ese mismo directorio y las imágenes se copian a memoria antes de
        borrar. **PR-036**: los JPEGs extraídos también pasan por
        `open_image_limited` (límite de píxeles).
        """
        frames: list[IndexedFrame] = []
        tmp_dir = Path(tempfile.mkdtemp(prefix="xtrace-crawler-asset-"))
        try:
            path = tmp_dir / "asset.mp4"
            path.write_bytes(data)
            extracted = self._preview_extractor.extract_frames(
                path, interval_s=self._preview_interval_s, out_dir=tmp_dir
            )
            for preview_frame in extracted:
                with open_image_limited(
                    preview_frame.path, max_pixels=self._max_image_pixels
                ) as image:
                    frames.append(
                        IndexedFrame(image=image.copy(), timestamp_ms=preview_frame.timestamp_ms)
                    )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return frames

    async def _frames_from_asset(
        self, fetcher: AssetFetcher, asset: VisualAsset, video: VideoSource
    ) -> list[IndexedFrame]:
        """Convierte UN asset descargado por HTTP en frames (ruta de fuentes reales).

        - storyboard: si la fuente ya da `position`/`timestamp_ms` por asset, el
          asset ES un frame (con su timestamp); un sprite sin esos campos se
          indexa como un único frame, salvo que `storyboard_grid` resuelva el
          grid (entonces se recorta en tiles con timestamp aproximado, PR-029).
        - thumbnail: un frame sin timestamp (paridad FR-012 del spike: el frame
          se indexa sin timestamp, sin fallar).
        - preview: FFmpeg extrae frames del preview CORTO con `preview_interval_s`
          (PR-029; nunca un vídeo completo, SC-006).

        **PR-036**: la imagen descargada se abre con `open_image_limited`
        (presupuesto de píxeles por header antes de decodificar).
        """
        if asset.kind == "preview":
            return await self._preview_frames(fetcher, asset)
        async with fetcher.fetch(asset) as path:
            with open_image_limited(path, max_pixels=self._max_image_pixels) as image:
                return self._image_frames(image, asset, video)

    def _image_frames(
        self, image: Image.Image, asset: VisualAsset, video: VideoSource
    ) -> list[IndexedFrame]:
        """Convierte una imagen ya abierta en frames (lógica compartida PR-034:
        ruta HTTP y ruta in-process producen exactamente los mismos frames).

        - storyboard: si la fuente ya da `position`/`timestamp_ms` por asset, el
          asset ES un frame (con su timestamp); un sprite sin esos campos se
          indexa como un único frame, salvo que `storyboard_grid` resuelva el
          grid (entonces se recorta en tiles con timestamp aproximado, PR-029).
        - thumbnail: un frame sin timestamp (paridad FR-012 del spike: el frame
          se indexa sin timestamp, sin fallar).
        """
        if asset.kind == "storyboard" and asset.position is None and asset.timestamp_ms is None:
            grid = self._storyboard_grid(asset) if self._storyboard_grid is not None else None
            if grid is not None:
                cols, rows = grid
                return [
                    IndexedFrame(image=tile.image.copy(), timestamp_ms=tile.timestamp_ms)
                    for tile in split_storyboard(
                        image,
                        cols=cols,
                        rows=rows,
                        duration_ms=video.duration_ms,
                        max_pixels=self._max_image_pixels,
                    )
                ]
        return [IndexedFrame(image=image.copy(), timestamp_ms=asset.timestamp_ms)]

    async def _preview_frames(
        self, fetcher: AssetFetcher, asset: VisualAsset
    ) -> list[IndexedFrame]:
        """Frames de un preview corto; los JPEGs viven dentro del temporal del asset.

        `out_dir=path.parent` = el directorio temporal del asset descargado: al
        salir del context manager de `AssetFetcher.fetch` se eliminan junto con
        el preview (FR-015). Las imágenes se copian a memoria antes de borrar.
        **PR-036**: los JPEGs extraídos pasan por `open_image_limited`.
        """
        frames: list[IndexedFrame] = []
        async with fetcher.fetch(asset) as path:
            extracted = self._preview_extractor.extract_frames(
                path, interval_s=self._preview_interval_s, out_dir=path.parent
            )
            for preview_frame in extracted:
                with open_image_limited(
                    preview_frame.path, max_pixels=self._max_image_pixels
                ) as image:
                    frames.append(
                        IndexedFrame(image=image.copy(), timestamp_ms=preview_frame.timestamp_ms)
                    )
        return frames

    def _embed_frames(self, *, video_id: str, frames: Sequence[IndexedFrame]) -> list[FrameRecord]:
        """Embedding por lotes + pHash (FIX-phash) → registros del índice (FR-011).

        Paridad con `xtrace_spike.indexing.pipeline._embed_frames` (PR-010):
        `frame_id` estable por (video_id, frame_seq) vía `frame_id_for` del spike,
        `frame_seq = timestamp_ms` o un ordinal con offset 1e9 (paridad con
        `PgVectorStore`), embeddings a float nativo.
        """
        records: list[FrameRecord] = []
        no_timestamp_ordinal = 0
        for start in range(0, len(frames), self._batch_size):
            chunk = frames[start : start + self._batch_size]
            images = [frame.image for frame in chunk]
            vectors = self._embeddings.embed_images(images)
            self._check_embedding_shape(vectors, len(chunk))
            phashes = [compute_phash(image) for image in images]
            for frame, vector, phash in zip(chunk, vectors, phashes, strict=True):
                frame_seq = (
                    frame.timestamp_ms
                    if frame.timestamp_ms is not None
                    else _NULL_TS_SEQ_OFFSET + no_timestamp_ordinal
                )
                if frame.timestamp_ms is None:
                    no_timestamp_ordinal += 1
                records.append(
                    FrameRecord(
                        frame_id=frame_id_for(video_id, frame_seq),
                        video_id=video_id,
                        timestamp_ms=frame.timestamp_ms,
                        phash=phash,
                        embedding=[float(value) for value in vector],
                    )
                )
        return records

    def _check_embedding_shape(self, vectors: np.ndarray[Any, Any], count: int) -> None:
        """Valida el contrato del proveedor: shape (N, D) con D = dimension (paridad spike)."""
        expected = (count, self._embeddings.dimension)
        if vectors.shape != expected:
            raise ValueError(
                f"EmbeddingProvider devolvió shape {vectors.shape}; se esperaba {expected}"
            )

    # -- Rate limit (FR-009 · SC-005) -------------------------------------------

    async def _acquire(self, adapter: SourceAdapter) -> None:
        """Espera el rate limit de la fuente antes de CADA llamada al adapter (FR-009)."""
        await self._limiter_for(adapter).acquire()

    def _limiter_for(self, adapter: SourceAdapter) -> RateLimiter:
        """Limiter por fuente (cacheado): `limiter_factory` inyectado o spec efectivo.

        Spec efectivo = `Settings.rate_limit_for(source, manifest.rate_limit)`:
        override por env (D5) > defaults del manifest (contracts §1).
        """
        name = adapter.manifest.source
        limiter = self._limiters.get(name)
        if limiter is None:
            if self._limiter_factory is not None:
                limiter = self._limiter_factory(adapter)
            else:
                spec = self._settings.rate_limit_for(name, adapter.manifest.rate_limit)
                limiter = RateLimiter(spec, source=name)
            self._limiters[name] = limiter
        return limiter

    def rate_limit_stats(self) -> dict[str, RateLimitStatsRecord]:
        """Contabilidad del rate limiter por fuente (PR-035 · SC-005 · NFR-004).

        Expone las métricas acumuladas de los limiters cacheados por fuente
        (requests/waits/tiempo total esperado) para que `stats` las incruste
        (`repo.stats(rate_limits=...)`) como sección `rate_limits` — sección
        nueva del JSON, sin tocar las existentes (FR-014 · contracts §5).
        """
        return {
            name: rate_limit_stats_record(limiter.stats)
            for name, limiter in sorted(self._limiters.items())
        }

    # -- Helpers ------------------------------------------------------------------

    def _source_name(self, job: Job) -> str:
        """Nombre canónico de la fuente del job: payload['source'] o el manifest del adapter."""
        value = job.payload.get("source")
        if isinstance(value, str) and value:
            return value
        return self._adapter_for(job).manifest.source

    def _external_id(self, job: Job) -> str:
        value = job.payload.get("external_id")
        if not isinstance(value, str) or not value:
            raise ValueError(f"payload['external_id'] requerido (str no vacío); recibido {value!r}")
        return value

    async def _mark_unavailable(self, source: str, external_id: str) -> None:
        """Marca el vídeo `unavailable` + exclusión del índice si su fila existe (FR-012/013)."""
        record = await self._repo.get_web_video(source, external_id)
        if record is None:
            return
        await self._repo.set_video_status(record.id, "unavailable")
        await self._repo.exclude(record.id, excluded=True)
        await self._store.delete_video(record.id)
