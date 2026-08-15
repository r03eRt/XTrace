"""Tests de integración del pipeline de búsqueda por imagen (PR-012 · FR-010
· FR-012 parcial · contracts §1).

Criterios verificables (tasks.md PR-012 · spec 001):
- Captura exacta de un vídeo indexado → aparece como primer candidato
  (FR-010 · US2 esc. 1): distancia 0.0 con FakeEmbeddingProvider (embedding
  determinista de los píxeles, PR-002) y timestamp del frame coincidente
  (FR-012 parcial).
- Agrupación correcta por video_id: un candidato por vídeo con nº de frames
  coincidentes, mejor distancia y timestamp del mejor frame; orden por
  distancia (contracts §2).
- `top_k` configurable (constructor y por llamada) y validado (<= 0).
- El pHash de la consulta viaja en el resultado para el ranking (PR-013).
- La búsqueda no escribe artefactos temporales (ADR-0006): la captura de
  consulta se genera con `extract_frames` (misma cadena que la indexación)
  y su directorio se limpia en finally (FR-009/SC-006).

Los tests usan `InMemoryVectorStore` + `FakeEmbeddingProvider`
(deterministas, sin DB ni Torch; ADR-0007) y se skippean si ffmpeg/ffprobe no
están disponibles (mismo patrón que test_indexing_pipeline.py).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from tests.fixtures import make_test_video
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.hashing.phash import compute_phash
from xtrace_spike.indexing import (
    IndexingConfig,
    IndexingPipeline,
    InMemoryVideoStateStore,
    frame_id_for,
    video_id_for,
)
from xtrace_spike.ingest.dataset import DatasetVideo, scan_dataset
from xtrace_spike.ingest.frames import ExtractedFrame, extract_frames
from xtrace_spike.search import (
    ImageSearch,
    VideoCandidate,
    group_hits_by_video,
    normalize_query_image,
)
from xtrace_spike.vectorstore.base import FrameHit
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore

_EMBEDDING_DIMENSION = 64
_FRAMES_PER_VIDEO = 5
_SCALE_WIDTH = 256  # paridad con IndexingConfig (DEFAULT_SCALE_WIDTH, PR-008)


def _require_ffmpeg() -> None:
    """Skip cuando ffmpeg/ffprobe no están disponibles (p. ej. CI sin FFmpeg)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe no están disponibles en este entorno")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del pipeline (sin pytest-asyncio, estilo PR-003)."""
    return asyncio.run(coro)


def _dataset_videos(tmp_path: Path) -> tuple[DatasetVideo, ...]:
    """Dataset fixture determinista (PR-008): 2 vídeos testsrc2."""
    root = tmp_path / "dataset"
    root.mkdir()
    make_test_video(root / "a.mp4")
    make_test_video(root / "b.mp4", duration_s=1.5, size="160x120", rate=30)
    return scan_dataset(root)


def _indexed_store(videos: tuple[DatasetVideo, ...], tmp_path: Path) -> InMemoryVectorStore:
    """Indexa el dataset con el pipeline real (PR-010) y devuelve el store.

    `dedupe_threshold=0`: solo se eliminan duplicados exactos; testsrc2
    produce frames todos distintos, así que cada vídeo deja exactamente
    `_FRAMES_PER_VIDEO` frames (conteos deterministas para las aserciones).
    """
    store = InMemoryVectorStore()
    report = _run(
        IndexingPipeline(
            store=store,
            embeddings=FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            video_states=InMemoryVideoStateStore(),
            config=IndexingConfig(
                frames_per_video=_FRAMES_PER_VIDEO,
                dedupe_threshold=0,
                batch_size=4,
                scale_width=_SCALE_WIDTH,
            ),
        ).index_dataset(videos, work_root=tmp_path / "work")
    )
    assert report.videos_indexed == len(videos)
    assert report.videos_failed == 0
    return store


def _open_loaded(path: Path) -> Image.Image:
    """Abre la imagen cargándola en memoria y cerrando el fichero.

    Necesario porque los PNG de `extract_frames` son temporales: el
    directorio se borra al salir del context manager (FR-009/SC-006).
    """
    with Image.open(path) as image:
        image.load()
        return image.copy()


def _exact_capture(video: DatasetVideo, work_root: Path) -> tuple[Image.Image, ExtractedFrame]:
    """Captura exacta de `video`: frame re-extraído con la misma cadena.

    La extracción de PR-008 es determinista (testsrc2 + mismos parámetros
    ffmpeg), así que el frame es idéntico (mismos píxeles) al indexado: con
    FakeEmbeddingProvider el embedding coincide y la distancia coseno es 0.0.
    """
    with extract_frames(
        video,
        work_root=work_root,
        frames_per_video=_FRAMES_PER_VIDEO,
        scale_width=_SCALE_WIDTH,
    ) as extraction:
        frame = extraction.frames[0]
        return _open_loaded(frame.path), frame


