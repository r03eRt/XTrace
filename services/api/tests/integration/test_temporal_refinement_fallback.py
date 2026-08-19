"""Fallback Postgres del refinamiento temporal (TASK-006-T020).

Las pruebas siguen el contrato de ``test_search.py``: usan el backend
``PgVectorStore`` y el proveedor fake determinista, pero solo poseen y limpian
las filas que crean. Si Supabase local no está disponible, el módulo se salta
sin intentar ejecutar una integración parcial.

Cobertura:

* una fuente deshabilitada conserva timestamp, ranking y provenance del índice
  base con un summary ``unavailable``;
* un fallo controlado del adapter conserva el mismo resultado con summary
  ``failed``;
* ninguna de las dos rutas modifica las filas/conteo de ``frames`` ni deja
  temporales de consulta en ``work_root``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from xtrace_spike.cli import build_backend  # type: ignore[import-untyped]
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider  # type: ignore[import-untyped]
from xtrace_spike.hashing.phash import compute_phash  # type: ignore[import-untyped]
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.base import FrameRecord  # type: ignore[import-untyped]

from tests.fixtures import EMBEDDING_DIMENSION, make_query_image, rgb_of
from xtrace_api.config import get_settings
from xtrace_api.main import app


def _db_available() -> bool:
    """¿Supabase local alcanzable? (misma convención que ``test_search.py``)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): fallback Postgres saltado",
)


@dataclass
class _OwnedRows:
    """IDs de filas creadas por una prueba para limpiar sin resetear la BD."""

    source_ids: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    search_ids: list[str] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _postgres_api_env(monkeypatch: pytest.MonkeyPatch, api_env: Path) -> Iterator[Path]:
    """Backend Postgres/fake determinista, con un work root aislado por test."""
    monkeypatch.setenv("SUPABASE_DB_URL", resolve_dsn())
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", str(api_env))
    # Los defaults son parte del contrato de la prueba; no heredar overrides
    # del entorno del operador que puedan convertir el fallback en disabled.
    monkeypatch.setenv("XTRACE_REFINEMENT_ENABLED", "true")
    monkeypatch.setenv("XTRACE_REFINEMENT_CANDIDATE_LIMIT", "3")
    monkeypatch.setenv("XTRACE_REFINEMENT_MAX_ASSETS_PER_CANDIDATE", "30")
    monkeypatch.setenv("XTRACE_REFINEMENT_SEARCH_TIMEOUT_MS", "10000")
    monkeypatch.setenv("XTRACE_REFINEMENT_CANDIDATE_TIMEOUT_MS", "3000")
    monkeypatch.setenv("XTRACE_REFINEMENT_SOURCE_OVERRIDES", "{}")
    get_settings.cache_clear()
    build_backend.cache_clear()
    yield api_env
    get_settings.cache_clear()
    build_backend.cache_clear()


@pytest.fixture
def _owned_rows() -> Iterator[_OwnedRows]:
    """Limpia solo las filas UUID creadas por el test actual."""
    owned = _OwnedRows()
    yield owned
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            for search_id in owned.search_ids:
                cur.execute("delete from public.searches where id = %s", (search_id,))
            # videos → frames/evidence por cascade; source_id usa SET NULL,
            # pero borramos vídeos primero para no dejar referencias propias.
            for video_id in owned.video_ids:
                cur.execute("delete from public.videos where id = %s", (video_id,))
            for source_id in owned.source_ids:
                cur.execute("delete from public.sources where id = %s", (source_id,))


def _seed_web_video(
    image: Path,
    *,
    source_name: str,
    adapter: str,
    source_enabled: bool,
    owned: _OwnedRows,
) -> tuple[str, str]:
    """Siembra una fuente web y un frame idéntico a ``image`` en Postgres."""
    source_id = str(uuid.uuid4())
    video_id = str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    external_id = f"synthetic-{video_id}"

    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.sources (id, name, adapter, manifest, enabled) "
                "values (%s, %s, %s, %s::jsonb, %s)",
                (
                    source_id,
                    source_name,
                    adapter,
                    json.dumps({"test": "temporal-refinement-fallback"}),
                    source_enabled,
                ),
            )
            owned.source_ids.append(source_id)
            cur.execute(
                "insert into public.videos "
                "(id, local_ref, title, page_url, source_id, external_id, "
                "duration_ms, status, frame_count) "
                "values (%s, %s, %s, %s, %s, %s, %s, 'indexed', 1)",
                (
                    video_id,
                    f"temporal-refinement-{video_id}.mp4",
                    "Vídeo sintético de fallback",
                    f"https://www.xvideos.com/video.test/{external_id}",
                    source_id,
                    external_id,
                    120_000,
                ),
            )
            owned.video_ids.append(video_id)

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


