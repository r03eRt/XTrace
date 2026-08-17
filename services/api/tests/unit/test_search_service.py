"""Tests de la cadena de búsqueda y del endpoint feliz en modo in-memory
(PR-055 · FR-001/004/005 · NFR-002 · SC-001/003 · contracts §1).

La cadena `run_image_search` se testea con `InMemoryVectorStore` +
`FakeEmbeddingProvider` (deterministas, ADR-0007 — misma pareja que los
tests de la CLI): el índice se siembra con los mismos componentes de la
cadena real (Fake D=768 + compute_phash + `build_backend` del spike), de
modo que la búsqueda es exacta (distancia 0, match_score 1.0, evidencia
pHash 1.0) — paridad con los tests de la CLI del spike.

El endpoint feliz (TestClient, sin DB) verifica el JSON del contracts §1 con
la extensión MAY null, `search_id` único por búsqueda y SC-003 (work_root
sin restos tras el éxito).
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from tests.fixtures import make_query_image, seed_in_memory_index
from xtrace_api.main import app
from xtrace_api.search_service import run_image_search


def _open_query_image(path: Path) -> Image.Image:
    """Abre la imagen como la cadena real (helper de test)."""
    with Image.open(path) as image:
        image.load()
        return image.convert("RGB")


def test_run_image_search_finds_seeded_video_exactly(api_env: Path) -> None:
    """Cadena del spike: frame idéntico → video_id correcto con score 1.0 (FR-001/005)."""
    query = make_query_image(api_env / "query.png")
    video_id, _ = seed_in_memory_index(query, timestamp_ms=94_000)

    outcome = run_image_search(_open_query_image(query), top_k=10, min_score=0.0)

    assert len(outcome.ranked) == 1
    top = outcome.ranked[0]
    assert top.video_id == video_id
    assert top.match_score == 1.0
    assert top.matching_frames == 1
    assert top.match_timestamp_ms == 94_000
    assert top.visual_similarity == 1.0
    assert top.phash_score == 1.0
    assert outcome.metadata == {}  # in-memory: sin metadatos (paridad PR-014)


def test_run_image_search_empty_index_returns_no_results(api_env: Path) -> None:
    """Índice vacío → results vacíos (contracts §1: no es un error)."""
    query = make_query_image(api_env / "query.png")
    outcome = run_image_search(_open_query_image(query), top_k=10, min_score=0.0)
    assert outcome.ranked == ()


def test_run_image_search_min_score_filters_weak_matches(api_env: Path) -> None:
    """min_score descarta coincidencias débiles y conserva la exacta (SC-002)."""
    exact = make_query_image(api_env / "exact.png")
    other = make_query_image(api_env / "other.png", size=(80, 60))
    seed_in_memory_index(exact)
    seed_in_memory_index(other)

    outcome = run_image_search(_open_query_image(exact), top_k=10, min_score=0.99)
    assert len(outcome.ranked) == 1
    assert outcome.ranked[0].match_score == 1.0

    outcome_all = run_image_search(_open_query_image(exact), top_k=10, min_score=0.0)
    assert len(outcome_all.ranked) == 2  # la exacta primero, la débil después


def test_run_image_search_top_k_limits_ann(api_env: Path) -> None:
    """top_k limita los frames candidatos del ANN (paridad con la CLI)."""
    exact = make_query_image(api_env / "exact.png")
    other = make_query_image(api_env / "other.png", size=(80, 60))
    seed_in_memory_index(exact)
    seed_in_memory_index(other)

    outcome = run_image_search(_open_query_image(exact), top_k=1, min_score=0.0)
    assert len(outcome.ranked) == 1  # solo el frame exacto entra en el ANN


def test_post_search_happy_path_contract(api_env: Path, tmp_path: Path) -> None:
    """POST /search feliz: JSON del contracts §1 + extensión MAY null (FR-004)."""
    query = make_query_image(tmp_path / "query.png")
    video_id, _ = seed_in_memory_index(query, timestamp_ms=94_000)

    with TestClient(app) as client:
        with query.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": ("query.png", handle, "image/png")},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"search_id", "processing_ms", "results"}
    assert uuid.UUID(payload["search_id"]).version == 4
    assert isinstance(payload["processing_ms"], int)
    assert payload["processing_ms"] >= 0

    assert len(payload["results"]) == 1
    top = payload["results"][0]
    assert set(top) == {
        "video_id",
        "local_ref",
        "title",
        "page_url",
        "match_score",
        "matching_frames",
        "match_timestamp_ms",
        "evidence",
    }
    assert top["video_id"] == video_id
    assert top["local_ref"] is None  # in-memory: paridad CLI
    assert top["title"] is None  # extensión MAY nullable
    assert top["page_url"] is None
    assert top["match_score"] == 1.0
    assert top["matching_frames"] == 1
    assert top["match_timestamp_ms"] == 94_000
    assert top["evidence"] == {"visual": 1.0, "phash": 1.0}

    # SC-003: la media de consulta no deja restos en work_root.
    assert list(api_env.iterdir()) == []


def test_post_search_returns_unique_search_ids(api_env: Path) -> None:
    """Cada búsqueda tiene su propio search_id (contracts §1: uuid único)."""
    query = make_query_image(api_env / "query.png")
    seed_in_memory_index(query)

    ids: list[str] = []
    with TestClient(app) as client:
        for _ in range(3):
            with query.open("rb") as handle:
                response = client.post(
                    "/search",
                    files={"image": ("query.png", handle, "image/png")},
                )
            assert response.status_code == 200, response.text
            ids.append(response.json()["search_id"])

    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_post_search_concurrent_requests_get_unique_ids(api_env: Path) -> None:
    """Búsquedas concurrentes → search_id independientes (edge case spec)."""
    query = make_query_image(api_env / "query.png")
    seed_in_memory_index(query)

    def run_search() -> str:
        with TestClient(app) as client:
            with query.open("rb") as handle:
                response = client.post(
                    "/search",
                    files={"image": ("query.png", handle, "image/png")},
                )
        assert response.status_code == 200, response.text
        return response.json()["search_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: run_search(), range(4)))

    assert len(ids) == 4
    assert len(set(ids)) == 4


def test_post_search_defaults_match_cli_contract(api_env: Path) -> None:
    """Sin top_k/min_score se usan los defaults de la CLI (10 / 0.0, contracts §1)."""
    query = make_query_image(api_env / "query.png")
    video_id, _ = seed_in_memory_index(query)

    with TestClient(app) as client:
        with query.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": ("query.png", handle, "image/png")},
                data={"top_k": "10", "min_score": "0.0"},
            )

    assert response.status_code == 200, response.text
    assert [item["video_id"] for item in response.json()["results"]] == [video_id]
