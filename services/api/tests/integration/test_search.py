"""Tests de integración de `POST /search` contra Supabase local (PR-055 · FR-002/003
/005/012/013 · SEC-003/004/005 · SC-001/003/006 · contracts §1/§5/§7).

Siguen el patrón del spike/crawler: se saltan limpiamente si la BD local no
está disponible (`skipif`, CI sin stack Supabase). Con BD, el backend del
índice es `PgVectorStore` (env `SUPABASE_DB_URL`; paridad con la CLI) y el
acceso es service-side con RLS deny-by-default intacta (SEC-004): sin
políticas ni grants nuevos.

Cada test trunca `frames`/`videos`/`searches` (misma convención que
`test_pgvector_store.py` del spike): el índice tocado es solo el sembrado
por el propio test.

Requisitos cubiertos:
- FR-012: cada búsqueda aceptada inserta una fila en `searches`
  (`id = search_id`, `search_type='image'`, `processing_ms`, `results_count`).
- FR-004 MAY: enriquecimiento `title`/`page_url` desde `public.videos`.
- SC-003: `work_root` sin restos tras búsqueda con éxito y con error.
- SC-006: los 4xx se devuelven sin ejecutar la búsqueda (ni insertar fila).
- contracts §7.2: `search_id` único por búsqueda (búsquedas concurrentes).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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

from tests.fixtures import (
    EMBEDDING_DIMENSION,
    make_bogus_file,
    make_query_image,
    rgb_of,
)
from xtrace_api.config import get_settings
from xtrace_api.main import app


def _db_available() -> bool:
    """¿Supabase local alcanzable? (mismo patrón que spike/crawler)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): integración de /search saltada",
)


