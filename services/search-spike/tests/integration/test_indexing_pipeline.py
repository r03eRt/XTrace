"""Tests de integración del pipeline de indexación (PR-010 · FR-005/006/007/008/009
· SC-005/006 · ADR-0006/0007).

Criterios verificables (tasks.md PR-010 · spec 001):
- End-to-end sobre fixtures: el dataset queda `indexed` con frames+vectores
  (FR-005/006/007 · US1 esc. 1).
- Reindexar NO duplica frames (FR-008/SC-005): mismo dataset y configuración
  -> mismo número de frames/vectores y mismos ids estables (SC-007 mindset).
- Sin temporales tras éxito o fallo (FR-009/SC-006/ADR-0006).
- Estado del vídeo: discovered -> indexing -> indexed | failed (FR-007).
- Embedding por lotes con `batch_size` configurable (FR-005).
- Un vídeo corrupto o con error de embedding se marca `failed` y el resto
  del dataset continúa (FR-001 US1 esc. 3).

Los tests usan `InMemoryVectorStore` + `FakeEmbeddingProvider` +
`InMemoryVideoStateStore` (deterministas, sin DB ni Torch; ADR-0007). El
estado real sobre PostgreSQL (`PgVideoStateStore`) se valida en un test
opcional que se skippea si la DB local no es alcanzable (mismo patrón que
test_pgvector_store.py). Los tests de vídeo se skippean si ffmpeg/ffprobe no
están disponibles (mismo patrón que test_ingest.py).
"""

from __future__ import annotations

import asyncio
import math
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from PIL import Image

from tests.fixtures import make_corrupt_video, make_test_video
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.hashing.phash import compute_phash
from xtrace_spike.indexing import (
    IndexingConfig,
    IndexingPipeline,
    InMemoryVideoStateStore,
    PgVideoStateStore,
    frame_id_for,
    video_id_for,
)
from xtrace_spike.ingest.dataset import scan_dataset
from xtrace_spike.ingest.dedupe import dedupe_frames
from xtrace_spike.ingest.frames import extract_frames
from xtrace_spike.repo import resolve_dsn
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore

_EMBEDDING_DIMENSION = 64


class _BatchSpyProvider(FakeEmbeddingProvider):
    """Fake provider que registra los tamaños de lote y puede fallar a demanda.

    Permite verificar FR-005 (embedding por lotes con `batch_size`) y el
    manejo de un fallo del proveedor a mitad de indexación (FR-001 esc. 3).
    """

    def __init__(
        self, *, dimension: int = _EMBEDDING_DIMENSION, fail_on_call: int | None = None
    ) -> None:
        super().__init__(dimension=dimension)
        self.batch_sizes: list[int] = []
        self.call_count = 0
        self._fail_on_call = fail_on_call

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray[Any, Any]:
        self.call_count += 1
        if self._fail_on_call is not None and self.call_count == self._fail_on_call:
            raise RuntimeError("embedding explosion (simulado)")
        self.batch_sizes.append(len(images))
        return super().embed_images(images)


class _RecordingStateStore(InMemoryVideoStateStore):
    """Estado en memoria que registra las transiciones en orden (FR-007)."""

    def __init__(self) -> None:
        super().__init__()
        self.transitions: list[tuple[str, str]] = []

    async def mark_discovered(self, video_id: str, local_ref: str) -> None:
        self.transitions.append((video_id, "discovered"))
        await super().mark_discovered(video_id, local_ref)

    async def mark_indexing(self, video_id: str) -> None:
        self.transitions.append((video_id, "indexing"))
        await super().mark_indexing(video_id)

    async def mark_indexed(
        self, video_id: str, *, frame_count: int, duration_ms: int | None
    ) -> None:
        self.transitions.append((video_id, "indexed"))
        await super().mark_indexed(video_id, frame_count=frame_count, duration_ms=duration_ms)

    async def mark_failed(self, video_id: str, error: str) -> None:
        self.transitions.append((video_id, "failed"))
        await super().mark_failed(video_id, error)


def _require_ffmpeg() -> None:
    """Skip cuando ffmpeg/ffprobe no están disponibles (p. ej. CI sin FFmpeg)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe no están disponibles en este entorno")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del pipeline (sin pytest-asyncio, estilo PR-003)."""
    return asyncio.run(coro)


