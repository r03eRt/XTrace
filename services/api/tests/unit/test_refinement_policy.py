"""Tests first for the bounded temporal-refinement policy (TASK-006-T004)."""

from __future__ import annotations

import pytest

from xtrace_api.refinement.policy import RefinementPolicy


def test_defaults_match_approved_contract() -> None:
    policy = RefinementPolicy()

    assert policy.enabled is True
    assert policy.candidate_limit == 3
    assert policy.max_assets_per_candidate == 30
    assert policy.search_timeout_ms == 10_000
    assert policy.candidate_timeout_ms == 3_000
    assert policy.max_asset_bytes == 10 * 1024 * 1024
    assert policy.policy_version == "temporal-refinement-v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_limit", 0),
        ("candidate_limit", 6),
        ("max_assets_per_candidate", 0),
        ("max_assets_per_candidate", 31),
        ("search_timeout_ms", 0),
        ("search_timeout_ms", 10_001),
        ("candidate_timeout_ms", 0),
        ("candidate_timeout_ms", 3_001),
        ("max_asset_bytes", 0),
    ],
)
def test_hard_limits_fail_closed(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        RefinementPolicy(**{field: value})


def test_candidate_timeout_cannot_exceed_search_timeout() -> None:
    with pytest.raises(ValueError):
        RefinementPolicy(search_timeout_ms=2_000, candidate_timeout_ms=2_001)


def test_environment_and_source_overrides_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XTRACE_REFINEMENT_ENABLED", "false")
    monkeypatch.setenv("XTRACE_REFINEMENT_CANDIDATE_LIMIT", "5")
    monkeypatch.setenv("XTRACE_REFINEMENT_MAX_ASSETS_PER_CANDIDATE", "12")
    monkeypatch.setenv("XTRACE_REFINEMENT_SEARCH_TIMEOUT_MS", "9000")
    monkeypatch.setenv("XTRACE_REFINEMENT_CANDIDATE_TIMEOUT_MS", "2500")
    monkeypatch.setenv(
        "XTRACE_REFINEMENT_SOURCE_OVERRIDES",
        '{"xvideos":{"enabled":true,"max_assets_per_candidate":8}}',
    )

    policy = RefinementPolicy.from_env()
    source_policy = policy.for_source("xvideos")

    assert policy.enabled is False
    assert policy.candidate_limit == 5
    assert policy.max_assets_per_candidate == 12
    assert policy.search_timeout_ms == 9_000
    assert policy.candidate_timeout_ms == 2_500
    assert source_policy.enabled is True
    assert source_policy.max_assets_per_candidate == 8
    assert policy.for_source("unknown") == policy


@pytest.mark.parametrize(
    "value",
    ["maybe", "", "2", "-1"],
)
def test_invalid_enabled_environment_fails_closed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("XTRACE_REFINEMENT_ENABLED", value)
    with pytest.raises(ValueError):
        RefinementPolicy.from_env()


def test_source_override_cannot_bypass_absolute_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "XTRACE_REFINEMENT_SOURCE_OVERRIDES",
        '{"xvideos":{"candidate_limit":6,"max_assets_per_candidate":31}}',
    )
    with pytest.raises(ValueError):
        RefinementPolicy.from_env()


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("XTRACE_REFINEMENT_CANDIDATE_LIMIT", "6"),
        ("XTRACE_REFINEMENT_MAX_ASSETS_PER_CANDIDATE", "31"),
        ("XTRACE_REFINEMENT_SEARCH_TIMEOUT_MS", "10001"),
        ("XTRACE_REFINEMENT_CANDIDATE_TIMEOUT_MS", "3001"),
        ("XTRACE_REFINEMENT_MAX_ASSET_BYTES", "0"),
    ],
)
def test_environment_overrides_cannot_exceed_absolute_limits(
    monkeypatch: pytest.MonkeyPatch, env_name: str, value: str
) -> None:
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ValueError):
        RefinementPolicy.from_env()


@pytest.mark.parametrize(
    "override",
    [
        {"search_timeout_ms": 10_001},
        {"candidate_timeout_ms": 3_001},
        {"candidate_timeout_ms": 2_500, "search_timeout_ms": 2_000},
        {"max_asset_bytes": 0},
    ],
)
def test_source_timeout_and_budget_overrides_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, int]
) -> None:
    import json

    monkeypatch.setenv("XTRACE_REFINEMENT_SOURCE_OVERRIDES", json.dumps({"xvideos": override}))
    with pytest.raises(ValueError):
        RefinementPolicy.from_env()


def test_source_overrides_are_deeply_immutable() -> None:
    policy = RefinementPolicy(source_overrides={"xvideos": {"enabled": False}})

    with pytest.raises(TypeError):
        policy.source_overrides["xvideos"]["enabled"] = True  # type: ignore[index]
