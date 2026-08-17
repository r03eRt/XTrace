"""Pipeline de indexación (PR-010 · FR-005/006/007/008/009 · SC-005/006 · ADR-0006/0007).

Orquesta, por vídeo del dataset, la cadena completa:

    scan (ingest/dataset.py, llamador) -> extract_frames (PR-008)
    -> dedupe_frames (PR-009) -> pHash + embed_images en batches (FR-004/FR-005)
    -> VideoIndexWriter.replace_video_index (FR-006/FR-007/FR-010)

El pHash de cada frame representativo (FR-004) se calcula con
hashing.phash.compute_phash — la misma función que usa el dedupe (PR-009) —
y viaja en el FrameRecord (FIX-phash) para que el índice lo persista
(InMemory y PgVectorStore): frames.phash deja de ser un centinela y queda
con la firma real del frame.

Depende solo de las interfaces (ADR-0007): `VectorStore`, `EmbeddingProvider`
y `VideoStateStore` se inyectan por constructor. En tests se usan
`InMemoryVectorStore` + `FakeEmbeddingProvider` + `InMemoryVideoStateStore`
(deterministas, sin DB ni Torch); en producción `PgVectorStore` +
`SiglipLocalProvider` + `PgVideoStateStore`.

Garantías:
- **Idempotencia (FR-008/SC-005)**: `video_id` y `frame_id` se derivan de
  forma determinista (uuid5) del `local_ref` estable (PR-008) y de la
  posición del frame (`frame_seq` = timestamp o ordinal), de modo que
  reindexar re-upserta las mismas claves y no genera duplicados (el upsert
  del store es idempotente: InMemory por `frame_id`, Pg por
  UNIQUE(video_id, frame_seq)).
- **Cleanup garantizado (FR-009/SC-006/ADR-0006)**: cada vídeo se procesa
  dentro del context manager `extract_frames` (borra su directorio temporal
  en finally, PR-008) y `index_dataset` envuelve la ejecución en un
  directorio temporal propio que se elimina en finally pase lo que pase.
- **Resiliencia (FR-001 US1 esc. 3)**: un vídeo que falla (fichero corrupto,
  error de embedding, …) se marca `failed` con el error y el resto del
  dataset continúa.
- **Embedding por lotes (FR-005)**: `batch_size` configurable; se valida que
  el proveedor devuelva shape (N, D).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from xtrace_spike.embeddings.provider import EmbeddingProvider
from xtrace_spike.hashing.phash import PHASH_BITS, compute_phash
from xtrace_spike.indexing.state import (
    STATUS_FAILED,
    STATUS_INDEXED,
    InMemoryVideoStateStore,
    VideoStateStore,
)
from xtrace_spike.indexing.writer import VideoIndexWriter
from xtrace_spike.ingest.dataset import DatasetVideo
from xtrace_spike.ingest.dedupe import DEFAULT_HAMMING_THRESHOLD, dedupe_frames
from xtrace_spike.ingest.frames import (
    DEFAULT_FRAMES_PER_VIDEO,
    DEFAULT_SCALE_WIDTH,
    ExtractedFrame,
    extract_frames,
)
from xtrace_spike.sampling import AdaptiveSamplingPolicy, select_representative_frames
from xtrace_spike.vectorstore.base import (
    FrameRecord,
    VectorStore,
    VideoIndexMetadataProvider,
)

logger = logging.getLogger(__name__)

#: Namespace UUID estable para derivar `video_id` del `local_ref` (FR-008).
_VIDEO_ID_NAMESPACE = uuid.UUID("6d9f0c0e-2c4a-4b7e-8f1a-3d5b7c9e0f2a")

#: Namespace UUID estable para derivar `frame_id` de (video_id, frame_seq).
_FRAME_ID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")

#: Paridad con PgVectorStore (PR-007): los frames sin timestamp usan como
#: `frame_seq` un ordinal en [1e9, 2^31) para no colisionar con timestamps.
_NULL_TS_SEQ_OFFSET = 1_000_000_000


def video_id_for(local_ref: str) -> str:
    """Id de vídeo estable derivado del `local_ref` (FR-008: clave estable).

    Determinista (uuid5): el mismo dataset produce siempre los mismos ids,
    por lo que reindexar re-upserta las mismas claves (SC-005).
    """
    return str(uuid.uuid5(_VIDEO_ID_NAMESPACE, local_ref))


def frame_id_for(video_id: str, frame_seq: int) -> str:
    """Id de frame estable derivado de (video_id, frame_seq) (FR-008).

    `frame_seq` es el timestamp_ms del frame o, si no existe, un ordinal
    desplazado (paridad con PgVectorStore, `_NULL_TS_SEQ_OFFSET`).
    """
    return str(uuid.uuid5(_FRAME_ID_NAMESPACE, f"{video_id}:{frame_seq}"))


def _frame_seq(frame: ExtractedFrame, no_timestamp_ordinal: int) -> int:
    """Clave de posición del frame (paridad con PgVectorStore, PR-007)."""
    if frame.timestamp_ms is not None:
        return frame.timestamp_ms
    return _NULL_TS_SEQ_OFFSET + no_timestamp_ordinal


@dataclass(frozen=True)
class IndexingConfig:
    """Configuración de la indexación (por defectos alineados con PR-008/009).

    Atributos:
        frames_per_video: frames representativos por vídeo (FR-002).
        dedupe_threshold: umbral de Hamming del dedupe (FR-003, ADR-0005).
        batch_size: tamaño de lote del embedding (FR-005).
        scale_width: anchura de los frames extraídos; None = sin escalar.
        sampling: ``legacy_fixed`` (30) o ``adaptive`` (1..8).
        max_frames: techo adaptativo (1..8).
        target_interval_ms: intervalo objetivo adaptativo.
    """

    frames_per_video: int = DEFAULT_FRAMES_PER_VIDEO
    dedupe_threshold: int = DEFAULT_HAMMING_THRESHOLD
    batch_size: int = 64
    scale_width: int | None = DEFAULT_SCALE_WIDTH
    sampling: str = "legacy_fixed"
    sampling_mode: str | None = None
    max_frames: int = 8
    target_interval_ms: int = 120_000

    @property
    def mode(self) -> str:
        """Effective policy mode; ``sampling_mode`` is a compatibility alias."""
        return self.sampling_mode or self.sampling

    @property
    def adaptive_policy(self) -> AdaptiveSamplingPolicy | None:
        """Return the validated adaptive policy, or ``None`` for legacy mode."""
        if self.mode != "adaptive":
            return None
        return AdaptiveSamplingPolicy(
            target_interval_ms=self.target_interval_ms,
            max_frames=self.max_frames,
        )

    def validate(self) -> None:
        """Configuración inválida -> ValueError (falla pronto, antes de FFmpeg)."""
        if self.frames_per_video <= 0:
            raise ValueError(f"frames_per_video debe ser > 0 (recibido {self.frames_per_video})")
        if not 0 <= self.dedupe_threshold <= PHASH_BITS:
            raise ValueError(
                f"dedupe_threshold debe estar en [0, {PHASH_BITS}] "
                f"(recibido {self.dedupe_threshold})"
            )
        if self.batch_size <= 0:
            raise ValueError(f"batch_size debe ser > 0 (recibido {self.batch_size})")
        if self.scale_width is not None and self.scale_width <= 0:
            raise ValueError(f"scale_width debe ser > 0 o None (recibido {self.scale_width})")
        if self.mode not in {"legacy_fixed", "adaptive"}:
            raise ValueError("sampling debe ser 'legacy_fixed' o 'adaptive'")
        if not 1 <= self.max_frames <= 8:
            raise ValueError("max_frames debe estar en [1, 8]")
        if self.target_interval_ms <= 0:
            raise ValueError("target_interval_ms debe ser > 0")
        if self.mode == "adaptive":
            _ = self.adaptive_policy  # force policy validation


@dataclass(frozen=True)
class VideoIndexingResult:
    """Resultado de indexar un vídeo (para logs y el reporte agregado, FR-007)."""

    local_ref: str
    status: str  # STATUS_INDEXED | STATUS_FAILED
    frame_count: int
    error: str | None = None


@dataclass(frozen=True)
class IndexingReport:
    """Resumen de una ejecución (formato del contrato CLI §1, PR-011)."""

    videos_indexed: int
    videos_failed: int
    frames: int
    vectors: int
    results: tuple[VideoIndexingResult, ...] = ()


class IndexingPipeline:
    """Orquesta ingest→dedupe→embed(batch)→upsert por vídeo (PR-010 · FR-005..009).

    Depende solo de interfaces (ADR-0007): `VectorStore`, `EmbeddingProvider`
    y `VideoStateStore` se inyectan por constructor. La llamada típica:

        report = await IndexingPipeline(
            store=store, embeddings=embeddings, video_states=states,
            config=IndexingConfig(...),
        ).index_dataset(scan_dataset(root), work_root="/tmp/xtrace-work")
    """

    def __init__(
        self,
        *,
        store: VectorStore,
        embeddings: EmbeddingProvider,
        video_states: VideoStateStore | None = None,
        config: IndexingConfig | None = None,
    ) -> None:
        if config is None:
            config = IndexingConfig()
        config.validate()
        self._store = store
        self._embeddings = embeddings
        self._video_states = video_states or InMemoryVideoStateStore()
        self._config = config
        self._writer = VideoIndexWriter(store=store, video_states=self._video_states)

    async def index_dataset(
        self, videos: Sequence[DatasetVideo], *, work_root: str | Path
    ) -> IndexingReport:
        """Indexa todos los vídeos; un fallo por vídeo no aborta el resto.

        Fase 1: marca cada vídeo como `discovered` (FR-007). Fase 2: procesa
        cada vídeo (`indexing -> indexed | failed`). Los temporales de la
        ejecución se eliminan en finally (FR-009/SC-006/ADR-0006); los de cada
        vídeo los borra `extract_frames` al salir de su context manager.
        """
        work_dir = Path(work_root)
        work_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="xtrace-index-", dir=work_dir))
        results: list[VideoIndexingResult] = []
        try:
            for video in videos:
                await self._reconcile_state_from_index(
                    video_id_for(video.local_ref), video.local_ref
                )
                await self._video_states.mark_discovered(
                    video_id_for(video.local_ref), video.local_ref
                )
            for video in videos:
                results.append(await self._index_video(video, work_root=run_dir))
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

        indexed = [result for result in results if result.status == STATUS_INDEXED]
        stats = await self._store.stats()
        return IndexingReport(
            videos_indexed=len(indexed),
            videos_failed=len(results) - len(indexed),
            frames=sum(result.frame_count for result in indexed),
            vectors=stats["vectors"],
            results=tuple(results),
        )

    async def index_video(
        self, video: DatasetVideo, *, work_root: str | Path
    ) -> VideoIndexingResult:
        """Indexa un vídeo individual (discovered -> indexing -> indexed|failed)."""
        video_id = video_id_for(video.local_ref)
        await self._reconcile_state_from_index(video_id, video.local_ref)
        await self._video_states.mark_discovered(video_id, video.local_ref)
        return await self._index_video(video, work_root=Path(work_root))

    async def _reconcile_state_from_index(self, video_id: str, local_ref: str) -> None:
        """Hydrate a fresh local state store from an existing public index API.

        PostgreSQL does not expose this optional capability because its writer
        commits frames and video metadata together; skipping the read-back
        keeps that path transactionally single-source and query-free.
        """
        if not isinstance(self._store, VideoIndexMetadataProvider):
            return
        metadata = await self._store.get_video_index_metadata(video_id)
        if metadata is None:
            return
        await self._video_states.reconcile_index_metadata(
            video_id,
            local_ref,
            frame_count=metadata["frame_count"],
            duration_ms=metadata["duration_ms"],
        )

    async def _index_video(self, video: DatasetVideo, *, work_root: Path) -> VideoIndexingResult:
        """Cadena por vídeo: extract -> dedupe -> embed(batch) -> upsert.

        Todo el procesamiento ocurre dentro de `extract_frames` (cleanup
        garantizado de los PNG temporales, PR-008); cualquier excepción marca
        el vídeo como `failed` con el error y no aborta el dataset (FR-001
        US1 esc. 3). Si falla también la marca de estado, el error original
        prevalece (la cadena de estado es sistémica, no por vídeo).
        """
        video_id = video_id_for(video.local_ref)
        try:
            await self._video_states.mark_indexing(video_id)
            duration_ms: int | None = None
            kept_count = 0
            with extract_frames(
                video,
                work_root=work_root,
                frames_per_video=self._config.frames_per_video,
                scale_width=self._config.scale_width,
                sampling_policy=self._config.adaptive_policy,
            ) as extraction:
                duration_ms = extraction.probe.duration_ms
                deduped = dedupe_frames(extraction.frames, threshold=self._config.dedupe_threshold)
                representative_frames = deduped
                if self._config.adaptive_policy is not None:
                    representative_frames = tuple(
                        select_representative_frames(
                            deduped,
                            duration_ms=duration_ms,
                            timestamp=lambda frame: frame.timestamp_ms,
                            policy=self._config.adaptive_policy,
                        )
                    )
                kept_count = len(representative_frames)
                records = self._embed_frames(video_id, representative_frames)
            await self._writer.replace_video_index(video_id, records, duration_ms=duration_ms)
            logger.info("vídeo indexado local_ref=%s frames=%d", video.local_ref, kept_count)
            return VideoIndexingResult(
                local_ref=video.local_ref, status=STATUS_INDEXED, frame_count=kept_count
            )
        except Exception as exc:
            error = _describe_error(exc)
            logger.warning("vídeo fallido local_ref=%s error=%s", video.local_ref, error)
            try:
                await self._video_states.mark_failed(video_id, error)
                mark_store_failed = getattr(self._store, "mark_video_failed", None)
                if callable(mark_store_failed):
                    await mark_store_failed(video_id)
            except Exception as state_exc:
                raise exc from state_exc
            return VideoIndexingResult(
                local_ref=video.local_ref, status=STATUS_FAILED, frame_count=0, error=error
            )

    def _embed_frames(self, video_id: str, frames: Sequence[ExtractedFrame]) -> list[FrameRecord]:
        """Embedding por lotes (FR-005) y construcción de los FrameRecord (FR-006).

        Los frames se agrupan en batches de `batch_size`; las imágenes se
        abren/cierran por lote y el proveedor debe devolver shape (N, D). Los
        ids de frame son estables (FR-008) y los embeddings se convierten a
        float nativo (serializable y compatible con el contrato).

        El pHash de cada frame (FR-004, FIX-phash) se calcula con
        `compute_phash` sobre la misma imagen ya abierta del lote (sin
        relectura de disco) y viaja en el FrameRecord para que el índice lo
        persista. Es la misma función que usa el dedupe (PR-009); su valor
        interno no es accesible desde aquí (ingest/dedupe.py queda fuera del
        alcance de FIX-phash), así que el coste extra es solo el DCT, no una
        nueva apertura del fichero.
        """
        records: list[FrameRecord] = []
        no_timestamp_ordinal = 0
        batch_size = self._config.batch_size
        for start in range(0, len(frames), batch_size):
            chunk = frames[start : start + batch_size]
            images = [Image.open(frame.path) for frame in chunk]
            try:
                vectors = self._embeddings.embed_images(images)
                self._check_embedding_shape(vectors, len(chunk))
                phashes = [compute_phash(image) for image in images]
            finally:
                for image in images:
                    image.close()
            for frame, vector, phash in zip(chunk, vectors, phashes, strict=True):
                frame_seq = _frame_seq(frame, no_timestamp_ordinal)
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
        """Valida el contrato del proveedor: shape (N, D) con D = dimension."""
        expected = (count, self._embeddings.dimension)
        if vectors.shape != expected:
            raise ValueError(
                f"EmbeddingProvider devolvió shape {vectors.shape}; se esperaba {expected}"
            )


def _describe_error(exc: Exception) -> str:
    """Mensaje corto del error para `videos.error` (FR-007)."""
    return f"{type(exc).__name__}: {exc}"
