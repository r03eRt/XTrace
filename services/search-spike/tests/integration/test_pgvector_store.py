"""Tests de integración de `PgVectorStore` (PR-007 · FR-006/008/014 · SC-003/005
· ADR-0004/0007 · contracts §2) contra Supabase local.

Cubren el contrato `VectorStore` con la implementación real sobre
pgvector/HNSW (coseno, tabla `frames` de la migración PR-006) y verifican
**paridad de comportamiento** con `InMemoryVectorStore` (PR-003): orden por
distancia, idempotencia del upsert, `delete_video`, filtro `exclude_videos`
y `stats`.

Se **skippean** si la DB local no es alcanzable (p. ej. CI sin Supabase): la
comprobación ocurre en recolección vía `pytestmark`, no por import. Cada test
limpia las tablas al inicio (constitución §6: tests independientes y
reproducibles).
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

import xtrace_spike.vectorstore.pgvector as pgvector_module
from xtrace_spike.repo import resolve_dsn
from xtrace_spike.vectorstore.base import FrameRecord, VectorStore
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore
from xtrace_spike.vectorstore.pgvector import (
    EMBEDDING_DIMENSION,
    PgVectorStore,
    phash_from_db,
    phash_to_db,
)

D = EMBEDDING_DIMENSION


def _db_available() -> bool:
    """¿Supabase local alcanzable? (DSN por defecto/env, migración PR-006)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): integration PgVectorStore saltada",
)


@pytest.fixture(autouse=True)
def _clean_tables() -> None:
    """Estado DB limpio por test (misma conexión sync que la comprobación)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate table public.frames, public.videos, public.searches cascade")


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del store (sin pytest-asyncio, estilo PR-003)."""
    return asyncio.run(coro)


def _uuid(n: int) -> str:
    """UUID determinista (canónico) para tests: `000…-n`."""
    return f"00000000-0000-4000-8000-{n:012d}"


def _unit(index: int) -> list[float]:
    """Vector unitario del eje `index` (dimensión D, determinista)."""
    vec = [0.0] * D
    vec[index] = 1.0
    return vec


def _proj(x: float) -> list[float]:
    """Vector normalizado con proyección `x` sobre el eje 0 (resto en 0)."""
    y = (1.0 - x * x) ** 0.5
    return [x, y] + [0.0] * (D - 2)


def _record(
    frame_id: str,
    video_id: str,
    timestamp_ms: int | None,
    embedding: list[float],
    phash: int | None = None,
) -> FrameRecord:
    if phash is None:
        # pHash determinista derivado del frame_id (FIX-phash): el hex del
        # UUID activa el bit 63, de modo que TODOS los upserts de estos
        # tests ejercitan la codificación con signo del bigint.
        phash = int(frame_id.replace("-", ""), 16) & ((1 << 64) - 1)
    return FrameRecord(
        frame_id=frame_id,
        video_id=video_id,
        timestamp_ms=timestamp_ms,
        phash=phash,
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# Contrato (contracts §2) + operaciones básicas
# ---------------------------------------------------------------------------


def test_store_satisfies_vectorstore_contract() -> None:
    """Compatibilidad estructural con el Protocol + estado inicial vacío."""
    store: VectorStore = PgVectorStore()
    assert _run(store.stats()) == {"videos": 0, "frames": 0, "vectors": 0}


def test_upsert_and_ann_search_orders_by_cosine_distance() -> None:
    """Upsert + ANN coseno (HNSW): hits ordenados por distancia ascendente (FR-006)."""
    store = PgVectorStore()
    v1, v2 = _uuid(1), _uuid(2)
    a = _record(_uuid(11), v1, 0, _unit(0))
    b = _record(_uuid(12), v1, 1000, _unit(1))
    c = _record(_uuid(13), v1, 2000, _proj(0.5))
    other = _record(_uuid(21), v2, 500, _proj(0.25))
    assert _run(store.upsert_frames([a, b, c, other])) == 4

    hits = _run(store.ann_search(_unit(0), k=10))
    assert [h["frame_id"] for h in hits] == [_uuid(11), _uuid(13), _uuid(21), _uuid(12)]
    assert [h["video_id"] for h in hits] == [v1, v1, v2, v1]
    assert [h["timestamp_ms"] for h in hits] == [0, 2000, 500, 1000]
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)
    assert hits[1]["distance"] == pytest.approx(0.5, abs=1e-6)
    assert hits[2]["distance"] == pytest.approx(0.75, abs=1e-6)
    assert hits[3]["distance"] == pytest.approx(1.0, abs=1e-6)


