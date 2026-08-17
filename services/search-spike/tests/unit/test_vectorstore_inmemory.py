"""Tests de InMemoryVectorStore (PR-003 · FR-006 · ADR-0007 · contracts §2).

Criterios verificables de la tarea:
- `ann_search` ordena por distancia coseno ascendente (menor = más similar).
- `delete_video` elimina los frames del vídeo del índice.
- El filtro `exclude_videos` (True por defecto) oculta los vídeos excluidos.

Los métodos del contrato son async; sin plugin pytest-asyncio (deps fijadas en
PR-001), cada test ejecuta el store con `asyncio.run`.
"""

import asyncio
import math

import pytest

from xtrace_spike.indexing.state import InMemoryVideoStateStore
from xtrace_spike.indexing.writer import AtomicReplacementError, VideoIndexWriter
from xtrace_spike.vectorstore.base import FrameRecord, VectorStore
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore


def _record(
    frame_id: str,
    video_id: str,
    embedding: list[float],
    timestamp_ms: int | None = None,
    phash: int = 0,
) -> FrameRecord:
    return FrameRecord(
        frame_id=frame_id,
        video_id=video_id,
        timestamp_ms=timestamp_ms,
        phash=phash,
        embedding=embedding,
    )


def test_in_memory_store_satisfies_vectorstore_contract() -> None:
    """`InMemoryVectorStore` cumple estructuralmente el contrato y arranca vacío."""
    store: VectorStore = InMemoryVectorStore()
    assert asyncio.run(store.stats()) == {"videos": 0, "frames": 0, "vectors": 0}
    assert asyncio.run(store.ann_search([1.0, 0.0], k=5)) == []


def test_ann_search_orders_by_cosine_distance_ascending() -> None:
    """`ann_search` devuelve los hits ordenados por distancia coseno ascendente."""
    store = InMemoryVectorStore()
    asyncio.run(
        store.upsert_frames(
            [
                _record("f-far", "v1", [1.0, 0.0], timestamp_ms=1000),
                _record("f-near", "v1", [math.sqrt(0.5), math.sqrt(0.5)], timestamp_ms=2000),
                _record("f-mid", "v1", [0.0, 1.0], timestamp_ms=3000),
            ]
        )
    )
    hits = asyncio.run(store.ann_search([1.0, 0.0], k=3))
    assert [h["frame_id"] for h in hits] == ["f-far", "f-near", "f-mid"]
    assert hits[0]["distance"] < hits[1]["distance"] < hits[2]["distance"]
    assert hits[0]["distance"] == pytest.approx(0.0)
    assert hits[1]["distance"] == pytest.approx(1 - math.sqrt(0.5))
    assert hits[2]["distance"] == pytest.approx(1.0)
    # FrameHit completo (contracts §2): frame_id, video_id, timestamp_ms, distance
    assert hits[0]["video_id"] == "v1"
    assert hits[0]["timestamp_ms"] == 1000


def test_ann_search_returns_top_k_nearest() -> None:
    """`k` limita el número de resultados; `k <= 0` no devuelve nada."""
    store = InMemoryVectorStore()
    asyncio.run(
        store.upsert_frames(
            [
                _record("f1", "v1", [1.0, 0.0]),
                _record("f2", "v1", [0.0, 1.0]),
                _record("f3", "v2", [0.0, -1.0]),
            ]
        )
    )
    assert len(asyncio.run(store.ann_search([1.0, 0.0], k=2))) == 2
    assert len(asyncio.run(store.ann_search([1.0, 0.0], k=0))) == 0
    assert len(asyncio.run(store.ann_search([1.0, 0.0], k=-1))) == 0


def test_upsert_preserves_real_phash_in_frame_record() -> None:
    """FIX-phash · FR-004/FR-006: el FrameRecord conserva el pHash real (no 0).

    El pHash viaja en el contrato y el doble in-memory lo guarda tal cual
    (entero sin signo de 64 bits, p. ej. con bit 63 a 1 como los pHash
    reales de imagehash); `get_frame` lo devuelve para inspección.
    """
    store = InMemoryVectorStore()
    real_phash = 0x8000_0000_0000_0001
    asyncio.run(
        store.upsert_frames([_record("f1", "v1", [1.0, 0.0], timestamp_ms=1000, phash=real_phash)])
    )
    stored = asyncio.run(store.get_frame("f1"))
    assert stored is not None
    assert stored["phash"] == real_phash
    assert stored["phash"] != 0  # nunca el centinela
    assert stored["frame_id"] == "f1"
    assert stored["video_id"] == "v1"
    assert stored["timestamp_ms"] == 1000
    assert stored["embedding"] == (1.0, 0.0)
    assert asyncio.run(store.get_frame("missing")) is None


