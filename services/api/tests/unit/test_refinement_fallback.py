"""Fallback and cancellation contracts for temporal refinement (TASK-006-T015)."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

import httpx
import numpy as np
import pytest
from PIL import Image
from xtrace_crawler.adapters.models import VisualAsset  # type: ignore[import-untyped]
from xtrace_spike.search.ranking import RankedVideo  # type: ignore[import-untyped]

from xtrace_api.refinement.assets import (
    AssetMaterializer,
    MaterializationResult,
    MaterializedAsset,
)
from xtrace_api.refinement.catalog import candidate_from_record
from xtrace_api.refinement.models import ResultRefinementStatus
from xtrace_api.refinement.policy import RefinementPolicy
from xtrace_api.refinement.service import TemporalRefinementOrchestrator


class ColourEmbeddings:
    model_id = "fallback-test"
    dimension = 2

    def embed_images(self, images: list[Image.Image]) -> np.ndarray[Any, Any]:
        rows: list[list[float]] = []
        for image in images:
            pixel = cast(
                tuple[int, int, int], image.convert("RGB").resize((1, 1)).getpixel((0, 0))
            )
            red, _green, blue = pixel
            vector = np.array([float(red), float(blue)], dtype=np.float32)
            vector /= np.linalg.norm(vector)
            rows.append(vector.tolist())
        return np.asarray(rows, dtype=np.float32)


@dataclass(frozen=True)
class CandidateFixture:
    ranked: RankedVideo
    candidate: Any


def _fixture() -> CandidateFixture:
    ranked = RankedVideo(
        video_id="video-fallback",
        match_score=0.9,
        match_timestamp_ms=1_000,
        matching_frames=1,
        best_frame_id="frame-fallback",
        best_distance=0.2,
        visual_similarity=0.8,
        frames_score=1.0,
        phash_score=1.0,
    )
    candidate = candidate_from_record(
        {
            "video_id": ranked.video_id,
            "source": "mock",
            "adapter": "mock",
            "external_id": "mock-1",
            "page_url": None,
            "duration_ms": 10_000,
            "base_timestamp_ms": ranked.match_timestamp_ms,
            "base_visual_similarity": ranked.visual_similarity,
        }
    )
    return CandidateFixture(ranked=ranked, candidate=candidate)


def _asset(
    *, url: str, timestamp_ms: int | None, colour: tuple[int, int, int]
) -> MaterializedAsset:
    return MaterializedAsset(
        asset=VisualAsset(
            kind="thumbnail",
            url=url,
            position=1,
            timestamp_ms=timestamp_ms,
        ),
        image=Image.new("RGB", (8, 8), colour),
        byte_count=32,
    )


@pytest.mark.parametrize("status_code", [403, 404])
def test_source_http_errors_preserve_base_result(status_code: int) -> None:
    fixture = _fixture()
    request = httpx.Request("GET", "https://cdn.example/asset.jpg")
    response = httpx.Response(status_code, request=request)

    async def resolve_assets(_candidate: Any) -> tuple[MaterializedAsset, ...]:
        raise httpx.HTTPStatusError(
            f"HTTP {status_code}", request=request, response=response
        )

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(),
        )
    )

    assert outcome.ranked == (fixture.ranked,)
    assert outcome.ranked[0].match_timestamp_ms == 1_000
    assert outcome.provenance[fixture.ranked.video_id].status == ResultRefinementStatus.UNAVAILABLE
    assert outcome.summary.errors_count == 1


def test_candidate_timeout_preserves_base_and_marks_limited() -> None:
    fixture = _fixture()

    async def resolve_assets(_candidate: Any) -> tuple[MaterializedAsset, ...]:
        await asyncio.sleep(0.05)
        return ()

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(search_timeout_ms=100, candidate_timeout_ms=10),
        )
    )

    assert outcome.ranked[0].match_timestamp_ms == 1_000
    assert outcome.summary.status == "limited"
    assert outcome.provenance[fixture.ranked.video_id].status == ResultRefinementStatus.LIMITED


def test_global_timeout_covers_candidate_resolution() -> None:
    fixture = _fixture()

    async def resolve_candidate(_item: Any, _metadata: Any) -> Any:
        await asyncio.sleep(0.05)
        return fixture.candidate

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=resolve_candidate,
        asset_resolver=lambda _candidate: _empty_assets(),
    )
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(search_timeout_ms=10, candidate_timeout_ms=10),
        )
    )

    assert outcome.ranked[0].match_timestamp_ms == 1_000
    assert outcome.summary.status == "limited"
    assert outcome.summary.candidates_processed == 1
    assert outcome.provenance[fixture.ranked.video_id].status == ResultRefinementStatus.LIMITED


def test_candidate_timeout_also_covers_synchronous_embedding_evaluation() -> None:
    fixture = _fixture()
    asset = _asset(
        url="https://cdn.example/valid.jpg",
        timestamp_ms=2_000,
        colour=(255, 0, 0),
    )

    finished = threading.Event()

    class SlowEmbeddings(ColourEmbeddings):
        def embed_images(self, images: list[Image.Image]) -> np.ndarray[Any, Any]:
            try:
                time.sleep(0.2)
                return super().embed_images(images)
            finally:
                finished.set()

    async def resolve_assets(_candidate: Any) -> tuple[MaterializedAsset, ...]:
        return (asset,)

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=SlowEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )
    started = time.monotonic()
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(search_timeout_ms=100, candidate_timeout_ms=10),
        )
    )
    elapsed = time.monotonic() - started

    assert outcome.ranked[0].match_timestamp_ms == 1_000
    assert outcome.summary.status == "limited"
    # The request budget must bound the caller even while the blocking provider
    # finishes and closes its assets in the bounded background executor.
    assert elapsed < 0.15
    assert finished.wait(1.0)
    assert outcome.provenance[fixture.ranked.video_id].status == ResultRefinementStatus.LIMITED


def test_corrupt_materialization_and_invalid_assets_degrade_without_interpolation() -> None:
    fixture = _fixture()
    valid = _asset(
        url="https://cdn.example/valid.jpg",
        timestamp_ms=2_000,
        colour=(255, 0, 0),
    )
    assets = (
        valid,
        _asset(url="https://cdn.example/duplicate.jpg", timestamp_ms=2_000, colour=(255, 0, 0)),
        _asset(url="https://cdn.example/duplicate.jpg", timestamp_ms=2_000, colour=(0, 0, 255)),
        _asset(url="https://cdn.example/out-of-range.jpg", timestamp_ms=20_000, colour=(255, 0, 0)),
        _asset(url="https://cdn.example/missing-ts.jpg", timestamp_ms=None, colour=(255, 0, 0)),
    )

    async def resolve_assets(_candidate: Any) -> MaterializationResult:
        return MaterializationResult(assets=assets, discarded_count=1, bytes_downloaded=32)

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(),
        )
    )

    assert outcome.ranked[0].match_timestamp_ms == 1_000
    assert outcome.provenance[fixture.ranked.video_id].status == ResultRefinementStatus.UNCHANGED
    assert outcome.summary.assets_evaluated == 2
    assert outcome.summary.assets_discarded == 4
    with pytest.raises(ValueError):
        _ = valid.image.im


def test_refined_provenance_sanitises_public_asset_url() -> None:
    fixture = _fixture()
    selected = _asset(
        url="https://cdn.example/path/frame.jpg?token=secret#fragment",
        timestamp_ms=2_000,
        colour=(255, 0, 0),
    )

    async def resolve_assets(_candidate: Any) -> tuple[MaterializedAsset, ...]:
        return (selected,)

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(),
        )
    )

    assert outcome.provenance[fixture.ranked.video_id].asset_url == (
        "https://cdn.example/path/frame.jpg"
    )


@pytest.mark.parametrize("rejection", ["corrupt", "pixels", "bytes"])
def test_materializer_rejections_flow_to_base_fallback(rejection: str) -> None:
    fixture = _fixture()
    asset = VisualAsset(
        kind="thumbnail",
        url=f"https://cdn.example/{rejection}.jpg",
        position=1,
        timestamp_ms=2_000,
    )

    image_buffer = BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(image_buffer, format="PNG")
    valid_bytes = image_buffer.getvalue()

    async def fetch(_asset: VisualAsset) -> bytes:
        if rejection == "corrupt":
            return b"not-an-image"
        return valid_bytes

    materializer = AssetMaterializer(fetch, max_pixels=10 if rejection == "pixels" else 25_000_000)

    async def resolve_assets(_candidate: Any) -> MaterializationResult:
        return await materializer.materialize(
            [asset],
            max_assets=1,
            max_bytes=1 if rejection == "bytes" else 10 * 1024 * 1024,
        )

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            Image.new("RGB", (8, 8), (255, 0, 0)),
            (fixture.ranked,),
            {},
            policy=RefinementPolicy(),
        )
    )

    assert outcome.ranked[0].match_timestamp_ms == fixture.ranked.match_timestamp_ms
    assert outcome.provenance[fixture.ranked.video_id].status == ResultRefinementStatus.UNAVAILABLE
    assert outcome.summary.assets_evaluated == 0
    assert outcome.summary.assets_discarded == 1


async def _empty_assets() -> tuple[MaterializedAsset, ...]:
    return ()


def test_cancellation_propagates_and_cleans_resolver() -> None:
    fixture = _fixture()
    cleanup_called = False

    async def resolve_assets(_candidate: Any) -> tuple[MaterializedAsset, ...]:
        nonlocal cleanup_called
        try:
            await asyncio.sleep(1)
        finally:
            cleanup_called = True
        return ()

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda _item, _metadata: fixture.candidate,
        asset_resolver=resolve_assets,
    )

    async def run_and_cancel() -> None:
        task = asyncio.create_task(
            orchestrator.refine(
                Image.new("RGB", (8, 8), (255, 0, 0)),
                (fixture.ranked,),
                {},
                policy=RefinementPolicy(),
            )
        )
        # Candidate resolution runs in a worker so the global deadline can
        # interrupt synchronous catalog lookups; give it one scheduling turn
        # before cancelling the in-flight asset resolver.
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())
    assert cleanup_called