def test_ann_search_limits_top_k() -> None:
    """Límite `k` respetado; `k <= 0` devuelve lista vacía."""
    store = PgVectorStore()
    v1 = _uuid(1)
    _run(store.upsert_frames([_record(_uuid(i), v1, i * 1000, _unit(i)) for i in range(1, 6)]))
    assert len(_run(store.ann_search(_unit(0), k=3))) == 3
    assert _run(store.ann_search(_unit(0), k=0)) == []
    assert _run(store.ann_search(_unit(0), k=-1)) == []


# ---------------------------------------------------------------------------
# FIX-phash · FR-004/FR-006 · persistencia del pHash real del frame
# ---------------------------------------------------------------------------


def test_upsert_persists_real_phash() -> None:
    """frames.phash queda con el pHash REAL del frame (no centinela 0).

    Cubre el rango completo [0, 2^64): valores pequeños y pHash con bit 63
    (los reales de imagehash) que desbordarían bigint sin la codificación
    con signo (phash_to_db). El round-trip phash_from_db(columna) debe
    devolver exactamente el pHash del contrato.
    """
    store = PgVectorStore()
    v1 = _uuid(1)
    phashes = [0, 42, (1 << 63) - 1, 1 << 63, (1 << 64) - 1]
    records = [
        _record(_uuid(10 + i), v1, i * 1000, _unit(0), phash=phash)
        for i, phash in enumerate(phashes)
    ]
    assert _run(store.upsert_frames(records)) == len(records)

    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("select frame_seq, phash from public.frames order by frame_seq")
            rows = cur.fetchall()
    assert [phash_from_db(row[1]) for row in rows] == phashes
    # ningún frame con pHash real distinto de 0 queda almacenado como 0
    assert [row[1] for row in rows if row[1] != 0] == [
        phash_to_db(phash) for phash in phashes if phash != 0
    ]


# ---------------------------------------------------------------------------
# Idempotencia (FR-008 / SC-005)
# ---------------------------------------------------------------------------


def test_upsert_is_idempotent() -> None:
    """Re-upsert (mismo lote o misma clave con nuevo frame_id) no duplica."""
    store = PgVectorStore()
    v1 = _uuid(1)
    frames = [
        _record(_uuid(11), v1, 0, _unit(0)),
        _record(_uuid(12), v1, 1000, _unit(1)),
    ]
    assert _run(store.upsert_frames(frames)) == 2
    stats1 = _run(store.stats())
    # re-upsert idéntico → reemplaza, no duplica
    assert _run(store.upsert_frames(frames)) == 2
    # misma clave (video_id, timestamp) con frame_id nuevo → reemplaza igualmente
    frames2 = [
        _record(_uuid(99), v1, 0, _unit(0)),
        _record(_uuid(98), v1, 1000, _unit(1)),
    ]
    assert _run(store.upsert_frames(frames2)) == 2
    assert _run(store.stats()) == stats1 == {"videos": 1, "frames": 2, "vectors": 2}


def test_replace_video_index_removes_stale_rows_and_commits_metadata() -> None:
    """TASK-005-001/FR-010: frames + estado + conteo se publican juntos."""
    store = PgVectorStore()
    v1 = _uuid(1)
    old = [
        _record(_uuid(11), v1, 0, _unit(0)),
        _record(_uuid(12), v1, 1000, _unit(1)),
    ]
    _run(store.upsert_frames(old))

    replacement = [_record(_uuid(13), v1, 500, _unit(0))]
    _run(store.replace_video_index(v1, replacement, duration_ms=12_345))

    assert _run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}
    hits = _run(store.ann_search(_unit(0), k=10))
    assert [hit["frame_id"] for hit in hits] == [_uuid(13)]
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select status, frame_count, duration_ms, excluded "
                "from public.videos where id = %s",
                (v1,),
            )
            row = cur.fetchone()
    assert row == ("indexed", 1, 12_345, False)