def test_upsert_is_idempotent_by_frame_id() -> None:
    """Re-upsert del mismo `frame_id` reemplaza sin duplicar (FR-008 / SC-005)."""
    store = InMemoryVectorStore()
    first = asyncio.run(
        store.upsert_frames(
            [
                _record("f1", "v1", [1.0, 0.0]),
                _record("f2", "v1", [0.0, 1.0]),
            ]
        )
    )
    second = asyncio.run(store.upsert_frames([_record("f2", "v1", [1.0, 0.0])]))
    assert first == 2
    assert second == 1
    assert asyncio.run(store.stats()) == {"videos": 1, "frames": 2, "vectors": 2}
    hits = asyncio.run(store.ann_search([1.0, 0.0], k=2))
    # f2 fue reemplazado por el embedding de f1 → ambos a distancia 0 (orden estable)
    assert [h["frame_id"] for h in hits] == ["f1", "f2"]


def test_delete_video_removes_frames_and_updates_stats() -> None:
    """`delete_video` elimina los frames del vídeo y actualiza `stats` (FR-014)."""
    store = InMemoryVectorStore()
    asyncio.run(
        store.upsert_frames(
            [
                _record("v1-f1", "v1", [1.0, 0.0]),
                _record("v1-f2", "v1", [0.0, 1.0]),
                _record("v2-f1", "v2", [0.5, 0.5]),
            ]
        )
    )
    asyncio.run(store.delete_video("v1"))
    assert asyncio.run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}
    hits = asyncio.run(store.ann_search([1.0, 0.0], k=5))
    assert [h["frame_id"] for h in hits] == ["v2-f1"]
    # Idempotente: borrar un vídeo inexistente no falla ni altera el índice
    asyncio.run(store.delete_video("v1"))
    assert asyncio.run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}


def test_ann_search_filters_excluded_videos() -> None:
    """`exclude_videos=True` (default) oculta vídeos excluidos; `False` los incluye."""
    store = InMemoryVectorStore()
    asyncio.run(
        store.upsert_frames(
            [
                _record("v1-f1", "v1", [1.0, 0.0]),
                _record("v2-f1", "v2", [0.9, 0.1]),
            ]
        )
    )
    # delete_video marca el vídeo como excluido (equivalente a videos.excluded,
    # FR-014): un re-upsert posterior no lo devuelve a los resultados.
    asyncio.run(store.delete_video("v2"))
    asyncio.run(store.upsert_frames([_record("v2-f1", "v2", [0.9, 0.1])]))

    hidden = asyncio.run(store.ann_search([1.0, 0.0], k=5))
    assert [h["frame_id"] for h in hidden] == ["v1-f1"]

    visible = asyncio.run(store.ann_search([1.0, 0.0], k=5, exclude_videos=False))
    assert {h["video_id"] for h in visible} == {"v1", "v2"}


def test_ann_search_preserves_timestamp_or_none() -> None:
    """El timestamp del frame se conserva; `None` cuando no existe (FR-012/edge case)."""
    store = InMemoryVectorStore()
    asyncio.run(
        store.upsert_frames(
            [
                _record("with-ts", "v1", [1.0, 0.0], timestamp_ms=94000),
                _record("no-ts", "v1", [0.0, 1.0]),
            ]
        )
    )
    hits = asyncio.run(store.ann_search([1.0, 0.0], k=2))
    by_id = {h["frame_id"]: h for h in hits}
    assert by_id["with-ts"]["timestamp_ms"] == 94000
    assert by_id["no-ts"]["timestamp_ms"] is None


def test_ann_search_rejects_dimension_mismatch() -> None:
    """Embedding de consulta con dimensión distinta a la indexada → ValueError."""
    store = InMemoryVectorStore()
    asyncio.run(store.upsert_frames([_record("f1", "v1", [1.0, 0.0, 0.0])]))
    with pytest.raises(ValueError, match="Dimensiones"):
        asyncio.run(store.ann_search([1.0, 0.0], k=5))


def test_ann_search_zero_vectors_get_max_distance() -> None:
    """Vector nulo indexado: distancia máxima (defensivo; embeddings L2-normalizados)."""
    store = InMemoryVectorStore()
    asyncio.run(store.upsert_frames([_record("f-zero", "v1", [0.0, 0.0])]))
    hits = asyncio.run(store.ann_search([1.0, 0.0], k=1))
    assert hits[0]["frame_id"] == "f-zero"
    assert hits[0]["distance"] == 1.0


