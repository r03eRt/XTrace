"""Crawler-side no-video contract for temporal refinement (TASK-006-T016)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from xtrace_crawler.adapters.xvideos import XvideosAdapter, parse_video_page


def test_xvideos_video_source_never_exposes_full_video_asset() -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "xvideos" / "video_page_full.html"
    video = parse_video_page(
        fixture.read_text(),
        page_url="https://www.xvideos.invalid/video.synth00001/example",
    )

    assert video.preview_url is None
    assert video.storyboard_urls == []


def test_xvideos_refinement_assets_are_thumbnail_only_and_never_mp4() -> None:
    fixture = Path(__file__).parent.parent / "fixtures" / "xvideos" / "video_page_full.html"
    video = parse_video_page(
        fixture.read_text(),
        page_url="https://www.xvideos.invalid/video.synth00001/example",
    )

    html = fixture.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, request=request)

    assets = asyncio.run(
        XvideosAdapter(transport=httpx.MockTransport(handler)).get_visual_assets(video)
    )

    assert assets
    assert all(asset.kind == "thumbnail" for asset in assets)
    assert all(not urlsplit(asset.url).path.lower().endswith(".mp4") for asset in assets)