def test_replace_video_index_rejects_invalid_batch_without_mutating_previous_rows() -> None:
    """FR-010: una sustitución inválida conserva la representación anterior."""
    store = PgVectorStore()
    v1 = _uuid(1)
    previous = [_record(_uuid(11), v1, 0, _unit(0))]
    _run(store.upsert_frames(previous))

    with pytest.raises(ValueError, match="posiciones duplicadas"):
        _run(
            store.replace_video_index(
                v1,
                [
                    _record(_uuid(12), v1, 100, _unit(0)),
                    _record(_uuid(13), v1, 100, _unit(1)),
                ],
                duration_ms=2_000,
            )
        )

    assert _run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}
    assert [hit["frame_id"] for hit in _run(store.ann_search(_unit(0), k=10))] == [_uuid(11)]


def test_replace_video_index_rolls_back_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-010: un fallo real dentro de commit ejecuta rollback de la transacción."""

    class _FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def __aenter__(self) -> _FakeCursor:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, sql: str, _params: object = None) -> None:
            self.statements.append(sql)

        async def executemany(self, sql: str, _params: object) -> None:
            self.statements.append(sql)

    class _FakeConnection:
        def __init__(self) -> None:
            self.cursor_obj = _FakeCursor()
            self.commit_called = False
            self.rollback_called = False

        async def __aenter__(self) -> _FakeConnection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _FakeCursor:
            return self.cursor_obj

        async def commit(self) -> None:
            self.commit_called = True
            raise RuntimeError("commit failure")

        async def rollback(self) -> None:
            self.rollback_called = True

    connection = _FakeConnection()

    class _FakeAsyncConnection:
        @classmethod
        async def connect(cls, _dsn: str, *, autocommit: bool) -> _FakeConnection:
            assert not autocommit
            return connection

    monkeypatch.setattr(pgvector_module.psycopg, "AsyncConnection", _FakeAsyncConnection)

    with pytest.raises(RuntimeError, match="commit failure"):
        _run(
            PgVectorStore(dsn="postgresql://fake").replace_video_index(
                _uuid(1), [_record(_uuid(11), _uuid(1), 100, _unit(0))], duration_ms=2_000
            )
        )

    assert connection.commit_called is True
    assert connection.rollback_called is True
    assert any("delete from public.frames" in sql for sql in connection.cursor_obj.statements)


# ---------------------------------------------------------------------------
# Filtro exclude_videos (FR-014) y delete_video
# ---------------------------------------------------------------------------


def test_ann_search_filters_excluded_videos() -> None:
    """`exclude_videos=True` oculta vídeos `excluded`; False los incluye (FR-014)."""
    store = PgVectorStore()
    v1, v2 = _uuid(1), _uuid(2)
    _run(
        store.upsert_frames(
            [
                _record(_uuid(11), v1, 0, _unit(0)),
                _record(_uuid(21), v2, 0, _unit(1)),
            ]
        )
    )
    # Exclusión directa en la DB (videos.excluded): la gestiona repo.py (PR-013).
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("update public.videos set excluded = true where id = %s", (v2,))
    hidden = _run(store.ann_search(_unit(1), k=10))
    assert [h["video_id"] for h in hidden] == [v1]
    shown = _run(store.ann_search(_unit(1), k=10, exclude_videos=False))
    assert [h["video_id"] for h in shown] == [v2, v1]


def test_delete_video_removes_frames_and_updates_stats() -> None:
    """`delete_video`: frames fuera del índice + vídeo excluido (paridad PR-003)."""
    store = PgVectorStore()
    v1, v2 = _uuid(1), _uuid(2)
    _run(
        store.upsert_frames(
            [
                _record(_uuid(11), v1, 0, _unit(0)),
                _record(_uuid(21), v2, 0, _unit(1)),
            ]
        )
    )
    _run(store.delete_video(v1))
    hits = _run(store.ann_search(_unit(0), k=10))
    assert [h["video_id"] for h in hits] == [v2]
    assert _run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}
    # borrado idempotente
    _run(store.delete_video(v1))
    assert _run(store.stats()) == {"videos": 1, "frames": 1, "vectors": 1}


# ---------------------------------------------------------------------------
# Robustez y paridad con InMemoryVectorStore
# ---------------------------------------------------------------------------


def test_ann_search_preserves_timestamp_or_none() -> None:
    """`timestamp_ms` conservado y `None` soportado (FR-012; NULLS DISTINCT)."""
    store = PgVectorStore()
    v1 = _uuid(1)
    _run(
        store.upsert_frames(
            [
                _record(_uuid(11), v1, None, _proj(1.0)),  # d=0.0
                _record(_uuid(12), v1, None, _proj(0.5)),  # d=0.5
                _record(_uuid(13), v1, 5000, _proj(0.25)),  # d=0.75
            ]
        )
    )
    hits = _run(store.ann_search(_unit(0), k=10))
    assert [h["timestamp_ms"] for h in hits] == [None, None, 5000]
    assert _run(store.stats()) == {"videos": 1, "frames": 3, "vectors": 3}


def test_ann_search_rejects_dimension_mismatch() -> None:
    """Dimensión distinta de 768 → ValueError (paridad con el doble in-memory)."""
    store = PgVectorStore()
    with pytest.raises(ValueError, match="Dimensiones de embedding distintas"):
        _run(store.ann_search([1.0, 2.0], k=5))


def test_parity_with_in_memory_store() -> None:
    """Mismo escenario en ambos stores → mismos hits/orden/distancia y stats.

    Distancias del escenario (estrictamente distintas por consulta, para que el
    orden no dependa de desempates):
    - `_proj(1.0)` → 0.0 · `_proj(0.75)` → 0.25 · `_proj(0.5)` → 0.5
    - `_proj(0.25)` → 0.75 · `_proj(0.0)` → 1.0   (frente a `_unit(0)`)
    """
    mem = InMemoryVectorStore()
    pg = PgVectorStore()
    v1, v2 = _uuid(1), _uuid(2)
    scenario = [
        _record(_uuid(11), v1, 0, _proj(1.0)),
        _record(_uuid(12), v1, 1000, _proj(0.75)),
        _record(_uuid(13), v1, 2000, _proj(0.5)),
        _record(_uuid(21), v2, None, _proj(0.25)),
        _record(_uuid(22), v2, 7000, _proj(0.0)),
    ]
    assert _run(mem.upsert_frames(scenario)) == 5
    assert _run(pg.upsert_frames(scenario)) == 5
    for query in (_unit(0), _unit(1)):
        mem_hits = _run(mem.ann_search(query, k=10))
        pg_hits = _run(pg.ann_search(query, k=10))
        assert [h["frame_id"] for h in mem_hits] == [h["frame_id"] for h in pg_hits]
        assert [h["timestamp_ms"] for h in mem_hits] == [h["timestamp_ms"] for h in pg_hits]
        for mh, ph in zip(mem_hits, pg_hits, strict=True):
            assert ph["distance"] == pytest.approx(mh["distance"], abs=1e-6)
    assert _run(mem.stats()) == _run(pg.stats()) == {"videos": 2, "frames": 5, "vectors": 5}
    # delete_video: paridad de resultados y stats tras el borrado
    _run(mem.delete_video(v2))
    _run(pg.delete_video(v2))
    mem_hits = _run(mem.ann_search(_unit(0), k=10))
    pg_hits = _run(pg.ann_search(_unit(0), k=10))
    assert [h["frame_id"] for h in mem_hits] == [h["frame_id"] for h in pg_hits]
    assert _run(mem.stats()) == _run(pg.stats()) == {"videos": 1, "frames": 3, "vectors": 3}
