"""Tests first for deterministic in-memory temporal evaluation (TASK-006-T008)."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from xtrace_crawler.adapters.models import VisualAsset

from xtrace_api.refinement.assets import MaterializedAsset
from xtrace_api.refinement.evaluator import TemporalRefinementEvaluator


class RecordingEmbeddingProvider:
    model_id = "test-colour"
    dimension = 2

    def __init__(self) -> None:
        self.calls = 0
        self.batch_sizes: list[int] = []

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        self.calls += 1
        self.batch_sizes.append(len(images))
        vectors: list[list[float]] = []
        for image in images:
            red, _green, blue = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
            vector = np.array([float(red), float(blue)], dtype=np.float32)
            vector /= np.linalg.norm(vector)
            vectors.append(vector.tolist())
        return np.asarray(vectors, dtype=np.float32)


def _asset(
    *, url: str, timestamp_ms: int | None, position: int | None, colour: tuple[int, int, int]
) -> MaterializedAsset:
    return MaterializedAsset(
        asset=VisualAsset(
            kind="thumbnail",
            url=url,
            timestamp_ms=timestamp_ms,
            position=position,
        ),
        image=Image.new("RGB", (8, 8), colour),
        byte_count=64,
    )


def test_evaluator_batches_assets_and_selects_strict_visual_improvement() -> None:
    provider = RecordingEmbeddingProvider()
    evaluator = TemporalRefinementEvaluator(provider)
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    assets = [
        _asset(
            url="https://cdn.example/red.jpg",
            timestamp_ms=7_340,
            position=7,
            colour=(255, 0, 0),
        ),
        _asset(
            url="https://cdn.example/blue.jpg",
            timestamp_ms=9_000,
            position=9,
            colour=(0, 0, 255),
        ),
    ]

    result = evaluator.evaluate(
        query,
        assets,
        base_timestamp_ms=1_200,
        base_visual_similarity=0.95,
        duration_ms=10_000,
    )

    assert result.timestamp_ms == 7_340
    assert result.selected_asset is assets[0].asset
    assert result.similarity > 0.95
    assert result.status == "improved"
    assert provider.calls == 2
    assert provider.batch_sizes == [1, 2]


def test_evaluator_keeps_base_when_visual_guard_is_not_strictly_better() -> None:
    provider = RecordingEmbeddingProvider()
    evaluator = TemporalRefinementEvaluator(provider)
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    asset = _asset(
        url="https://cdn.example/same.jpg",
        timestamp_ms=8_000,
        position=8,
        colour=(255, 0, 0),
    )

    result = evaluator.evaluate(
        query,
        [asset],
        base_timestamp_ms=1_200,
        base_visual_similarity=1.0,
        duration_ms=10_000,
    )

    assert result.timestamp_ms == 1_200
    assert result.selected_asset is None
    assert result.status == "unchanged"


def test_evaluator_uses_the_same_cosine_scale_as_the_base_index() -> None:
    provider = RecordingEmbeddingProvider()
    evaluator = TemporalRefinementEvaluator(provider)
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    orthogonal = _asset(
        url="https://cdn.example/orthogonal.jpg",
        timestamp_ms=8_000,
        position=8,
        colour=(0, 0, 255),
    )

    result = evaluator.evaluate(
        query,
        [orthogonal],
        base_timestamp_ms=1_200,
        base_visual_similarity=0.4,
        duration_ms=10_000,
    )

    assert result.similarity == 0.4
    assert result.timestamp_ms == 1_200
    assert result.status == "unchanged"


def test_evaluator_deduplicates_and_discards_unusable_timestamps_without_interpolation() -> None:
    provider = RecordingEmbeddingProvider()
    evaluator = TemporalRefinementEvaluator(provider)
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    duplicate_a = _asset(
        url="https://cdn.example/duplicate.jpg",
        timestamp_ms=5_000,
        position=5,
        colour=(255, 0, 0),
    )
    duplicate_b = _asset(
        url="https://cdn.example/duplicate.jpg",
        timestamp_ms=5_000,
        position=6,
        colour=(0, 0, 255),
    )
    assets = [
        duplicate_a,
        duplicate_b,
        _asset(
            url="https://cdn.example/out-of-range.jpg",
            timestamp_ms=12_000,
            position=12,
            colour=(255, 0, 0),
        ),
        _asset(
            url="https://cdn.example/no-position.jpg",
            timestamp_ms=6_000,
            position=None,
            colour=(255, 0, 0),
        ),
        _asset(
            url="https://cdn.example/no-timestamp.jpg",
            timestamp_ms=None,
            position=13,
            colour=(255, 0, 0),
        ),
    ]

    result = evaluator.evaluate(
        query,
        assets,
        base_timestamp_ms=1_200,
        base_visual_similarity=1.0,
        duration_ms=10_000,
    )

    assert result.timestamp_ms == 1_200
    assert result.evaluated_count == 2
    assert result.discarded_count == 3
    assert result.status == "unchanged"


def test_evaluator_rejects_timestamp_equal_to_duration_boundary() -> None:
    provider = RecordingEmbeddingProvider()
    evaluator = TemporalRefinementEvaluator(provider)
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    at_duration = _asset(
        url="https://cdn.example/at-duration.jpg",
        timestamp_ms=10_000,
        position=10,
        colour=(255, 0, 0),
    )

    result = evaluator.evaluate(
        query,
        [at_duration],
        base_timestamp_ms=1_200,
        base_visual_similarity=0.5,
        duration_ms=10_000,
    )

    assert result.timestamp_ms == 1_200
    assert result.selected_asset is None
    assert result.evaluated_count == 0
    assert result.discarded_count == 1
    assert result.status == "unchanged"


def test_evaluator_is_idempotent_and_keeps_base_on_equal_similarity_tie() -> None:
    provider = RecordingEmbeddingProvider()
    evaluator = TemporalRefinementEvaluator(provider)
    query = Image.new("RGB", (8, 8), (255, 0, 0))
    assets = [
        _asset(
            url="https://cdn.example/tie-a.jpg",
            timestamp_ms=3_000,
            position=3,
            colour=(255, 0, 0),
        ),
        _asset(
            url="https://cdn.example/tie-b.jpg",
            timestamp_ms=4_000,
            position=4,
            colour=(255, 0, 0),
        ),
    ]

    first = evaluator.evaluate(
        query,
        [
            _asset(
                url=item.asset.url,
                timestamp_ms=item.asset.timestamp_ms,
                position=item.asset.position,
                colour=(255, 0, 0),
            )
            for item in assets
        ],
        base_timestamp_ms=1_200,
        base_visual_similarity=0.5,
        duration_ms=10_000,
    )
    second = evaluator.evaluate(
        query,
        [
            _asset(
                url=item.asset.url,
                timestamp_ms=item.asset.timestamp_ms,
                position=item.asset.position,
                colour=(255, 0, 0),
            )
            for item in assets
        ],
        base_timestamp_ms=1_200,
        base_visual_similarity=0.5,
        duration_ms=10_000,
    )

    assert first.timestamp_ms == second.timestamp_ms == 1_200
    assert first.selected_asset is second.selected_asset is None
    assert first.status == second.status == "unchanged"


def test_evaluator_closes_asset_images_on_success_fallback_and_exception() -> None:
    query = Image.new("RGB", (8, 8), (255, 0, 0))

    def assert_closed(asset: MaterializedAsset) -> None:
        with pytest.raises(ValueError):
            _ = asset.image.im

    success_asset = _asset(
        url="https://cdn.example/close-success.jpg",
        timestamp_ms=2_000,
        position=2,
        colour=(255, 0, 0),
    )
    TemporalRefinementEvaluator(RecordingEmbeddingProvider()).evaluate(
        query,
        [success_asset],
        base_timestamp_ms=1_000,
        base_visual_similarity=0.5,
        duration_ms=10_000,
    )
    assert_closed(success_asset)

    fallback_asset = _asset(
        url="https://cdn.example/close-fallback.jpg",
        timestamp_ms=2_000,
        position=2,
        colour=(255, 0, 0),
    )
    TemporalRefinementEvaluator(RecordingEmbeddingProvider()).evaluate(
        query,
        [fallback_asset],
        base_timestamp_ms=1_000,
        base_visual_similarity=1.0,
        duration_ms=10_000,
    )
    assert_closed(fallback_asset)

    class ExplodingProvider(RecordingEmbeddingProvider):
        def embed_images(self, images: list[Image.Image]) -> np.ndarray:
            if self.calls:
                raise RuntimeError("embedding failure")
            return super().embed_images(images)

    exception_asset = _asset(
        url="https://cdn.example/close-exception.jpg",
        timestamp_ms=2_000,
        position=2,
        colour=(255, 0, 0),
    )
    with pytest.raises(RuntimeError):
        TemporalRefinementEvaluator(ExplodingProvider()).evaluate(
            query,
            [exception_asset],
            base_timestamp_ms=1_000,
            base_visual_similarity=0.5,
            duration_ms=10_000,
        )
    assert_closed(exception_asset)