def _searcher(store: InMemoryVectorStore, *, top_k: int | None = None) -> ImageSearch:
    return ImageSearch(
        store=store,
        embeddings=FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
        **({"top_k": top_k} if top_k is not None else {}),
    )


# ---------------------------------------------------------------------------
# FR-010 · US2 esc. 1: captura exacta de un vídeo indexado -> aparece
# ---------------------------------------------------------------------------


def test_exact_capture_of_indexed_video_appears(tmp_path: Path) -> None:
    """US2 esc. 1: captura exacta -> el vídeo correcto es el primer candidato."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)
    target = videos[0]
    target_id = video_id_for(target.local_ref)

    capture, capture_frame = _exact_capture(target, tmp_path / "capture")
    result = _run(_searcher(store).search_image(capture))

    # El pHash de la consulta viaja en el resultado para el ranking (PR-013).
    assert result.query_phash == compute_phash(capture)
    assert result.query_phash >= 0
    assert result.total_hits >= 1

    top = result.candidates[0]
    assert top.video_id == target_id
    assert top.best_distance == 0.0  # embedding idéntico (fake determinista)
    assert top.best_frame_id == frame_id_for(target_id, capture_frame.timestamp_ms)
    assert top.best_timestamp_ms == capture_frame.timestamp_ms
    assert top.matching_frames >= 1
    # Los candidatos se ordenan por mejor distancia (contracts §2).
    distances = [c.best_distance for c in result.candidates]
    assert distances == sorted(distances)


def test_exact_capture_is_top_result_with_multiple_videos(tmp_path: Path) -> None:
    """US2 esc. 1 con 2 vídeos: el correcto gana y el resto tiene peor distancia."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)
    target = videos[0]

    capture, _ = _exact_capture(target, tmp_path / "capture")
    result = _run(_searcher(store, top_k=100).search_image(capture))

    assert result.total_hits == len(videos) * _FRAMES_PER_VIDEO
    assert result.candidates[0].video_id == video_id_for(target.local_ref)
    assert result.candidates[0].best_distance == 0.0
    assert all(c.best_distance > 0.0 for c in result.candidates[1:])


# ---------------------------------------------------------------------------
# FR-010 · agrupación correcta por video_id
# ---------------------------------------------------------------------------


def test_grouping_by_video_id(tmp_path: Path) -> None:
    """Un candidato por vídeo: nº de frames, mejor distancia y timestamp."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)
    target = videos[0]
    expected_ids = {video_id_for(video.local_ref) for video in videos}

    capture, _ = _exact_capture(target, tmp_path / "capture")
    result = _run(_searcher(store, top_k=100).search_image(capture))

    # top_k >= total de vectores -> todos los frames del índice son hits.
    assert result.total_hits == len(videos) * _FRAMES_PER_VIDEO
    assert len(result.candidates) == len(expected_ids)
    assert {c.video_id for c in result.candidates} == expected_ids
    # Cada hit pertenece a exactamente un candidato (conteo íntegro).
    assert sum(c.matching_frames for c in result.candidates) == result.total_hits
    assert all(c.matching_frames >= 1 for c in result.candidates)
    # Orden por mejor distancia ascendente.
    distances = [c.best_distance for c in result.candidates]
    assert distances == sorted(distances)


def test_grouping_preserves_best_frame_per_video(tmp_path: Path) -> None:
    """El mejor frame del grupo determina distancia y timestamp (FR-012)."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)
    target = videos[0]
    target_id = video_id_for(target.local_ref)

    capture, capture_frame = _exact_capture(target, tmp_path / "capture")
    result = _run(_searcher(store).search_image(capture))
    top = result.candidates[0]

    # El mejor frame del vídeo correcto es el de la captura (distancia 0).
    assert top.best_distance == 0.0
    assert top.best_timestamp_ms == capture_frame.timestamp_ms
    assert top.best_frame_id == frame_id_for(target_id, capture_frame.timestamp_ms)
    # Los demás hits del mismo vídeo (si los hay en top_k) se cuentan.
    assert top.matching_frames >= 1


# ---------------------------------------------------------------------------
# FR-010 · top_k configurable y validado
# ---------------------------------------------------------------------------


