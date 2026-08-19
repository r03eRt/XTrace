"""Tests first for mapping indexed metadata to refinement candidates."""

from __future__ import annotations

from xtrace_api.refinement.catalog import (
    RefinementCatalog,
    candidate_from_record,
    is_refinable_candidate,
)


def test_catalog_builds_web_candidate_without_parsing_html() -> None:
    candidate = candidate_from_record(
        {
            "video_id": "00000000-0000-0000-0000-000000000010",
            "source": "mock",
            "adapter": "mock",
            "external_id": "mock-0000",
            "page_url": "https://mock.example/videos/mock-0000",
            "duration_ms": 120_000,
            "base_timestamp_ms": 12_000,
            "base_visual_similarity": 0.88,
        }
    )

    assert candidate.video_id.endswith("0010")
    assert candidate.source == "mock"
    assert candidate.external_id == "mock-0000"
    assert candidate.duration_ms == 120_000
    assert is_refinable_candidate(candidate) is True


def test_catalog_marks_local_and_incomplete_candidates_unavailable() -> None:
    local = candidate_from_record(
        {
            "video_id": "00000000-0000-0000-0000-000000000011",
            "source": None,
            "adapter": None,
            "external_id": None,
            "page_url": None,
            "duration_ms": None,
            "base_timestamp_ms": 1_000,
            "base_visual_similarity": 0.8,
        }
    )
    incomplete = candidate_from_record(
        {
            "video_id": "00000000-0000-0000-0000-000000000012",
            "source": "mock",
            "adapter": None,
            "external_id": "mock-1",
            "page_url": None,
            "duration_ms": None,
            "base_timestamp_ms": None,
            "base_visual_similarity": 0.7,
        }
    )
    nullable_web = candidate_from_record(
        {
            "video_id": "00000000-0000-0000-0000-000000000013",
            "source": "mock",
            "adapter": "mock",
            "external_id": "mock-2",
            "page_url": None,
            "duration_ms": None,
            "base_timestamp_ms": None,
            "base_visual_similarity": 0.7,
        }
    )

    assert is_refinable_candidate(local) is False
    assert is_refinable_candidate(incomplete) is False
    assert is_refinable_candidate(nullable_web) is True
    assert RefinementCatalog.is_refinable(local) is False