def _query() -> list[float]:
    """Vector de consulta unitario (dimensión del fake provider)."""
    return [1.0] + [0.0] * (_EMBEDDING_DIMENSION - 1)


def _dataset_root(tmp_path: Path, *, with_corrupt: bool = False) -> Path:
    """Dataset fixture determinista (PR-008): 2 vídeos testsrc2 + corrupto opcional."""
    root = tmp_path / "dataset"
    root.mkdir()
    make_test_video(root / "a.mp4")
    make_test_video(root / "b.mp4", duration_s=1.5, size="160x120", rate=30)
    if with_corrupt:
        make_corrupt_video(root / "corrupt.mp4")
    return root


def _pipeline(
    store: InMemoryVectorStore,
    embeddings: FakeEmbeddingProvider,
    states: InMemoryVideoStateStore | None = None,
    **config: Any,
) -> IndexingPipeline:
    """Pipeline con la configuración del test (DI: stores deterministas, ADR-0007)."""
    return IndexingPipeline(
        store=store,
        embeddings=embeddings,
        video_states=states or InMemoryVideoStateStore(),
        config=IndexingConfig(**config),
    )


# ---------------------------------------------------------------------------
# FR-005/006/007 · end-to-end: dataset fixture -> indexed con frames+vectores
# ---------------------------------------------------------------------------