def test_top_k_configurable_constructor_and_call(tmp_path: Path) -> None:
    """`top_k` limita los hits del ANN; el override por llamada gana."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)
    capture, _ = _exact_capture(videos[0], tmp_path / "capture")

    one = _run(_searcher(store, top_k=1).search_image(capture))
    assert one.total_hits == 1
    assert len(one.candidates) == 1
    assert one.candidates[0].best_distance == 0.0

    three = _run(_searcher(store, top_k=3).search_image(capture))
    assert three.total_hits == 3
    assert sum(c.matching_frames for c in three.candidates) == 3

    overridden = _run(_searcher(store, top_k=3).search_image(capture, top_k=5))
    assert overridden.total_hits == 5

    with pytest.raises(ValueError, match="top_k"):
        ImageSearch(
            store=store,
            embeddings=FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
            top_k=0,
        )
    with pytest.raises(ValueError, match="top_k"):
        _run(_searcher(store).search_image(capture, top_k=0))


def test_ann_order_by_distance_is_kept(tmp_path: Path) -> None:
    """El orden por distancia del ANN se mantiene en los candidatos (contracts §2)."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)
    capture, _ = _exact_capture(videos[0], tmp_path / "capture")

    result = _run(_searcher(store, top_k=100).search_image(capture))
    for candidate in result.candidates:
        assert candidate.best_distance >= 0.0
    assert [c.best_distance for c in result.candidates] == sorted(
        c.best_distance for c in result.candidates
    )


# ---------------------------------------------------------------------------
# FR-010 · consultas fuera del índice y normalización
# ---------------------------------------------------------------------------


def test_unrelated_query_does_not_match_exactly(tmp_path: Path) -> None:
    """Imagen monocroma ajena al índice: candidatos sin distancia 0 (sin falso exacto)."""
    _require_ffmpeg()
    videos = _dataset_videos(tmp_path)
    store = _indexed_store(videos, tmp_path)

    capture = Image.new("RGB", (256, 192), (255, 0, 0))
    result = _run(_searcher(store, top_k=100).search_image(capture))

    assert result.total_hits == len(videos) * _FRAMES_PER_VIDEO
    assert len(result.candidates) == len(videos)
    assert all(c.best_distance > 0.0 for c in result.candidates)
    assert result.query_phash == compute_phash(capture)


def test_normalize_query_image_is_rgb_without_resize() -> None:
    """Normalización: solo RGB; el resize es responsabilidad del proveedor (ADR-0007)."""
    rgba = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
    gray = Image.new("L", (4, 4), 128)
    assert normalize_query_image(rgba).mode == "RGB"
    assert normalize_query_image(gray).mode == "RGB"

    original_size = (37, 23)
    normalized = normalize_query_image(Image.new("RGB", original_size))
    assert normalized.mode == "RGB"
    assert normalized.size == original_size  # sin resize duplicado del provider


# ---------------------------------------------------------------------------
# FR-010 · agrupación (función pura, casos sintéticos)
# ---------------------------------------------------------------------------


def test_group_hits_by_video_pure() -> None:
    """Agrupación pura: conteo, mejor frame, orden por distancia y tie-break."""
    hits: list[FrameHit] = [
        {"frame_id": "f1", "video_id": "A", "timestamp_ms": 100, "distance": 0.1},
        {"frame_id": "f2", "video_id": "A", "timestamp_ms": 200, "distance": 0.05},
        {"frame_id": "f3", "video_id": "B", "timestamp_ms": 400, "distance": 0.15},
        {"frame_id": "f4", "video_id": "B", "timestamp_ms": 500, "distance": 0.2},
    ]
    candidates = group_hits_by_video(hits)
    assert len(candidates) == 2
    first, second = candidates
    assert first == VideoCandidate(
        video_id="A",
        matching_frames=2,
        best_distance=0.05,
        best_frame_id="f2",
        best_timestamp_ms=200,
    )
    assert second == VideoCandidate(
        video_id="B",
        matching_frames=2,
        best_distance=0.15,
        best_frame_id="f3",
        best_timestamp_ms=400,
    )
    assert group_hits_by_video([]) == ()


def test_group_best_frame_without_timestamp() -> None:
    """FR-012: el mejor frame sin timestamp -> best_timestamp_ms None (no falla)."""
    hits: list[FrameHit] = [
        {"frame_id": "f1", "video_id": "A", "timestamp_ms": 100, "distance": 0.3},
        {"frame_id": "f2", "video_id": "A", "timestamp_ms": None, "distance": 0.1},
    ]
    candidates = group_hits_by_video(hits)
    assert candidates[0].best_frame_id == "f2"
    assert candidates[0].best_timestamp_ms is None
    assert candidates[0].matching_frames == 2
