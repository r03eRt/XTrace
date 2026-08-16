"""Pipeline crawler → índice (PR-030 · FR-007/FR-010/FR-011/FR-013/FR-015 · SC-002/003/004/005
· ADR-0011 · contracts §6).

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
- **INDEX_VIDEO** (FR-011 · SC-002): `adapter.get_visual_assets` → descarga de
  assets permitidos (storyboard/thumbnail/preview; **nunca** vídeo completo,
  SC-006) → frames con timestamp (storyboard con `timestamp_ms`/`position` como
  frame; previews cortos vía FFmpeg con `PreviewFrameExtractor`, PR-029) →
  pHash (`compute_phash`) + embedding por lotes (`EmbeddingProvider`; el FAKE
  determinista de `xtrace_spike.embeddings.fake` es el default de tests/CI, el
  real queda para ejecución local vía inyección) → `VectorStore.upsert_frames`
  (idempotente: `UNIQUE(video_id, frame_seq)`, SC-003) → vídeo `indexed`
  (FR-012).
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
- DISCOVER: `{"source": str, "cursor": str|null, "limit": int, "mode": "backfill"|"incremental"}`
- FETCH_METADATA / INDEX_VIDEO / CHECK_AVAILABILITY: `{"source": str, "external_id": str}`

El módulo depende solo de contratos (protocols locales + `SourceAdapter` y
`Settings`): `repo` (CrawlerRepoProtocol), `jobs` (JobsRepoProtocol) y `store`
(contrato `VectorStore` del spike) se inyectan; `adapter_for` resuelve el adapter
del job (p. ej. el registry de PR-028). Añadir una fuente no toca este módulo
(SC-007).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypedDict
from urllib.parse import urlsplit

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
from xtrace_crawler.assets.fetch import AssetFetcher
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
    SafeHTTPClient,
    SchemeNotAllowedError,
)
from xtrace_crawler.crawling.ratelimit import RateLimiter
from xtrace_crawler.jobs.types import Job, JobType
from xtrace_crawler.jobs.worker import DEFAULT_DISCOVER_LIMIT, JobHandler, JobWorker
from xtrace_crawler.repo import SourceRecord, VideoRecord, VideoStatus, parse_uuid

logger = logging.getLogger(__name__)

#: Tamaño de lote del embedding (paridad con el pipeline del spike, PR-010).
DEFAULT_BATCH_SIZE: int = 64
#: Intervalo de extracción de frames de previews cortos (PR-029).
DEFAULT_PREVIEW_INTERVAL_S: float = 1.0
#: Paridad con `PgVectorStore` (spike PR-007): los frames sin timestamp usan un
#: ordinal en [1e9, 2^31) para no colisionar con los `frame_seq = timestamp_ms`.
_NULL_TS_SEQ_OFFSET: int = 1_000_000_000
#: Fallos de PROCESADO de un asset que degradan por asset (nunca tumban el vídeo):
#: descarga (HTTP/límite/política de hosts), preview (FFmpeg/duración) y crop.
#: Los fallos del ADAPTER no están aquí: se propagan al worker (PR-027).
_DEGRADABLE_ASSET_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPStatusError,
    DownloadTooLargeError,
    HostNotAllowedError,
    SchemeNotAllowedError,
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
                construye uno por vídeo con la allowlist derivada de las URLs de
                los assets (default seguro: solo https, SEC-001 — los adapters
                con http en dev inyectan su cliente con `allow_http=True`).
            settings: configuración para los overrides de rate limit (D5);
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

        Raises:
            ValueError: parámetros fuera de rango (uso incorrecto).
        """
        if batch_size < 1:
            raise ValueError(f"batch_size debe ser >= 1; recibido {batch_size}")
        if preview_interval_s <= 0:
            raise ValueError(f"preview_interval_s debe ser > 0; recibido {preview_interval_s}")
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

        adapter = self._adapter_for(job)
        source_record = await self.sync_source(adapter)
        await self._acquire(adapter)
        page = await adapter.discover(cursor=cursor, limit=limit)

        for external_id in page.external_ids:
            if mode == "incremental":
                existing = await self._repo.get_web_video(source, external_id)
                if existing is not None:
                    logger.info(
                        "INCREMENTAL: %s de %s ya existe; se omite (SC-003)", external_id, source
                    )
                    continue
            await self._acquire(adapter)
            video = await adapter.get_video(external_id)
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

        if page.next_cursor is not None:
            await self._jobs.enqueue(
                JobType.DISCOVER,
                source_id=parse_uuid(source_record.id, "source_id"),
                payload={
                    "source": source,
                    "cursor": page.next_cursor,
                    "limit": limit,
                    "mode": mode,
                },
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
        video = await adapter.get_video(external_id)
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
        assets = await adapter.get_visual_assets(video)
        await self._repo.set_video_status(record.id, "indexing")

        frames = await self._collect_frames(assets, video)
        try:
            if not frames:
                raise ValueError(
                    f"no se obtuvieron frames de ningún asset de {external_id!r} "
                    "(todos degradados o lista vacía)"
                )
            records = self._embed_frames(video_id=record.id, frames=frames)
            await self._store.upsert_frames(records)
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
        self, assets: list[VisualAsset], video: VideoSource
    ) -> list[IndexedFrame]:
        """Descarga los assets y los convierte en frames; degradación por asset.

        Cada asset se procesa en su propio `try/except` de `_DEGRADABLE_ASSET_ERRORS`
        (descarga/preview/crop): un asset fallido se omite con warning y el resto
        del vídeo continúa (spec edge case: la jerarquía de assets degrada sin
        fallar todo el vídeo).
        """
        client = self._client if self._client is not None else self._client_for_assets(assets)
        fetcher = AssetFetcher(client)
        frames: list[IndexedFrame] = []
        for asset in assets:
            try:
                frames.extend(await self._frames_from_asset(fetcher, asset, video))
            except _DEGRADABLE_ASSET_ERRORS as exc:
                logger.warning(
                    "asset %s (%s) omitido por degradación: %s", asset.url, asset.kind, exc
                )
        return frames

    async def _frames_from_asset(
        self, fetcher: AssetFetcher, asset: VisualAsset, video: VideoSource
    ) -> list[IndexedFrame]:
        """Convierte UN asset en frames (imágenes en memoria, nunca archivos temporales).

        - storyboard: si la fuente ya da `position`/`timestamp_ms` por asset, el
          asset ES un frame (con su timestamp); un sprite sin esos campos se
          indexa como un único frame, salvo que `storyboard_grid` resuelva el
          grid (entonces se recorta en tiles con timestamp aproximado, PR-029).
        - thumbnail: un frame sin timestamp (paridad FR-012 del spike: el frame
          se indexa sin timestamp, sin fallar).
        - preview: FFmpeg extrae frames del preview CORTO con `preview_interval_s`
          (PR-029; nunca un vídeo completo, SC-006).
        """
        if asset.kind == "preview":
            return await self._preview_frames(fetcher, asset)
        frames: list[IndexedFrame] = []
        async with fetcher.fetch(asset) as path:
            with Image.open(path) as image:
                if (
                    asset.kind == "storyboard"
                    and asset.position is None
                    and asset.timestamp_ms is None
                ):
                    grid = (
                        self._storyboard_grid(asset) if self._storyboard_grid is not None else None
                    )
                    if grid is not None:
                        cols, rows = grid
                        for tile in split_storyboard(
                            image, cols=cols, rows=rows, duration_ms=video.duration_ms
                        ):
                            frames.append(
                                IndexedFrame(
                                    image=tile.image.copy(), timestamp_ms=tile.timestamp_ms
                                )
                            )
                        return frames
                frames.append(IndexedFrame(image=image.copy(), timestamp_ms=asset.timestamp_ms))
        return frames

    async def _preview_frames(
        self, fetcher: AssetFetcher, asset: VisualAsset
    ) -> list[IndexedFrame]:
        """Frames de un preview corto; los JPEGs viven dentro del temporal del asset.

        `out_dir=path.parent` = el directorio temporal del asset descargado: al
        salir del context manager de `AssetFetcher.fetch` se eliminan junto con
        el preview (FR-015). Las imágenes se copian a memoria antes de borrar.
        """
        frames: list[IndexedFrame] = []
        async with fetcher.fetch(asset) as path:
            extracted = self._preview_extractor.extract_frames(
                path, interval_s=self._preview_interval_s, out_dir=path.parent
            )
            for preview_frame in extracted:
                with Image.open(preview_frame.path) as image:
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

    def _client_for_assets(self, assets: list[VisualAsset]) -> SafeHTTPClient:
        """Cliente por defecto con allowlist derivada de las URLs de los assets (SEC-001).

        Solo https por defecto (http requiere `allow_http=True` explícito, flag
        dev): los adapters con http (p. ej. el mock en tests) inyectan su cliente
        con MockTransport + `allow_http=True`.
        """
        hosts: set[str] = set()
        for asset in assets:
            host = urlsplit(asset.url).hostname
            if host:
                hosts.add(host)
        if not hosts:
            raise ValueError("sin hosts de assets para construir el cliente HTTP; inyecta `client`")
        return SafeHTTPClient(allowed_hosts=hosts)

    async def _mark_unavailable(self, source: str, external_id: str) -> None:
        """Marca el vídeo `unavailable` + exclusión del índice si su fila existe (FR-012/013)."""
        record = await self._repo.get_web_video(source, external_id)
        if record is None:
            return
        await self._repo.set_video_status(record.id, "unavailable")
        await self._repo.exclude(record.id, excluded=True)
        await self._store.delete_video(record.id)