@pytest.fixture(autouse=True)
def _postgres_api_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, api_env: Path
) -> Iterator[Path]:
    """Backend postgres determinista por test: env, work_root y cachés reset."""
    monkeypatch.setenv("SUPABASE_DB_URL", resolve_dsn())
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", str(tmp_path / "work"))
    get_settings.cache_clear()
    build_backend.cache_clear()
    yield api_env
    get_settings.cache_clear()
    build_backend.cache_clear()


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    """Estado DB limpio por test (misma convención que el spike: truncate)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate table public.frames, public.videos, public.searches cascade")
    yield
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate table public.frames, public.videos, public.searches cascade")


def _seed_video(
    *,
    image: Path,
    title: str | None = None,
    page_url: str | None = None,
) -> tuple[str, str]:
    """Siembra un vídeo con metadatos y un frame idéntico a la imagen en postgres.

    Devuelve (video_id, frame_id). El vídeo se inserta con `local_ref`,
    `title` y `page_url`; el frame con el embedding/pHash reales de la
    cadena (Fake D=768 + compute_phash) → la búsqueda de la imagen lo
    encuentra con distancia 0.0.
    """
    video_id = str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.videos (id, local_ref, title, page_url) "
                "values (%s, %s, %s, %s)",
                (video_id, f"local-{video_id[:8]}.mp4", title, page_url),
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


def _search(client: TestClient, image: Path, **form: object) -> Response:
    """POST /search con la imagen (helper tipado ligero)."""
    with image.open("rb") as handle:
        return client.post(
            "/search",
            files={"image": (image.name, handle, "image/png")},
            data=form,
        )


def _searches_rows() -> list[tuple[object, ...]]:
    """Filas de `searches` (para verificar FR-012 y SC-006)."""
    with psycopg.connect(resolve_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id::text, search_type, processing_ms, results_count from public.searches"
            )
            return cur.fetchall()


def test_search_end_to_end_with_enrichment(api_env: Path, tmp_path: Path) -> None:
    """POST /search end-to-end contra postgres: resultados + extensión MAY + FR-012."""
    query = make_query_image(tmp_path / "query.png")
    video_id, _ = _seed_video(
        image=query,
        title="Vídeo de ejemplo del corpus",
        page_url="https://www.xvideos.com/video.abc123/ejemplo",
    )

    with TestClient(app) as client:
        response = _search(client, query)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"search_id", "processing_ms", "refinement", "results"}
    assert payload["refinement"]["status"] == "unavailable"
    search_id = uuid.UUID(payload["search_id"])
    assert search_id.version == 4
    assert isinstance(payload["processing_ms"], int)
    assert payload["processing_ms"] >= 0

    assert len(payload["results"]) == 1
    top = payload["results"][0]
    # contracts §1 (campos CLI) + extensión MAY title/page_url (FR-004).
    assert set(top) == {
        "video_id",
        "local_ref",
        "title",
        "page_url",
        "match_score",
        "matching_frames",
        "match_timestamp_ms",
        "evidence",
        "timestamp_provenance",
    }
    assert top["video_id"] == video_id
    assert top["local_ref"] == f"local-{video_id[:8]}.mp4"
    assert top["title"] == "Vídeo de ejemplo del corpus"
    assert top["page_url"] == "https://www.xvideos.com/video.abc123/ejemplo"
    assert top["match_score"] == 1.0
    assert top["matching_frames"] == 1
    assert top["match_timestamp_ms"] == 94_000
    assert top["evidence"] == {"visual": 1.0, "phash": 1.0}
    assert top["timestamp_provenance"]["origin"] == "base_index"
    assert top["timestamp_provenance"]["status"] == "unavailable"

    # FR-012: la fila de analítica existe con search_type='image' (contracts §7.6).
    rows = _searches_rows()
    assert len(rows) == 1
    row_id, search_type, processing_ms, results_count = rows[0]
    assert row_id == payload["search_id"]
    assert search_type == "image"
    assert isinstance(processing_ms, int) and processing_ms >= 0
    assert results_count == 1

    # SC-003: la media de consulta no deja restos en work_root.
    assert list(api_env.iterdir()) == []


def test_search_local_video_returns_null_metadata(api_env: Path) -> None:
    """Vídeo local (sin title/page_url): extensión MAY null (contracts §1)."""
    query = make_query_image(api_env / "query.png")
    video_id, _ = _seed_video(image=query)

    with TestClient(app) as client:
        response = _search(client, query)

    assert response.status_code == 200, response.text
    top = response.json()["results"][0]
    assert top["video_id"] == video_id
    assert top["local_ref"] == f"local-{video_id[:8]}.mp4"
    assert top["title"] is None
    assert top["page_url"] is None


def test_search_concurrent_requests_get_unique_ids(api_env: Path) -> None:
    """Búsquedas concurrentes → search_id independientes (edge case spec §7.2)."""
    query = make_query_image(api_env / "query.png")
    _seed_video(image=query)

    def run_search() -> str:
        with TestClient(app) as client:
            response = _search(client, query)
        assert response.status_code == 200, response.text
        return response.json()["search_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: run_search(), range(4)))

    assert len(ids) == 4
    assert len(set(ids)) == 4  # únicos
    assert len(_searches_rows()) == 4  # FR-012: una fila por búsqueda


def test_search_validation_errors_do_not_run_search(api_env: Path, tmp_path: Path) -> None:
    """SC-006: 413/415/400 sin ejecutar búsqueda (ni fila en searches)."""
    with TestClient(app) as client:
        # 413: media > 10 MB (sin procesar).
        oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1)
        response = client.post(
            "/search",
            files={"image": ("big.png", oversize, "image/png")},
        )
        assert response.status_code == 413
        assert response.json() == {
            "error": "la imagen de consulta supera el límite de 10 MB",
            "error_type": "media_too_large",
        }

        # 415: firma MIME no soportada.
        bogus = make_bogus_file(tmp_path / "fake.png")
        response = _search(client, bogus)
        assert response.status_code == 415
        assert response.json()["error_type"] == "media_type_not_supported"

        # 400: contenido corrupto con firma válida.
        corrupt = make_bogus_file(tmp_path / "corrupt.png", with_png_signature=True)
        response = _search(client, corrupt)
        assert response.status_code == 400
        assert response.json()["error_type"] == "media_corrupt"

        # 400: petición sin parte image.
        response = client.post("/search", data={"top_k": "10"})
        assert response.status_code == 400
        assert response.json()["error_type"] == "missing_file_part"

    assert _searches_rows() == []  # SC-006: nada se registró
    assert list(api_env.iterdir()) == []  # SC-003: ni restos de media


def test_search_min_score_and_top_k_propagate(api_env: Path) -> None:
    """`top_k`/`min_score` del formulario se propagan al pipeline (contracts §1)."""
    query = make_query_image(api_env / "query.png")
    other = make_query_image(api_env / "other.png", size=(80, 60))
    _seed_video(image=query)
    _seed_video(image=other)

    with TestClient(app) as client:
        # min_score alto: solo la coincidencia exacta supera el umbral.
        response = _search(client, query, min_score="0.99")
        assert response.status_code == 200, response.text
        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["match_score"] == 1.0

        # top_k=1: el ANN solo devuelve el frame exacto (1 resultado).
        response = _search(client, query, top_k="1")
        assert response.status_code == 200, response.text
        assert len(response.json()["results"]) == 1

        # Sin resultados por encima del umbral → 200 con results [] (contracts §1).
        unseeded = make_query_image(api_env / "unseeded.png", size=(96, 72))
        response = _search(client, unseeded, min_score="0.99")
        assert response.status_code == 200, response.text
        assert response.json()["results"] == []
