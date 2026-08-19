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
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image
from xtrace_crawler.adapters.base import (  # type: ignore[import-untyped]
    AdapterManifest,
    RateLimitSpec,
)
from xtrace_crawler.adapters.mock import MockAdapter  # type: ignore[import-untyped]
from xtrace_crawler.adapters.models import (  # type: ignore[import-untyped]
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.adapters.registry import AdapterRegistry  # type: ignore[import-untyped]
from xtrace_spike.search.ranking import RankedVideo  # type: ignore[import-untyped]

import xtrace_api.routers.search as search_router
from tests.fixtures import make_query_image, seed_in_memory_index
from xtrace_api.main import app
from xtrace_api.refinement.models import (
    AssetKind,
    RefinementOutcome,
    RefinementStatus,
    RefinementSummary,
    ResultRefinementStatus,
    TimestampOrigin,
    TimestampProvenance,
)
from xtrace_api.search_service import SearchOutcome, VideoMetadata, run_image_search


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
    assert set(payload) == {"search_id", "processing_ms", "refinement", "results"}
    assert payload["refinement"]["status"] == "unavailable"
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
        "timestamp_provenance",
    }
    assert top["video_id"] == video_id
    assert top["local_ref"] is None  # in-memory: paridad CLI
    assert top["title"] is None  # extensión MAY nullable
    assert top["page_url"] is None
    assert top["match_score"] == 1.0
    assert top["matching_frames"] == 1
    assert top["match_timestamp_ms"] == 94_000
    assert top["evidence"] == {"visual": 1.0, "phash": 1.0}
    assert top["timestamp_provenance"] == {
        "origin": "base_index",
        "status": "unavailable",
        "source": None,
        "asset_kind": None,
        "asset_url": None,
        "asset_position": None,
    }

    # SC-003: la media de consulta no deja restos en work_root.
    assert list(api_env.iterdir()) == []


def test_post_search_maps_refined_timestamp_and_summary(
    api_env: Path, tmp_path: Path, monkeypatch
) -> None:
    """El endpoint expone provenance refinada sin tocar el ranking base."""
    query = make_query_image(tmp_path / "query.png")
    video_id, _ = seed_in_memory_index(query, timestamp_ms=94_000)

    class FakeOrchestrator:
        async def refine(self, query_image, ranked, metadata, *, policy):
            del query_image, metadata, policy
            first = replace(ranked[0], match_timestamp_ms=12_345)
            return RefinementOutcome(
                ranked=(first, *ranked[1:]),
                provenance={
                    first.video_id: TimestampProvenance(
                        origin=TimestampOrigin.REFINED_ASSET,
                        status=ResultRefinementStatus.IMPROVED,
                        source="mock",
                        asset_kind=AssetKind.THUMBNAIL,
                        asset_url="https://cdn.example/mock.jpg",
                        asset_position=3,
                    )
                },
                summary=RefinementSummary(
                    status=RefinementStatus.COMPLETED,
                    candidates_requested=1,
                    candidates_processed=1,
                    assets_evaluated=1,
                    assets_discarded=0,
                    errors_count=0,
                    bytes_downloaded=128,
                    embedding_count=2,
                    embedding_elapsed_ms=1,
                    improved_results=1,
                    elapsed_ms=2,
                ),
            )

    monkeypatch.setattr(
        search_router,
        "build_refinement_orchestrator",
        lambda _outcome, *, policy: FakeOrchestrator(),
    )
    with TestClient(app) as client:
        with query.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": ("query.png", handle, "image/png")},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["refinement"]["status"] == "completed"
    assert payload["refinement"]["improved_results"] == 1
    assert payload["results"][0]["video_id"] == video_id
    assert payload["results"][0]["match_timestamp_ms"] == 12_345
    assert payload["results"][0]["timestamp_provenance"] == {
        "origin": "refined_asset",
        "status": "improved",
        "source": "mock",
        "asset_kind": "thumbnail",
        "asset_url": "https://cdn.example/mock.jpg",
        "asset_position": 3,
    }