def test_index_dataset_indexes_all_videos_end_to_end(tmp_path: Path) -> None:
    """US1 esc. 1: cada vídeo queda `indexed` con frames y vectores coherentes."""
    _require_ffmpeg()
    root = _dataset_root(tmp_path)
    videos = scan_dataset(root)
    store = InMemoryVectorStore()
    states = InMemoryVideoStateStore()

    report = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            frames_per_video=5,
            dedupe_threshold=10,
            batch_size=2,
        ).index_dataset(videos, work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 2
    assert report.videos_failed == 0
    assert [r.status for r in report.results] == ["indexed", "indexed"]
    assert all(0 < r.frame_count <= 5 for r in report.results)
    assert report.frames == report.vectors == _run(store.stats())["frames"]
    assert _run(store.stats()) == {
        "videos": 2,
        "frames": report.frames,
        "vectors": report.vectors,
    }
    for video in videos:
        assert _run(states.status(video_id_for(video.local_ref))) == "indexed"

    hits = _run(store.ann_search(_query(), k=100))
    assert {h["video_id"] for h in hits} == {video_id_for(v.local_ref) for v in videos}
    assert all(h["timestamp_ms"] is not None for h in hits)


# ---------------------------------------------------------------------------
# FIX-phash · FR-004/FR-006 · el FrameRecord lleva el pHash REAL del frame
# ---------------------------------------------------------------------------


def test_pipeline_persists_real_phash_per_frame(tmp_path: Path) -> None:
    """FIX-phash · FR-004/FR-006: el pHash almacenado es el real (no 0).

    Se compara cada FrameRecord persistido contra el pHash calculado por
    compute_phash sobre el frame, en una extracción + dedupe independiente
    con la misma configuración del pipeline (mismos frame_ids estables,
    FR-008): el índice conserva la firma perceptual real de cada frame
    representativo.
    """
    _require_ffmpeg()
    root = tmp_path / "dataset"
    root.mkdir()
    make_test_video(root / "a.mp4")
    video = scan_dataset(root)[0]
    video_id = video_id_for(video.local_ref)

    expected: dict[str, int] = {}
    with extract_frames(video, work_root=tmp_path / "work", frames_per_video=5) as extraction:
        for frame in dedupe_frames(extraction.frames):
            with Image.open(frame.path) as image:
                expected[frame_id_for(video_id, frame.timestamp_ms)] = compute_phash(image)
    assert expected, "el fixture debe producir al menos un frame representativo"

    store = InMemoryVectorStore()
    report = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            frames_per_video=5,
        ).index_dataset([video], work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 1
    assert report.frames == len(expected)
    for frame_id, real_phash in expected.items():
        stored = _run(store.get_frame(frame_id))
        assert stored is not None, f"frame {frame_id} no indexado"
        assert stored["phash"] == real_phash
        assert stored["phash"] != 0  # nunca el centinela 0


# ---------------------------------------------------------------------------
# FR-008/SC-005 · idempotencia: reindexar no duplica frames
# ---------------------------------------------------------------------------


def test_reindex_does_not_duplicate_frames(tmp_path: Path) -> None:
    """SC-005: dos ejecuciones sobre el mismo dataset -> mismos frames/vectores."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path))
    store = InMemoryVectorStore()
    states = InMemoryVideoStateStore()
    work_root = tmp_path / "work"

    def run() -> Any:
        return _run(
            _pipeline(
                store,
                FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
                states,
                frames_per_video=5,
                batch_size=4,
            ).index_dataset(videos, work_root=work_root)
        )

    first = run()
    second = run()

    assert second.videos_indexed == 2
    assert second.videos_failed == 0
    assert second.frames == first.frames > 0
    assert _run(store.stats()) == {
        "videos": 2,
        "frames": first.frames,
        "vectors": first.vectors,
    }
    hits_first = _run(store.ann_search(_query(), k=100))
    hits_second = _run(store.ann_search(_query(), k=100))
    assert [(h["frame_id"], h["video_id"], h["timestamp_ms"]) for h in hits_first] == [
        (h["frame_id"], h["video_id"], h["timestamp_ms"]) for h in hits_second
    ]
    for video in videos:
        assert _run(states.status(video_id_for(video.local_ref))) == "indexed"


def test_adaptive_reindex_replaces_legacy_frames_atomically(tmp_path: Path) -> None:
    """FR-001/002/003/010: adaptativo elimina frames legacy sobrantes."""
    _require_ffmpeg()
    video = scan_dataset(_dataset_root(tmp_path))[0]
    store = InMemoryVectorStore()
    states = InMemoryVideoStateStore()
    work_root = tmp_path / "work"

    legacy = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            frames_per_video=5,
        ).index_video(video, work_root=work_root)
    )
    assert legacy.status == "indexed"
    assert legacy.frame_count > 1

    adaptive = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            sampling="adaptive",
        ).index_video(video, work_root=work_root)
    )

    assert adaptive.status == "indexed"
    assert 1 <= adaptive.frame_count <= 8
    assert adaptive.frame_count < legacy.frame_count
    assert _run(store.stats()) == {
        "videos": 1,
        "frames": adaptive.frame_count,
        "vectors": adaptive.frame_count,
    }
    assert _run(states.status(video_id_for(video.local_ref))) == "indexed"


def test_failed_reindex_preserves_previous_complete_index(tmp_path: Path) -> None:
    """FR-010/SC-007: fallo posterior no publica un índice parcial."""
    _require_ffmpeg()
    video_path = _dataset_root(tmp_path) / "a.mp4"
    video = scan_dataset(video_path.parent)[0]
    store = InMemoryVectorStore()
    states = InMemoryVideoStateStore()
    work_root = tmp_path / "work"

    first = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            frames_per_video=5,
        ).index_video(video, work_root=work_root)
    )
    assert first.status == "indexed"
    before = _run(store.ann_search(_query(), k=100))
    assert before

    video_path.write_bytes(b"not a video")
    failed = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            sampling="adaptive",
        ).index_video(video, work_root=work_root)
    )

    assert failed.status == "failed"
    assert _run(states.status(video_id_for(video.local_ref))) == "failed"
    assert _run(store.get_video_index(video_id_for(video.local_ref)))["status"] == "failed"
    failed_state = _run(states.snapshot(video_id_for(video.local_ref)))
    assert failed_state is not None
    assert failed_state.frame_count == first.frame_count
    assert failed_state.duration_ms is not None
    after = _run(store.ann_search(_query(), k=100))
    assert [(hit["frame_id"], hit["timestamp_ms"]) for hit in after] == [
        (hit["frame_id"], hit["timestamp_ms"]) for hit in before
    ]


def test_failed_reindex_with_fresh_default_state_hydrates_index_metadata(
    tmp_path: Path,
) -> None:
    """A fresh default state store retains metadata from a reused memory index."""
    _require_ffmpeg()
    video_path = _dataset_root(tmp_path) / "a.mp4"
    video = scan_dataset(video_path.parent)[0]
    store = InMemoryVectorStore()
    work_root = tmp_path / "work"

    first = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            InMemoryVideoStateStore(),
            frames_per_video=5,
        ).index_video(video, work_root=work_root)
    )
    assert first.status == "indexed"
    before = _run(store.ann_search(_query(), k=100))

    video_path.write_bytes(b"not a video")
    fresh_pipeline = IndexingPipeline(
        store=store,
        embeddings=FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
        video_states=None,
        config=IndexingConfig(sampling="adaptive"),
    )
    failed = _run(fresh_pipeline.index_video(video, work_root=work_root))

    assert failed.status == "failed"
    failed_state = _run(fresh_pipeline._video_states.snapshot(video_id_for(video.local_ref)))
    assert failed_state is not None
    assert failed_state.status == "failed"
    assert failed_state.frame_count == first.frame_count
    assert failed_state.duration_ms is not None
    metadata = _run(store.get_video_index(video_id_for(video.local_ref)))
    assert metadata["status"] == "failed"
    assert metadata["frame_count"] == first.frame_count
    assert metadata["duration_ms"] == failed_state.duration_ms

    after = _run(store.ann_search(_query(), k=100))
    assert [(hit["frame_id"], hit["timestamp_ms"]) for hit in after] == [
        (hit["frame_id"], hit["timestamp_ms"]) for hit in before
    ]


def test_video_and_frame_ids_are_stable_across_runs(tmp_path: Path) -> None:
    """FR-008: ids estables (uuid5) aunque se parta de stores nuevos (SC-007)."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path))
    work_root = tmp_path / "work"

    def fresh_run() -> tuple[Any, list[Any]]:
        store = InMemoryVectorStore()
        report = _run(
            _pipeline(
                store,
                FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
                frames_per_video=5,
            ).index_dataset(videos, work_root=work_root)
        )
        return report, _run(store.ann_search(_query(), k=100))

    (report1, hits1) = fresh_run()
    (report2, hits2) = fresh_run()

    assert report1.frames == report2.frames
    assert {h["frame_id"] for h in hits1} == {h["frame_id"] for h in hits2}
    assert {h["video_id"] for h in hits1} == {h["video_id"] for h in hits2}
    # ids estables son UUIDs válidos (compatibles con el esquema PR-006)
    for hit in hits1:
        uuid.UUID(hit["frame_id"])
        uuid.UUID(hit["video_id"])
    assert video_id_for("a.mp4") != video_id_for("b.mp4")
    assert frame_id_for(video_id_for("a.mp4"), 0) == frame_id_for(video_id_for("a.mp4"), 0)


# ---------------------------------------------------------------------------
# FR-001 US1 esc. 3 / FR-007 · fallo de un vídeo: failed + el resto continúa
# ---------------------------------------------------------------------------


def test_corrupt_video_is_marked_failed_and_rest_continues(tmp_path: Path) -> None:
    """Fichero corrupto -> vídeo `failed` con error y el resto del dataset se indexa."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path, with_corrupt=True))
    store = InMemoryVectorStore()
    states = InMemoryVideoStateStore()

    report = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            frames_per_video=5,
        ).index_dataset(videos, work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 2
    assert report.videos_failed == 1
    failed = [r for r in report.results if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].local_ref == "corrupt.mp4"
    assert failed[0].frame_count == 0
    assert failed[0].error is not None and "ProbeError" in failed[0].error
    assert _run(states.status(video_id_for("corrupt.mp4"))) == "failed"
    assert _run(states.status(video_id_for("a.mp4"))) == "indexed"
    assert _run(states.status(video_id_for("b.mp4"))) == "indexed"
    assert _run(store.stats())["videos"] == 2


def test_embedding_error_marks_failed_and_rest_continues(tmp_path: Path) -> None:
    """Fallo del proveedor de embeddings a mitad -> failed + cleanup (FR-009)."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path))
    store = InMemoryVectorStore()
    spy = _BatchSpyProvider(dimension=_EMBEDDING_DIMENSION, fail_on_call=1)

    report = _run(
        _pipeline(
            store,
            spy,
            None,
            frames_per_video=3,
            batch_size=2,
        ).index_dataset(videos, work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 1
    assert report.videos_failed == 1
    failed = [r for r in report.results if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].error is not None and "embedding explosion" in failed[0].error
    # el vídeo fallido no dejó frames en el índice; el otro sí
    assert _run(store.stats()) == {"videos": 1, "frames": report.frames, "vectors": report.vectors}
    assert report.frames > 0
    # sin temporales residuales tras el fallo (SC-006)
    assert list((tmp_path / "work").iterdir()) == []


# ---------------------------------------------------------------------------
# FR-005 · embedding por lotes con batch_size configurable
# ---------------------------------------------------------------------------


def test_embedding_batches_respect_batch_size(tmp_path: Path) -> None:
    """FR-005: los frames deduplicados se embeden en lotes <= batch_size."""
    _require_ffmpeg()
    root = tmp_path / "dataset"
    root.mkdir()
    make_test_video(root / "a.mp4")
    videos = scan_dataset(root)
    store = InMemoryVectorStore()
    spy = _BatchSpyProvider(dimension=_EMBEDDING_DIMENSION)

    report = _run(
        _pipeline(
            store,
            spy,
            None,
            frames_per_video=7,
            batch_size=3,
        ).index_dataset(videos, work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 1
    assert all(size <= 3 for size in spy.batch_sizes)
    assert sum(spy.batch_sizes) == report.frames
    assert len(spy.batch_sizes) == math.ceil(report.frames / 3)


def test_embedding_batch_size_larger_than_frames_single_call(tmp_path: Path) -> None:
    """FR-005: batch_size >= frames del vídeo -> una sola llamada por vídeo."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path))
    spy = _BatchSpyProvider(dimension=_EMBEDDING_DIMENSION)

    report = _run(
        _pipeline(
            InMemoryVectorStore(),
            spy,
            None,
            frames_per_video=7,
            batch_size=64,
        ).index_dataset(videos, work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 2
    assert spy.batch_sizes == [report.results[0].frame_count, report.results[1].frame_count]


# ---------------------------------------------------------------------------
# FR-009/SC-006 · cleanup garantizado de temporales
# ---------------------------------------------------------------------------


def test_no_temporaries_left_after_successful_run(tmp_path: Path) -> None:
    """SC-006: tras indexar sin errores no queda ningún temporal en disco."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path))
    work_root = tmp_path / "work"

    report = _run(
        _pipeline(
            InMemoryVectorStore(),
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            frames_per_video=5,
        ).index_dataset(videos, work_root=work_root)
    )

    assert report.videos_failed == 0
    assert list(work_root.iterdir()) == []


def test_no_temporaries_left_after_failed_run(tmp_path: Path) -> None:
    """SC-006: tras un job con error no queda ningún temporal (ADR-0006)."""
    _require_ffmpeg()
    videos = scan_dataset(_dataset_root(tmp_path, with_corrupt=True))
    work_root = tmp_path / "work"

    report = _run(
        _pipeline(
            InMemoryVectorStore(),
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            frames_per_video=5,
        ).index_dataset(videos, work_root=work_root)
    )

    assert report.videos_failed == 1
    assert list(work_root.iterdir()) == []


# ---------------------------------------------------------------------------
# FR-007 · transiciones de estado discovered -> indexing -> indexed | failed
# ---------------------------------------------------------------------------


def test_state_transition_order_discovered_indexing_indexed(tmp_path: Path) -> None:
    """FR-007: ciclo completo de estado de un vídeo que indexa bien."""
    _require_ffmpeg()
    video = scan_dataset(_dataset_root(tmp_path))[0]
    states = _RecordingStateStore()

    result = _run(
        _pipeline(
            InMemoryVectorStore(),
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            frames_per_video=3,
        ).index_video(video, work_root=tmp_path / "work")
    )

    assert result.status == "indexed"
    video_id = video_id_for(video.local_ref)
    assert states.transitions == [
        (video_id, "discovered"),
        (video_id, "indexing"),
        (video_id, "indexed"),
    ]


def test_state_transition_order_indexing_failed(tmp_path: Path) -> None:
    """FR-007: un vídeo corrupto termina en `failed` (con error registrado)."""
    _require_ffmpeg()
    video = scan_dataset(_dataset_root(tmp_path, with_corrupt=True))[-1]
    states = _RecordingStateStore()

    result = _run(
        _pipeline(
            InMemoryVectorStore(),
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            states,
            frames_per_video=3,
        ).index_video(video, work_root=tmp_path / "work")
    )

    assert result.status == "failed"
    assert result.error is not None
    video_id = video_id_for(video.local_ref)
    assert states.transitions == [
        (video_id, "discovered"),
        (video_id, "indexing"),
        (video_id, "failed"),
    ]


# ---------------------------------------------------------------------------
# Edge cases y configuración
# ---------------------------------------------------------------------------


def test_empty_dataset_produces_empty_report(tmp_path: Path) -> None:
    """Edge case: dataset vacío -> reporte en cero, sin errores ni temporales."""
    root = tmp_path / "dataset"
    root.mkdir()
    store = InMemoryVectorStore()

    report = _run(
        _pipeline(
            store,
            FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
        ).index_dataset(scan_dataset(root), work_root=tmp_path / "work")
    )

    assert report.videos_indexed == 0
    assert report.videos_failed == 0
    assert report.frames == 0
    assert report.vectors == 0
    assert report.results == ()
    assert _run(store.stats()) == {"videos": 0, "frames": 0, "vectors": 0}
    assert list((tmp_path / "work").iterdir()) == []


def test_invalid_config_raises_value_error() -> None:
    """Configuración inválida -> ValueError al construir el pipeline (falla pronto)."""
    store = InMemoryVectorStore()
    embeddings = FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION)
    invalid = [
        IndexingConfig(frames_per_video=0),
        IndexingConfig(batch_size=0),
        IndexingConfig(dedupe_threshold=-1),
        IndexingConfig(dedupe_threshold=65),
        IndexingConfig(scale_width=0),
    ]
    for config in invalid:
        with pytest.raises(ValueError):
            IndexingPipeline(store=store, embeddings=embeddings, config=config)
        with pytest.raises(ValueError):
            config.validate()


# ---------------------------------------------------------------------------
# FR-007 · estado real sobre PostgreSQL (opcional: skip si no hay DB local)
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    """¿Supabase local alcanzable? (DSN por defecto/env, migración PR-006)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


def test_pg_video_state_store_lifecycle(tmp_path: Path) -> None:
    """PgVideoStateStore: transiciones y persistencia reales (FR-007 · PR-006)."""
    if not _db_available():
        pytest.skip("Supabase local no alcanzable (CI sin DB): PgVideoStateStore saltado")
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate table public.frames, public.videos, public.searches cascade")

    states = PgVideoStateStore()
    video_id = video_id_for("pg-lifecycle.mp4")
    assert _run(states.status(video_id)) is None

    _run(states.mark_discovered(video_id, "pg-lifecycle.mp4"))
    assert _run(states.status(video_id)) == "discovered"

    _run(states.mark_indexing(video_id))
    assert _run(states.status(video_id)) == "indexing"

    _run(states.mark_indexed(video_id, frame_count=7, duration_ms=2000))
    assert _run(states.status(video_id)) == "indexed"
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select local_ref, status, frame_count, duration_ms, error "
                "from public.videos where id = %s",
                (uuid.UUID(video_id),),
            )
            row = cur.fetchone()
    assert row == ("pg-lifecycle.mp4", "indexed", 7, 2000, None)

    _run(states.mark_failed(video_id, "ProbeError: boom"))
    assert _run(states.status(video_id)) == "failed"
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select status, error from public.videos where id = %s",
                (uuid.UUID(video_id),),
            )
            row = cur.fetchone()
    assert row == ("failed", "ProbeError: boom")

    # Idempotencia: re-discover reinicia el ciclo sin duplicar filas (FR-008)
    _run(states.mark_discovered(video_id, "pg-lifecycle.mp4"))
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from public.videos where id = %s", (uuid.UUID(video_id),))
            row = cur.fetchone()
    assert row == (1,)
    assert _run(states.status(video_id)) == "discovered"
