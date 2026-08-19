"""Tests first for the adapter gate used by temporal refinement (TASK-006-T010)."""

from __future__ import annotations

import asyncio

import pytest
from xtrace_crawler.adapters.base import (  # type: ignore[import-untyped]
    AdapterManifest,
    RateLimitSpec,
)
from xtrace_crawler.adapters.mock import MockAdapter  # type: ignore[import-untyped]
from xtrace_crawler.adapters.registry import (  # type: ignore[import-untyped]
    AdapterNotEnabledError,
    AdapterRegistry,
    UnknownAdapterError,
)  # type: ignore[import-untyped]

from xtrace_api.refinement.adapters import RefinementAdapterBridge


class ApprovedMockAdapter(MockAdapter):
    def __init__(self) -> None:
        super().__init__(seed=7, catalog_size=1)
        self.manifest = AdapterManifest(
            source="approved",
            access_method="json",
            assets_accessed=["thumbnail"],
            robots_reviewed=True,
            terms_reviewed=True,
            review_date="2026-08-19",
            rate_limit=RateLimitSpec(min_interval_ms=100, max_rps=10.0),
        )


def test_bridge_resolves_enabled_mock_without_network() -> None:
    registry = AdapterRegistry()
    adapter = MockAdapter(seed=7, catalog_size=1)
    registry.register(adapter, real=False)
    bridge = RefinementAdapterBridge(registry)

    assert bridge.resolve("mock", enabled_in_db=False) is adapter


def test_bridge_fails_closed_for_unknown_or_disabled_real_adapter() -> None:
    registry = AdapterRegistry()
    adapter = MockAdapter(seed=7, catalog_size=1)
    registry.register(adapter, real=True)
    bridge = RefinementAdapterBridge(registry)

    with pytest.raises(AdapterNotEnabledError):
        bridge.resolve("mock", enabled_in_db=False)
    with pytest.raises(AdapterNotEnabledError):
        bridge.resolve("mock", enabled_in_db=True)
    with pytest.raises(UnknownAdapterError):
        bridge.resolve("unknown", enabled_in_db=True)


def test_bridge_resolves_real_adapter_only_after_manifest_and_db_gate() -> None:
    registry = AdapterRegistry()
    adapter = ApprovedMockAdapter()
    registry.register(adapter, real=True)
    bridge = RefinementAdapterBridge(registry)

    assert bridge.resolve("approved", enabled_in_db=True) is adapter


def test_bridge_rejects_approved_real_adapter_when_db_gate_is_off() -> None:
    registry = AdapterRegistry()
    registry.register(ApprovedMockAdapter(), real=True)
    bridge = RefinementAdapterBridge(registry)

    with pytest.raises(AdapterNotEnabledError):
        bridge.resolve("approved", enabled_in_db=False)


def test_bridge_get_video_preserves_normalized_video_source() -> None:
    registry = AdapterRegistry()
    adapter = ApprovedMockAdapter()
    registry.register(adapter, real=True)
    bridge = RefinementAdapterBridge(registry)
    external_id = adapter.catalog_ids()[0]

    video = asyncio.run(
        bridge.get_video(
            "approved",
            external_id,
            page_url=None,
            enabled_in_db=True,
        )
    )

    assert video is not None
    assert video.external_id == external_id
    assert video.source == "mock"
    assert video.page_url.startswith("http")


def test_bridge_does_not_turn_local_candidate_into_network_source() -> None:
    registry = AdapterRegistry()
    bridge = RefinementAdapterBridge(registry)

    assert bridge.resolve_optional(None, enabled_in_db=False) is None