def test_replace_video_index_removes_stale_frames_and_updates_metadata() -> None:
    """TASK-005-001/FR-010: reemplazo completo conserva solo el nuevo índice."""
    store = InMemoryVectorStore()
    asyncio.run(
        store.upsert_frames(
            [
                _record("old-1", "v1", [1.0, 0.0], timestamp_ms=0),
                _record("old-2", "v1", [0.0, 1.0], timestamp_ms=1000),
                _record("other", "v2", [1.0, 0.0], timestamp_ms=0),
            ]
        )
    )

    asyncio.run(
        store.replace_video_index(
            "v1",
            [_record("new-1", "v1", [1.0, 0.0], timestamp_ms=500)],
            duration_ms=12_345,
        )
    )

    assert asyncio.run(store.get_frame("old-1")) is None
    assert asyncio.run(store.get_frame("old-2")) is None
    assert asyncio.run(store.get_frame("new-1")) is not None
    assert asyncio.run(store.get_frame("other")) is not None
    assert asyncio.run(store.stats()) == {"videos": 2, "frames": 2, "vectors": 2}
    metadata = asyncio.run(store.get_video_index("v1"))
    assert metadata == {
        "status": "indexed",
        "frame_count": 1,
        "duration_ms": 12_345,
        "excluded": False,
    }


def test_replace_video_index_rejects_empty_without_mutating_previous_index() -> None:
    """Un lote vacío no puede borrar accidentalmente una representación válida."""
    store = InMemoryVectorStore()
    asyncio.run(store.upsert_frames([_record("old", "v1", [1.0, 0.0], timestamp_ms=100)]))

    with pytest.raises(ValueError, match="vacío"):
        asyncio.run(store.replace_video_index("v1", [], duration_ms=99))

    assert asyncio.run(store.get_frame("old")) is not None
    assert asyncio.run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}


def test_replace_video_index_does_not_change_excluded_flag() -> None:
    """FR-010: reindexar no deshace una exclusión explícita."""
    store = InMemoryVectorStore()
    asyncio.run(store.upsert_frames([_record("old", "v1", [1.0, 0.0], timestamp_ms=100)]))
    asyncio.run(store.delete_video("v1"))

    asyncio.run(
        store.replace_video_index(
            "v1", [_record("new", "v1", [1.0, 0.0], timestamp_ms=200)], duration_ms=500
        )
    )

    assert asyncio.run(store.get_video_index("v1"))["excluded"] is True
    assert asyncio.run(store.ann_search([1.0, 0.0], k=10)) == []
    assert len(asyncio.run(store.ann_search([1.0, 0.0], k=10, exclude_videos=False))) == 1


def test_writer_rolls_back_frames_if_state_commit_fails() -> None:
    """FR-010: el estado y el conjunto de frames no quedan descoordinados."""

    class _FailingStateStore(InMemoryVideoStateStore):
        async def mark_indexed(
            self, video_id: str, *, frame_count: int, duration_ms: int | None
        ) -> None:
            raise RuntimeError("state commit failed")

    store = InMemoryVectorStore()
    asyncio.run(store.upsert_frames([_record("old", "v1", [1.0, 0.0], timestamp_ms=100)]))
    writer = VideoIndexWriter(store=store, video_states=_FailingStateStore())

    with pytest.raises(RuntimeError, match="state commit failed"):
        asyncio.run(
            writer.replace_video_index(
                "v1", [_record("new", "v1", [1.0, 0.0], timestamp_ms=200)], duration_ms=500
            )
        )

    assert asyncio.run(store.get_frame("old")) is not None
    assert asyncio.run(store.get_frame("new")) is None


def test_video_state_snapshot_restore_is_public_and_preserves_metadata() -> None:
    """FR-010: rollback usa snapshot/restore sin inspeccionar `_videos`."""
    states = InMemoryVideoStateStore()
    asyncio.run(states.mark_discovered("v1", "clip.mp4"))
    asyncio.run(states.mark_indexed("v1", frame_count=5, duration_ms=12_000))
    snapshot = asyncio.run(states.snapshot("v1"))
    assert snapshot is not None
    assert snapshot.frame_count == 5
    assert snapshot.duration_ms == 12_000

    asyncio.run(states.mark_failed("v1", "boom"))
    assert asyncio.run(states.snapshot("v1")).status == "failed"
    asyncio.run(states.restore("v1", snapshot))
    restored = asyncio.run(states.snapshot("v1"))
    assert restored == snapshot


def test_writer_fails_before_mutation_without_public_rollback_contract() -> None:
    """FR-010: un store no rollbackable no puede publicar una sustitución."""

    class _NoRollbackStore:
        def __init__(self) -> None:
            self.called = False

        async def replace_video_index(
            self, _video_id: str, _records: list[FrameRecord], *, duration_ms: int | None
        ) -> None:
            self.called = True

    store = _NoRollbackStore()
    writer = VideoIndexWriter(store=store, video_states=InMemoryVideoStateStore())  # type: ignore[arg-type]

    with pytest.raises(AtomicReplacementError, match="rollback"):
        asyncio.run(
            writer.replace_video_index(
                "v1", [_record("f1", "v1", [1.0, 0.0], timestamp_ms=100)], duration_ms=500
            )
        )
    assert store.called is False