def _search(client: TestClient, image: Path, **form: str) -> Response:
    """POST /search con la imagen sintética del fixture."""
    with image.open("rb") as handle:
        return client.post(
            "/search",
            files={"image": (image.name, handle, "image/png")},
            data=form,
        )


def _remember_search(payload: object, owned: _OwnedRows) -> None:
    """Registra pronto el ``search_id`` para no dejar huérfanos al fallar asserts."""
    if isinstance(payload, dict):
        search_id = payload.get("search_id")
        if isinstance(search_id, str):
            owned.search_ids.append(search_id)


def _frames_state() -> tuple[int, tuple[tuple[object, ...], ...]]:
    """Snapshot de filas observables de ``frames`` y su conteo global."""
    with psycopg.connect(resolve_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from public.frames")
            count_row = cur.fetchone()
            assert count_row is not None
            cur.execute(
                "select id::text, video_id::text, timestamp_ms, frame_seq, phash, "
                "embedding::text, width, height, source_kind "
                "from public.frames order by id"
            )
            rows = tuple(tuple(row) for row in cur.fetchall())
    return int(count_row[0]), rows


def _assert_base_fallback(
    payload: dict[str, object],
    *,
    video_id: str,
    source_name: str,
    summary_status: str,
    provenance_status: str,
) -> None:
    """Aserciones comunes de timestamp, ranking y procedencia base."""
    results = payload["results"]
    assert isinstance(results, list)
    assert len(results) == 1
    top = results[0]
    assert isinstance(top, dict)
    assert top["video_id"] == video_id
    assert top["match_score"] == 1.0
    assert top["matching_frames"] == 1
    assert top["match_timestamp_ms"] == 94_000

    provenance = top["timestamp_provenance"]
    assert isinstance(provenance, dict)
    assert provenance["origin"] == "base_index"
    assert provenance["status"] == provenance_status
    assert provenance["source"] == source_name
    assert provenance["asset_kind"] is None
    assert provenance["asset_url"] is None
    assert provenance["asset_position"] is None

    summary = payload["refinement"]
    assert isinstance(summary, dict)
    assert summary["status"] == summary_status
    assert summary["candidates_requested"] == 1
    assert summary["candidates_processed"] == 1
    assert summary["assets_evaluated"] == 0
    assert summary["improved_results"] == 0


def test_postgres_unavailable_source_preserves_base_and_frames(
    api_env: Path, tmp_path: Path, _owned_rows: _OwnedRows
) -> None:
    """Fuente deshabilitada → fallback honesto sin mutar el índice (FR-008/009)."""
    query = make_query_image(tmp_path / "unavailable-source.png")
    source_name = f"test-unavailable-{uuid.uuid4().hex}"
    video_id, _ = _seed_web_video(
        query,
        source_name=source_name,
        adapter="xvideos",
        source_enabled=False,
        owned=_owned_rows,
    )
    before_frames = _frames_state()

    with TestClient(app) as client:
        response = _search(client, query, min_score="0.99")

    payload = response.json()
    _remember_search(payload, _owned_rows)
    assert response.status_code == 200, response.text
    _assert_base_fallback(
        payload,
        video_id=video_id,
        source_name=source_name,
        summary_status="unavailable",
        provenance_status="unavailable",
    )
    assert _frames_state() == before_frames
    # FR-010/SEC-005: la imagen de consulta y la copia segura desaparecen.
    assert list(api_env.iterdir()) == []


def test_postgres_refinement_failure_preserves_base_and_cleans_work_root(
    api_env: Path, tmp_path: Path, _owned_rows: _OwnedRows
) -> None:
    """Adapter desconocido → fallo controlado, fallback base y cleanup (FR-010)."""
    query = make_query_image(tmp_path / "adapter-failure.png")
    source_name = f"test-failure-{uuid.uuid4().hex}"
    video_id, _ = _seed_web_video(
        query,
        source_name=source_name,
        adapter="adapter-not-registered",
        source_enabled=True,
        owned=_owned_rows,
    )
    before_frames = _frames_state()

    with TestClient(app) as client:
        response = _search(client, query, min_score="0.99")

    payload = response.json()
    _remember_search(payload, _owned_rows)
    assert response.status_code == 200, response.text
    _assert_base_fallback(
        payload,
        video_id=video_id,
        source_name=source_name,
        summary_status="failed",
        provenance_status="unavailable",
    )
    summary = payload["refinement"]
    assert isinstance(summary, dict)
    assert summary["errors_count"] == 1
    assert _frames_state() == before_frames
    assert list(api_env.iterdir()) == []