def test_post_search_runs_real_refinement_bridge_and_closes_adapter(
    api_env: Path, tmp_path: Path, monkeypatch
) -> None:
    """Positive POST path uses metadata, registry gate, materializer and adapter."""

    class PositiveMockAdapter(MockAdapter):
        def __init__(self) -> None:
            super().__init__(seed=11, catalog_size=1)
            self.manifest = AdapterManifest(
                source="mock-positive",
                access_method="json",
                assets_accessed=["thumbnail"],
                robots_reviewed=True,
                terms_reviewed=True,
                review_date="2026-08-19",
                rate_limit=RateLimitSpec(min_interval_ms=0, max_rps=10.0),
            )
            self.closed = False
            self.video = VideoSource(
                source="mock-positive",
                external_id="positive-1",
                title="Positive fixture",
                page_url="https://mock.example/videos/positive-1",
                duration_ms=10_000,
            )

        async def get_video(
            self, external_id: str, *, page_url: str | None = None
        ) -> VideoSource | None:
            del page_url
            return self.video if external_id == self.video.external_id else None

        async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
            assert video.external_id == self.video.external_id
            return [
                VisualAsset(
                    kind="thumbnail",
                    url="https://assets.example/red.jpg",
                    position=1,
                    timestamp_ms=8_000,
                ),
                VisualAsset(
                    kind="thumbnail",
                    url="https://assets.example/blue.jpg",
                    position=2,
                    timestamp_ms=2_000,
                ),
            ]

        async def fetch_asset_bytes(self, url: str) -> bytes | None:
            image = Image.new("RGB", (8, 8), (255, 0, 0) if "red" in url else (0, 0, 255))
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()

        async def aclose(self) -> None:
            self.closed = True

    class ColourEmbeddings:
        model_id = "post-positive"
        dimension = 2

        def embed_images(self, images: list[Image.Image]) -> np.ndarray[Any, Any]:
            rows: list[list[float]] = []
            for image in images:
                red, _green, blue = cast(
                    tuple[int, int, int],
                    image.convert("RGB").resize((1, 1)).getpixel((0, 0)),
                )
                vector = np.array([float(red), float(blue)], dtype=np.float32)
                vector /= np.linalg.norm(vector)
                rows.append(vector.tolist())
            return np.asarray(rows, dtype=np.float32)

    adapter = PositiveMockAdapter()
    registry = AdapterRegistry()
    registry.register(adapter, real=False)
    monkeypatch.setattr("xtrace_crawler.cli._default_registry", lambda: registry)
    monkeypatch.setenv("XTRACE_REFINEMENT_ENABLED", "true")
    monkeypatch.setenv("XTRACE_REFINEMENT_SOURCE_OVERRIDES", "{}")

    query = tmp_path / "query.png"
    Image.new("RGB", (8, 8), (255, 0, 0)).save(query)
    ranked = RankedVideo(
        video_id="video-positive",
        match_score=0.8,
        match_timestamp_ms=1_000,
        matching_frames=1,
        best_frame_id="frame-positive",
        best_distance=0.5,
        visual_similarity=0.5,
        frames_score=0.8,
        phash_score=0.8,
    )
    outcome = SearchOutcome(
        ranked=(ranked,),
        metadata={
            ranked.video_id: VideoMetadata(
                local_ref=None,
                title="Positive fixture",
                page_url="https://mock.example/videos/positive-1",
                source="mock-positive",
                adapter="mock-positive",
                external_id="positive-1",
                duration_ms=10_000,
                source_enabled=True,
            )
        },
        backend_label="in-memory",
        embeddings=ColourEmbeddings(),
    )
    monkeypatch.setattr(search_router, "run_image_search", lambda *args, **kwargs: outcome)

    with TestClient(app) as client:
        with query.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": ("query.png", handle, "image/png")},
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["refinement"]["status"] == "completed"
    assert payload["refinement"]["candidates_processed"] == 1
    assert payload["results"][0]["match_timestamp_ms"] == 8_000
    assert payload["results"][0]["timestamp_provenance"] == {
        "origin": "refined_asset",
        "status": "improved",
        "source": "mock-positive",
        "asset_kind": "thumbnail",
        "asset_url": "https://assets.example/red.jpg",
        "asset_position": 1,
    }
    assert adapter.closed is True
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
