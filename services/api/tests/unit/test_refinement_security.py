"""Fail-closed security boundaries for temporal refinement (TASK-006-T016)."""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
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
from xtrace_crawler.adapters.registry import (  # type: ignore[import-untyped]
    AdapterNotEnabledError,
    AdapterRegistry,
)
from xtrace_crawler.crawling.http import HostNotAllowedError  # type: ignore[import-untyped]

from xtrace_api.refinement.adapters import RefinementAdapterBridge
from xtrace_api.refinement.assets import AssetMaterializer


class ApprovedMockAdapter(MockAdapter):
    def __init__(self) -> None:
        super().__init__(seed=11, catalog_size=1)
        self.manifest = AdapterManifest(
            source="approved-security",
            access_method="json",
            assets_accessed=["thumbnail"],
            robots_reviewed=True,
            terms_reviewed=True,
            review_date="2026-08-19",
            rate_limit=RateLimitSpec(min_interval_ms=100, max_rps=10.0),
        )


class HttpOnlyAdapter(ApprovedMockAdapter):
    asset_hosts = ["allowed.example"]

    def __init__(self) -> None:
        super().__init__()
        # Force the bridge's SafeHTTPClient path; no socket is ever opened by
        # the test because the foreign host is rejected before construction.
        self.fetch_asset_bytes = None  # type: ignore[assignment]


def test_manifest_and_sources_gate_fail_closed() -> None:
    registry = AdapterRegistry()
    registry.register(ApprovedMockAdapter(), real=True)
    bridge = RefinementAdapterBridge(registry)

    with pytest.raises(AdapterNotEnabledError):
        bridge.resolve("approved-security", enabled_in_db=False)


def test_manifest_incomplete_real_adapter_is_rejected_even_if_db_enabled() -> None:
    registry = AdapterRegistry()
    registry.register(MockAdapter(seed=1, catalog_size=1), real=True)
    bridge = RefinementAdapterBridge(registry)

    with pytest.raises(AdapterNotEnabledError):
        bridge.resolve("mock", enabled_in_db=True)


def test_asset_host_outside_allowlist_is_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = HttpOnlyAdapter()
    registry = AdapterRegistry()
    registry.register(adapter, real=True)
    bridge = RefinementAdapterBridge(registry)
    approved = bridge.resolve("approved-security", enabled_in_db=True)
    asset = VisualAsset(
        kind="thumbnail",
        url="https://evil.example/image.jpg",
        position=1,
        timestamp_ms=1_000,
    )

    class ExplodingHTTPClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("no se debe construir el cliente para un host rechazado")

    # The host check must happen before SafeHTTPClient construction, not merely
    # after a failed socket request.
    monkeypatch.setattr("xtrace_api.refinement.adapters.SafeHTTPClient", ExplodingHTTPClient)
    with pytest.raises(HostNotAllowedError):
        asyncio.run(bridge.fetch_asset_bytes(approved, asset, max_bytes=1024))


def test_raw_adapter_cannot_bypass_registry_gate() -> None:
    adapter = ApprovedMockAdapter()
    registry = AdapterRegistry()
    registry.register(adapter, real=True)
    bridge = RefinementAdapterBridge(registry)
    asset = VisualAsset(
        kind="thumbnail",
        url="https://cdn.example/image.jpg",
        position=1,
        timestamp_ms=1_000,
    )

    # Even an in-process byte provider must be reached only through resolve();
    # otherwise sources.enabled/manifest approval could be bypassed.
    with pytest.raises(AdapterNotEnabledError):
        asyncio.run(bridge.fetch_asset_bytes(adapter, asset, max_bytes=1024))

    video = adapter.get_catalog_video(adapter.catalog_ids()[0])
    assert isinstance(video, VideoSource)
    with pytest.raises(AdapterNotEnabledError):
        asyncio.run(bridge.get_visual_assets(adapter, video))


def test_preview_video_and_unknown_assets_are_never_fetched() -> None:
    fetched: list[str] = []

    async def fetch(asset: VisualAsset) -> bytes:
        fetched.append(asset.url)
        image = Image.new("RGB", (4, 4), (255, 0, 0))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    preview = VisualAsset(
        kind="preview", url="https://cdn.example/video.mp4", position=1, timestamp_ms=None
    )
    video = VisualAsset.model_construct(
        kind="video", url="https://cdn.example/video.mp4", position=1, timestamp_ms=None
    )
    result = asyncio.run(
        AssetMaterializer(fetch).materialize(
            [preview, video],
            max_assets=30,
            max_bytes=1024,
        )
    )

    assert result.assets == ()
    assert result.discarded_count == 2
    assert fetched == []
