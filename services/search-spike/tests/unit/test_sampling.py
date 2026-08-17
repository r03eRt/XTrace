"""Pruebas de la política de muestreo adaptativo (TASK-005-001).

Las pruebas son deliberadamente independientes de FFmpeg: la política solo recibe
duración, posiciones y una secuencia de assets ya permitidos.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.unit.test_dedupe import _synthetic_image, _write_frame
from xtrace_spike.ingest.dedupe import dedupe_frames
from xtrace_spike.sampling import AdaptiveSamplingPolicy, select_representative_frames


@dataclass(frozen=True)
class _Frame:
    frame_id: str
    timestamp_ms: int | None


def _frames(*timestamps: int | None) -> list[_Frame]:
    return [
        _Frame(frame_id=f"f{i}", timestamp_ms=timestamp) for i, timestamp in enumerate(timestamps)
    ]


def test_target_count_is_monotonic_and_bounded() -> None:
    policy = AdaptiveSamplingPolicy()

    counts = [policy.target_count(duration) for duration in (1, 119_999, 120_000, 240_001, 960_000)]

    assert counts == sorted(counts)
    assert counts[0] == 1
    assert counts[-1] == 8
    assert all(1 <= count <= 8 for count in counts)


def test_target_count_respects_available_assets() -> None:
    policy = AdaptiveSamplingPolicy()

    assert policy.target_count(960_000, available_count=3) == 3
    assert policy.target_count(960_000, available_count=0) == 0
    assert policy.target_count(None, available_count=2) == 2
    assert policy.target_count(None) == 8


def test_ideal_timestamps_are_centered_and_uniform() -> None:
    policy = AdaptiveSamplingPolicy()

    timestamps = policy.ideal_timestamps(600_000)

    assert timestamps == (60_000, 180_000, 300_000, 420_000, 540_000)
    assert timestamps == tuple(sorted(set(timestamps)))
    assert all(0 <= timestamp < 600_000 for timestamp in timestamps)


def test_selection_uses_available_assets_once_and_keeps_temporal_order() -> None:
    policy = AdaptiveSamplingPolicy()
    frames = _frames(0, 100_000, 200_000, 300_000, 400_000, 500_000, 500_000)

    selected = select_representative_frames(
        frames,
        duration_ms=600_000,
        timestamp=lambda frame: frame.timestamp_ms,
        policy=policy,
    )

    assert len(selected) == 5
    assert [frame.timestamp_ms for frame in selected] == sorted(
        {frame.timestamp_ms for frame in selected}
    )
    assert len({frame.timestamp_ms for frame in selected}) == len(selected)


def test_dedupe_before_sampling_keeps_complete_unique_temporal_coverage(
    tmp_path: Path,
) -> None:
    """FR-006/SC-002: duplicados se quitan antes sin perder los puntos únicos."""
    # Reuse the deterministic perceptual fixtures from the existing dedupe suite:
    # every source image appears twice at different positions.
    images = [_synthetic_image(seed) for seed in (1, 2, 3, 4, 5)]
    frames = tuple(
        _write_frame(
            tmp_path,
            "adaptive",
            timestamp,
            images[index // 2],
        )
        for index, timestamp in enumerate(
            (0, 75_000, 150_000, 225_000, 300_000, 375_000, 450_000, 525_000, 550_000, 575_000)
        )
    )
    deduped = dedupe_frames(frames)
    selected = select_representative_frames(
        deduped,
        duration_ms=600_000,
        timestamp=lambda frame: frame.timestamp_ms,
        policy=AdaptiveSamplingPolicy(),
    )

    assert len(deduped) == 5
    assert len(selected) == 5
    assert selected[0].timestamp_ms == 0
    assert selected[-1].timestamp_ms >= 450_000


def test_selection_falls_back_to_all_assets_when_assets_are_scarce() -> None:
    policy = AdaptiveSamplingPolicy()
    frames = _frames(10_000, 300_000, None)

    selected = select_representative_frames(
        frames,
        duration_ms=960_000,
        timestamp=lambda frame: frame.timestamp_ms,
        policy=policy,
    )

    assert [frame.frame_id for frame in selected] == ["f0", "f1", "f2"]
    assert len(selected) == 3


def test_selection_keeps_unknown_timestamps_without_inventing_precision() -> None:
    policy = AdaptiveSamplingPolicy()
    frames = _frames(None, -1, 10_000, 999_999)

    selected = select_representative_frames(
        frames,
        duration_ms=960_000,
        timestamp=lambda frame: frame.timestamp_ms,
        policy=policy,
    )

    assert len(selected) == 4
    assert [frame.timestamp_ms for frame in selected] == [10_000, None, None, None]


def test_unknown_duration_preserves_stable_asset_order_and_caps_at_eight() -> None:
    policy = AdaptiveSamplingPolicy()
    frames = _frames(None, 20_000, None, 10_000, None, 30_000, 40_000, 50_000, 60_000, 70_000)

    selected = select_representative_frames(
        frames,
        duration_ms=None,
        timestamp=lambda frame: frame.timestamp_ms,
        policy=policy,
    )

    assert len(selected) == 8
    assert [frame.timestamp_ms for frame in selected] == [
        10_000,
        20_000,
        30_000,
        40_000,
        50_000,
        60_000,
        70_000,
        None,
    ]


def test_non_positive_duration_keeps_non_negative_source_timestamps() -> None:
    """FR-007: duración no fiable no invalida posiciones que sí aporta la fuente."""
    policy = AdaptiveSamplingPolicy()
    frames = _frames(-1, 0, 10_000, None)

    for duration_ms in (0, -1):
        selected = select_representative_frames(
            frames,
            duration_ms=duration_ms,
            timestamp=lambda frame: frame.timestamp_ms,
            policy=policy,
        )
        assert [frame.timestamp_ms for frame in selected] == [0, 10_000, None, None]


def test_policy_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="target_interval_ms"):
        AdaptiveSamplingPolicy(target_interval_ms=0)
    with pytest.raises(ValueError, match="max_frames"):
        AdaptiveSamplingPolicy(max_frames=9)


def test_ideal_timestamps_reject_unknown_duration() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        AdaptiveSamplingPolicy().ideal_timestamps(0)
