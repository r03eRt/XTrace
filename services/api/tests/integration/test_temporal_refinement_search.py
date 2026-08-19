"""Tests first for service integration over the immutable base ranking."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import numpy as np
from PIL import Image
from xtrace_crawler.adapters.models import VisualAsset  # type: ignore[import-untyped]
from xtrace_spike.search.ranking import RankedVideo  # type: ignore[import-untyped]

from xtrace_api.refinement.assets import MaterializedAsset
from xtrace_api.refinement.catalog import candidate_from_record
from xtrace_api.refinement.policy import RefinementPolicy
from xtrace_api.refinement.service import TemporalRefinementOrchestrator
from xtrace_api.search_service import VideoMetadata


class ColourEmbeddings:
    model_id = "test-colour"
    dimension = 2

    def embed_images(self, images: list[Image.Image]) -> np.ndarray[Any, Any]:
        rows: list[list[float]] = []
        for image in images:
            red, _green, blue = cast(
                tuple[int, int, int], image.convert("RGB").resize((1, 1)).getpixel((0, 0))
            )
            vector = np.array([float(red), float(blue)], dtype=np.float32)
            vector /= np.linalg.norm(vector)
            rows.append(vector.tolist())
        return np.asarray(rows, dtype=np.float32)


def _ranked(video_id: str, *, timestamp_ms: int, visual: float, score: float) -> RankedVideo:
    return RankedVideo(
        video_id=video_id,
        match_score=score,
        match_timestamp_ms=timestamp_ms,
        matching_frames=1,
        best_frame_id=f"frame-{video_id}",
        best_distance=1.0 - visual,
        visual_similarity=visual,
        frames_score=1.0,
        phash_score=1.0,
    )


def _asset(video_id: str, *, timestamp_ms: int) -> MaterializedAsset:
    return MaterializedAsset(
        asset=VisualAsset(
            kind="thumbnail",
            url=f"https://cdn.example/{video_id}.jpg",
            position=1,
            timestamp_ms=timestamp_ms,
        ),
        image=Image.new("RGB", (8, 8), (255, 0, 0)),
        byte_count=128,
    )


def test_orchestrator_limits_candidates_and_preserves_base_order_and_scores() -> None:
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    ranked = tuple(
        _ranked(f"video-{index}", timestamp_ms=100 + index, visual=0.8, score=0.9 - index / 10)
        for index in range(4)
    )
    candidates = {
        item.video_id: candidate_from_record(
            {
                "video_id": item.video_id,
                "source": "mock",
                "adapter": "mock",
                "external_id": item.video_id,
                "page_url": f"https://mock.example/{item.video_id}",
                "duration_ms": 10_000,
                "base_timestamp_ms": item.match_timestamp_ms,
                "base_visual_similarity": item.visual_similarity,
            }
        )
        for item in ranked
    }
    assets = {ranked[0].video_id: [_asset(ranked[0].video_id, timestamp_ms=5_000)]}
    requested: list[str] = []

    async def resolve_assets(candidate: Any) -> tuple[MaterializedAsset, ...]:
        requested.append(candidate.video_id)
        return tuple(assets.get(candidate.video_id, ()))

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda item, _metadata: candidates[item.video_id],
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            query,
            ranked,
            {},
            policy=RefinementPolicy(candidate_limit=2),
        )
    )

    assert requested == ["video-0", "video-1"]
    assert [item.video_id for item in outcome.ranked] == [item.video_id for item in ranked]
    assert [item.match_score for item in outcome.ranked] == [item.match_score for item in ranked]
    assert outcome.ranked[0].match_timestamp_ms == 5_000
    assert outcome.ranked[1].match_timestamp_ms == ranked[1].match_timestamp_ms
    assert outcome.ranked[2].match_timestamp_ms == ranked[2].match_timestamp_ms
    assert outcome.provenance["video-0"].origin == "refined_asset"
    assert outcome.provenance["video-1"].origin == "base_index"


def test_source_override_limits_candidates_before_asset_resolution() -> None:
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    ranked = tuple(
        _ranked(f"video-{index}", timestamp_ms=100 + index, visual=0.8, score=0.9)
        for index in range(3)
    )
    candidates = {
        item.video_id: candidate_from_record(
            {
                "video_id": item.video_id,
                "source": "mock",
                "adapter": "mock",
                "external_id": item.video_id,
                "page_url": None,
                "duration_ms": 10_000,
                "base_timestamp_ms": item.match_timestamp_ms,
                "base_visual_similarity": item.visual_similarity,
            }
        )
        for item in ranked
    }
    metadata = {
        item.video_id: VideoMetadata(
            local_ref=None,
            title=None,
            page_url=None,
            source="mock",
            adapter="mock",
            external_id=item.video_id,
            duration_ms=10_000,
            source_enabled=True,
        )
        for item in ranked
    }
    requested: list[str] = []

    async def resolve_assets(candidate: Any) -> tuple[MaterializedAsset, ...]:
        requested.append(candidate.video_id)
        return ()

    orchestrator = TemporalRefinementOrchestrator(
        embeddings=ColourEmbeddings(),
        candidate_resolver=lambda item, _metadata: candidates[item.video_id],
        asset_resolver=resolve_assets,
    )
    outcome = asyncio.run(
        orchestrator.refine(
            query,
            ranked,
            metadata,
            policy=RefinementPolicy(
                candidate_limit=3,
                source_overrides={"mock": {"candidate_limit": 1}},
            ),
        )
    )

    assert requested == ["video-0"]
    assert outcome.summary.candidates_requested == 1
    assert outcome.summary.candidates_processed == 1
    assert outcome.summary.status == "limited"
