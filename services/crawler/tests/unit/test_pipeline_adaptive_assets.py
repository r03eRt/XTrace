"""Regresiones de la frontera de assets adaptativos del crawler."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable

import httpx
from PIL import Image
from xtrace_spike.sampling import AdaptiveSamplingPolicy

from xtrace_crawler.adapters.models import VideoSource, VisualAsset
from xtrace_crawler.adapters.xvideos import XvideosAdapter
from xtrace_crawler.jobs.repo import JobsRepo
from xtrace_crawler.pipeline import CrawlerPipeline, IndexedFrame
from xtrace_crawler.repo import CrawlerRepo


class _AdaptiveAssetAdapter:
    """Double mínimo para comprobar el hook opcional de REINDEX."""

    manifest = XvideosAdapter.manifest
    asset_hosts: list[str] = []

    def __init__(self) -> None:
        self.legacy_calls = 0
        self.adaptive_calls: list[AdaptiveSamplingPolicy] = []

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        self.legacy_calls += 1
        return [VisualAsset(kind="thumbnail", url="https://example.invalid/legacy.jpg")]

    async def get_visual_assets_for_sampling(
        self, video: VideoSource, *, policy: AdaptiveSamplingPolicy
    ) -> list[VisualAsset]:
        self.adaptive_calls.append(policy)
        return [VisualAsset(kind="thumbnail", url="https://example.invalid/adaptive.jpg")]


class _Adaptive404Adapter(_AdaptiveAssetAdapter):
    """Adapter double where a generated public variant returns HTTP 404."""

    async def fetch_asset_bytes(self, url: str) -> bytes | None:
        if url.endswith("/xv_3_t.jpg"):
            buffer = io.BytesIO()
            Image.new("RGB", (8, 8), color=(20, 40, 60)).save(buffer, format="JPEG")
            return buffer.getvalue()
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        response.raise_for_status()
        return None


def _pipeline(adapter: object) -> CrawlerPipeline:
    return CrawlerPipeline(
        repo=CrawlerRepo(),
        jobs=JobsRepo(),
        adapter_for=lambda _job: adapter,  # type: ignore[arg-type]
    )


def _video() -> VideoSource:
    return VideoSource(
        source="xvideos",
        external_id="video.synth00022",
        page_url="https://www.xvideos.com/video.synth00022/example",
        duration_ms=758_000,
    )


def _run(coro: Callable[[], object]) -> object:
    return asyncio.run(coro())


def test_reindex_usa_hook_adaptativo_sin_cambiar_get_visual_assets_legacy() -> None:
    """FR-009/FR-014: REINDEX puede ampliar assets sin cambiar INDEX_VIDEO legacy."""
    adapter = _AdaptiveAssetAdapter()
    pipeline = _pipeline(adapter)
    policy = AdaptiveSamplingPolicy()

    async def scenario() -> list[VisualAsset]:
        return await pipeline._get_visual_assets_for_reindex(adapter, _video(), policy)

    assets = _run(scenario)
    assert isinstance(assets, list)
    assert [asset.url for asset in assets] == ["https://example.invalid/adaptive.jpg"]
    assert adapter.legacy_calls == 0
    assert adapter.adaptive_calls == [policy]


def test_variante_generada_404_degrada_y_conserva_asset_declarado() -> None:
    """FR-005/FR-007: un 404 de una variante no borra la evidencia declarada."""
    adapter = _Adaptive404Adapter()
    pipeline = _pipeline(adapter)
    video = _video()
    assets = [
        VisualAsset(
            kind="thumbnail",
            url="https://thumb-cdn77.xvideos.invalid/path/xv_3_t.jpg",
            position=3,
            timestamp_ms=300_000,
        ),
        VisualAsset(
            kind="thumbnail",
            url="https://thumb-cdn77.xvideos.invalid/path/xv_4_t.jpg",
            position=4,
            timestamp_ms=400_000,
        ),
    ]

    async def scenario() -> list[IndexedFrame]:
        return await pipeline._collect_frames(
            assets,
            video,
            adapter,  # type: ignore[arg-type]
            sampling_policy=AdaptiveSamplingPolicy(),
            use_default_sampling=False,
        )

    frames = _run(scenario)
    assert len(frames) == 1
    assert frames[0].timestamp_ms == 300_000
    pipeline._close_frames(frames)
