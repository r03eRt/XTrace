"""Tests de `GET /stats` y del TTL de `searches` en el lifespan (PR-056 ·
FR-007/012 · DATA-001 · SEC-004 · contracts §3 · data-model.md).

Dos niveles (mismo patrón que PR-055):
- **Sin BD** (siempre): contrato §3 con el backend in-memory — `videos`/
  `frames`/`vectors` del índice sembrado con los componentes reales de la
  cadena (Fake D=768, ADR-0007), `backend: "in-memory"` y
  `embedding_provider` = `model_id` del provider activo.
- **Con BD** (skipif sin Supabase local): `/stats` coherente con la cadena de
  la CLI `stats` (paridad FR-007 sobre el índice postgres) y **TTL ejecutado
  en el lifespan** (FR-012 · DATA-001: purge inicial al arrancar borra las
  filas vencidas por `created_at` y conserva las recientes, sin migración).
  Acceso service-side con RLS deny-by-default intacta (SEC-004).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from xtrace_spike.cli import (
    build_backend,  # type: ignore[import-untyped]
    resolve_embedding_provider,  # type: ignore[import-untyped]
)
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider  # type: ignore[import-untyped]
from xtrace_spike.hashing.phash import compute_phash  # type: ignore[import-untyped]
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.base import FrameRecord  # type: ignore[import-untyped]

from tests.fixtures import EMBEDDING_DIMENSION, make_query_image, rgb_of, seed_in_memory_index
from xtrace_api.config import get_settings
from xtrace_api.main import app

#: `model_id` del provider fake (paridad CLI: la CLI `stats` emite model_id).
FAKE_MODEL_ID = FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION).model_id


def _db_available() -> bool:
    """¿Supabase local alcanzable? (mismo patrón que spike/crawler/PR-055)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


DB_SKIP = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): integración de /stats saltada",
)


@pytest.fixture
def _postgres_api_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Backend postgres determinista por test: env, work_root y cachés reset.

    También fija el TTL (30 días / 60 min) para no depender de la env del
    operador en el test del lifespan.
    """
    monkeypatch.setenv("SUPABASE_DB_URL", resolve_dsn())
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("XTRACE_API_SEARCHES_TTL_DAYS", "30")
    monkeypatch.setenv("XTRACE_API_SEARCHES_TTL_CLEANUP_MIN", "60")
    get_settings.cache_clear()
    build_backend.cache_clear()
    yield tmp_path / "work"
    get_settings.cache_clear()
    build_backend.cache_clear()


@pytest.fixture
def _clean_tables() -> Iterator[None]:
    """Estado DB limpio por test (misma convención que el spike: truncate)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "truncate table public.frames, public.videos, public.searches, "
                "public.sources cascade"
            )
    yield
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "truncate table public.frames, public.videos, public.searches, "
                "public.sources cascade"
            )


def _seed_pg_video_with_frame(image: Path) -> tuple[str, str]:
    """Vídeo con un frame indexado en postgres (misma cadena que PR-055)."""
    video_id = str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.videos (id, local_ref) values (%s, %s)",
                (video_id, f"local-{video_id[:8]}.mp4"),
            )
    frame = FrameRecord(
        frame_id=frame_id,
        video_id=video_id,
        timestamp_ms=94_000,
        phash=compute_phash(rgb_of(image)),
        embedding=[
            float(value)
            for value in FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION).embed_images(
                [rgb_of(image)]
            )[0]
        ],
    )
    asyncio.run(build_backend().store.upsert_frames([frame]))
    return video_id, frame_id


def _searches_ids() -> set[str]:
    """Ids de `searches` (para verificar el TTL del lifespan)."""
    with psycopg.connect(resolve_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("select id::text from public.searches")
            return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Sin BD: contrato §3 (backend in-memory)
# ---------------------------------------------------------------------------


def test_stats_contract_empty_in_memory(api_env: Path) -> None:
    """Índice vacío → ceros + `backend: in-memory` + model_id del provider (FR-007)."""
    with TestClient(app) as client:
        response = client.get("/stats")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "videos": 0,
        "frames": 0,
        "vectors": 0,
        "backend": "in-memory",
        "embedding_provider": FAKE_MODEL_ID,
    }


def test_stats_contract_fields_in_memory(api_env: Path) -> None:
    """Contrato §3 con índice sembrado: conteos reales del store (FR-007)."""
    query_a = make_query_image(api_env / "query-a.png")
    query_b = make_query_image(api_env / "query-b.png", size=(32, 24))
    seed_in_memory_index(query_a)
    seed_in_memory_index(query_b)

    with TestClient(app) as client:
        response = client.get("/stats")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "videos": 2,
        "frames": 2,
        "vectors": 2,
        "backend": "in-memory",
        "embedding_provider": FAKE_MODEL_ID,
    }


# ---------------------------------------------------------------------------
# Con BD (skipif): paridad con la CLI `stats` y TTL en el lifespan
# ---------------------------------------------------------------------------


@DB_SKIP
def test_stats_coherent_with_cli_stats(
    _postgres_api_env: Path, _clean_tables: Iterator[None]
) -> None:
    """FR-007: `/stats` coincide con la cadena de la CLI `stats` sobre postgres.

    Misma regla que la CLI (`build_backend` + `resolve_embedding_provider` +
    `VectorStore.stats()`): paridad por construcción verificada con valores
    no vacíos (1 vídeo con 1 frame indexado).
    """
    query = make_query_image(_postgres_api_env / "query.png")
    _seed_pg_video_with_frame(query)

    with TestClient(app) as client:
        response = client.get("/stats")

    assert response.status_code == 200, response.text
    payload = response.json()
    backend = build_backend()
    index_stats = asyncio.run(backend.store.stats())
    embeddings = resolve_embedding_provider("fake")
    assert payload == {
        "videos": index_stats["videos"],
        "frames": index_stats["frames"],
        "vectors": index_stats["vectors"],
        "backend": backend.label,
        "embedding_provider": embeddings.model_id,
    }
    assert payload["backend"] == "postgres"
    assert payload["videos"] == 1
    assert payload["frames"] == 1
    assert payload["vectors"] == 1


@DB_SKIP
def test_searches_ttl_purged_in_lifespan(
    _postgres_api_env: Path, _clean_tables: Iterator[None]
) -> None:
    """FR-012 · DATA-001: el purge inicial del lifespan borra solo las vencidas.

    Fila vieja (created_at hace 31 días, TTL=30) → borrada; fila reciente →
    conservada. Sin migración: el TTL es cleanup por `created_at`
    (data-model.md).
    """
    old_id = str(uuid.uuid4())
    recent_id = str(uuid.uuid4())
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.searches "
                "(id, search_type, processing_ms, results_count, created_at) "
                "values (%s, 'image', 1, 0, now() - interval '31 days'), "
                "(%s, 'image', 1, 0, now())",
                (old_id, recent_id),
            )

    with TestClient(app) as client:  # el lifespan ejecuta el purge inicial
        assert client.get("/health").status_code == 200

    # El purge corre en una tarea de fondo del lifespan: se espera con
    # timeout corto a que la fila vencida desaparezca (determinismo).
    deadline = time.monotonic() + 3.0
    remaining = _searches_ids()
    while old_id in remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = _searches_ids()

    assert old_id not in remaining  # vencida, borrada
    assert recent_id in remaining  # reciente, conservada
