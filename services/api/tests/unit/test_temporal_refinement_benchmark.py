"""Tests first for the paired temporal-refinement benchmark (TASK-006-T022).

The fixtures deliberately contain metadata only.  They model independent
temporal truth and deterministic first/refinement observations without opening
network sockets, decoding a video, or writing a benchmark artifact to Git.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from xtrace_api.refinement.benchmark import (
    BenchmarkError,
    TemporalBenchmarkCase,
    TemporalBenchmarkObservation,
    compare_refinement_policies,
    load_temporal_benchmark_manifest,
    validate_temporal_benchmark_coverage,
)

_DURATION_BUCKETS = (240_000, 600_000, 1_200_000)
_SOURCES = ("local", "web")


def _cases() -> tuple[TemporalBenchmarkCase, ...]:
    """Build 30 unique positive cases: 5 per source/duration segment."""

    cases: list[TemporalBenchmarkCase] = []
    index = 0
    for source in _SOURCES:
        for duration_ms in _DURATION_BUCKETS:
            for _ in range(5):
                cases.append(
                    TemporalBenchmarkCase(
                        case_id=f"positive-{index:03d}",
                        expected_video_id=f"video-{index:03d}",
                        source=source,
                        duration_ms=duration_ms,
                        truth_timestamp_ms=duration_ms // 2,
                    )
                )
                index += 1
    return tuple(cases)


def _observations(
    cases: Sequence[TemporalBenchmarkCase], *, refined: bool
) -> tuple[TemporalBenchmarkObservation, ...]:
    """Return deterministic paired observations with a measurable improvement."""

    observations: list[TemporalBenchmarkObservation] = []
    for index, case in enumerate(cases):
        assert case.expected_video_id is not None
        assert case.truth_timestamp_ms is not None
        # Keep one expected video at rank 2 in the refined pass.  This proves
        # Top-1 and Top-5 are measured independently while Top-5 remains safe.
        ranked: tuple[str, ...] = (
            (f"other-{index}", case.expected_video_id, f"other-{index}-2")
            if refined and index == len(cases) - 1
            else (case.expected_video_id, f"other-{index}", f"other-{index}-2")
        )
        observations.append(
            TemporalBenchmarkObservation(
                case_id=case.case_id,
                ranked_video_ids=ranked,
                predicted_timestamp_ms=case.truth_timestamp_ms + (1_000 if refined else 10_000),
                assets_evaluated=4 if refined else 0,
                bytes_downloaded=16_384 if refined else 0,
                embedding_count=4 if refined else 1,
                elapsed_ms=80 if refined else 20,
            )
        )
    return tuple(observations)


def test_valid_paired_benchmark_measures_quality_cost_and_latency() -> None:
    """FR-014/NFR-003/SC-001..003/SC-007: compare the same 30 positives."""

    cases = _cases()
    report = compare_refinement_policies(
        cases,
        base=_observations(cases, refined=False),
        refined=_observations(cases, refined=True),
    )

    assert report.accepted is True
    assert report.coverage.valid is True
    assert report.coverage.positive_case_count == 30
    assert report.coverage.unique_positive_case_count == 30
    assert report.policies["base"].top1 == 1.0
    assert report.policies["base"].top5 == 1.0
    assert report.policies["refined"].top1 == pytest.approx(29 / 30)
    assert report.policies["refined"].top5 == 1.0

    # Independent truth is 10 seconds from the base timestamp and 1 second
    # from the refined timestamp for every pair.
    assert report.policies["base"].temporal_error_ms == {"median": 10_000, "p95": 10_000}
    assert report.policies["refined"].temporal_error_ms == {"median": 1_000, "p95": 1_000}
    assert report.policies["refined"].cost == {
        "assets_evaluated": 120,
        "bytes_downloaded": 491_520,
        "embedding_count": 120,
    }
    assert report.policies["base"].cost == {
        "assets_evaluated": 0,
        "bytes_downloaded": 0,
        "embedding_count": 30,
    }
    assert report.policies["base"].latency_ms == {"p50": 20, "p95": 20}
    assert report.policies["refined"].latency_ms == {"p50": 80, "p95": 80}

    assert report.top5_loss_percentage_points == 0.0
    assert report.gates["SC-001"] is True
    assert report.gates["SC-002"] is True
    assert report.gates["SC-003"] is True
    assert report.gates["SC-007"] is True
    assert set(report.segments) == {
        "local/<5m",
        "local/5-15m",
        "local/>15m",
        "web/<5m",
        "web/5-15m",
        "web/>15m",
    }
    local_short = report.segments["local/<5m"]
    assert local_short.base is not None
    assert local_short.refined is not None
    assert local_short.base.top5 == 1.0
    assert local_short.refined.temporal_error_ms["median"] == 1_000


def test_equal_temporal_error_counts_as_acceptable_refinement() -> None:
    """SC-001 accepts a deterministic fallback that preserves temporal error."""

    cases = _cases()
    refined = list(_observations(cases, refined=True))
    truth_timestamp_ms = cases[0].truth_timestamp_ms
    assert truth_timestamp_ms is not None
    refined[0] = replace(refined[0], predicted_timestamp_ms=truth_timestamp_ms + 10_000)

    report = compare_refinement_policies(
        cases,
        base=_observations(cases, refined=False),
        refined=tuple(refined),
    )

    assert report.temporal_improvement_percentage == 100.0
    assert report.gates["SC-001"] is True
    assert report.accepted is True


def test_temporal_improvement_below_eighty_percent_fails_adoption_gate() -> None:
    """SC-001: a refinement improving fewer than 80% of pairs is rejected."""

    cases = _cases()
    refined = list(_observations(cases, refined=True))
    for index in range(7):
        truth_timestamp_ms = cases[index].truth_timestamp_ms
        assert truth_timestamp_ms is not None
        refined[index] = replace(refined[index], predicted_timestamp_ms=truth_timestamp_ms + 20_000)

    report = compare_refinement_policies(
        cases,
        base=_observations(cases, refined=False),
        refined=tuple(refined),
    )

    assert report.gates["SC-001"] is False
    assert report.accepted is False


def test_top5_loss_above_five_percentage_points_fails_adoption_gate() -> None:
    """SC-002: losing more than 5 pp of Top-5 rejects adoption."""

    cases = _cases()
    refined = list(_observations(cases, refined=True))
    for index in range(2):
        expected_video_id = cases[index].expected_video_id
        assert expected_video_id is not None
        refined[index] = replace(
            refined[index],
            ranked_video_ids=(
                f"other-{index}-1",
                f"other-{index}-2",
                f"other-{index}-3",
                f"other-{index}-4",
                f"other-{index}-5",
                expected_video_id,
            ),
        )

    report = compare_refinement_policies(
        cases,
        base=_observations(cases, refined=False),
        refined=tuple(refined),
    )

    assert report.top5_loss_percentage_points is not None
    assert report.top5_loss_percentage_points > 5.0
    assert report.gates["SC-002"] is False
    assert report.accepted is False


def test_manifest_rejects_non_numeric_observation_counters(tmp_path: Path) -> None:
    """Corrupt counters cannot be normalised to zero and accepted silently."""

    manifest_path = tmp_path / "invalid-counter.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "case-1",
                        "expected_video_id": "video-1",
                        "source": "local",
                        "duration_ms": 240_000,
                        "truth_timestamp_ms": 120_000,
                    }
                ],
                "base": [
                    {
                        "case_id": "case-1",
                        "ranked_video_ids": ["video-1"],
                        "assets_evaluated": "not-a-number",
                    }
                ],
                "refined": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkError, match="contador inválido"):
        load_temporal_benchmark_manifest(manifest_path)


def test_paired_report_is_reproducible_and_serialization_excludes_query_media() -> None:
    """NFR-003/SEC-005/SC-007: deterministic safe report, no query bytes/media."""

    cases = _cases()
    base = _observations(cases, refined=False)
    refined = _observations(cases, refined=True)
    first = compare_refinement_policies(cases, base=base, refined=refined)
    second = compare_refinement_policies(cases, base=base, refined=refined)

    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    serialized = json.dumps(payload, sort_keys=True)
    assert "query_bytes" not in serialized
    assert "query_image" not in serialized
    assert "video_bytes" not in serialized
    assert "secret" not in serialized.lower()
    assert payload["coverage"]["valid"] is True


def test_coverage_shortfall_fails_closed_and_never_marks_benchmark_apt() -> None:
    """SC-003/SC-007: 29 positives cannot authorize a benchmark run."""

    cases = _cases()[:-1]
    base = _observations(cases, refined=False)
    refined = _observations(cases, refined=True)

    coverage = validate_temporal_benchmark_coverage(cases)
    report = compare_refinement_policies(cases, base=base, refined=refined)

    assert coverage.valid is False
    assert coverage.positive_case_count == 29
    assert "minimum_positive_cases:29<30" in coverage.errors
    assert report.accepted is False
    assert report.gates["SC-003"] is False
    assert report.gates["SC-001"] is False
    assert report.gates["SC-007"] is False


def test_missing_refined_pair_fails_closed_even_when_coverage_is_valid() -> None:
    """FR-014/SC-007: metrics from an incomplete pair are not adoptable."""

    cases = _cases()
    report = compare_refinement_policies(
        cases,
        base=_observations(cases, refined=False),
        refined=_observations(cases[:-1], refined=True),
    )

    assert report.coverage.valid is True
    assert report.accepted is False
    assert report.missing_refined_case_ids == ("positive-029",)
    assert report.gates["SC-001"] is False
    assert report.gates["SC-002"] is False
    assert report.gates["SC-007"] is False


def test_invalid_independent_truth_and_duplicate_case_id_fail_coverage() -> None:
    """SC-003/SC-005: invalid truth or duplicate identities block adoption."""

    cases = list(_cases())
    cases[0] = TemporalBenchmarkCase(
        case_id=cases[1].case_id,
        expected_video_id=cases[0].expected_video_id,
        source=cases[0].source,
        duration_ms=cases[0].duration_ms,
        truth_timestamp_ms=cases[0].duration_ms,
    )

    coverage = validate_temporal_benchmark_coverage(tuple(cases))
    report = compare_refinement_policies(
        tuple(cases),
        base=_observations(tuple(cases), refined=False),
        refined=_observations(tuple(cases), refined=True),
    )

    assert coverage.valid is False
    assert any(error.startswith("duplicate_case_ids:") for error in coverage.errors)
    assert any(error.startswith("invalid_truth:") for error in coverage.errors)
    assert report.accepted is False
    assert report.gates["SC-003"] is False
