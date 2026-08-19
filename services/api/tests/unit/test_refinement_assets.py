"""Tests first for bounded, in-memory visual-asset materialization (TASK-006-T009)."""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from xtrace_crawler.adapters.models import VisualAsset

from xtrace_api.refinement.assets import AssetMaterializer


class TrackingImage:
    """Small Pillow proxy that exposes deterministic close state in tests."""

    def __init__(self, image: Image.Image) -> None:
        self._image = image
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._image.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._image, name)


def _png_bytes(size: tuple[int, int] = (8, 8), colour: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format="PNG")
    return output.getvalue()


def _asset(kind: str, url: str, *, timestamp_ms: int | None = 1000) -> VisualAsset:
    return VisualAsset(kind=kind, url=url, timestamp_ms=timestamp_ms, position=1)


def test_materializer_accepts_only_public_visual_kinds_and_deduplicates() -> None:
    fetched: list[str] = []

    async def fetch(asset: VisualAsset) -> bytes:
        fetched.append(asset.url)
        return _png_bytes()

    assets = [
        _asset("thumbnail", "https://cdn.example/a.jpg"),
        _asset("thumbnail", "https://cdn.example/a.jpg"),
        _asset("storyboard", "https://cdn.example/b.jpg", timestamp_ms=2000),
        _asset("preview", "https://cdn.example/preview.mp4"),
    ]
    result = asyncio.run(
        AssetMaterializer(fetch).materialize(assets, max_assets=30, max_bytes=1024 * 1024)
    )

    assert len(result.assets) == 2
    assert result.discarded_count == 2
    assert fetched == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
    assert all(item.asset.kind in {"thumbnail", "storyboard"} for item in result.assets)
    assert all(item.asset.url != "https://cdn.example/preview.mp4" for item in result.assets)


def test_materializer_discards_corrupt_bytes_and_pixel_bombs_without_leaking_resources() -> None:
    payloads = {
        "https://cdn.example/corrupt.jpg": b"not-an-image",
        "https://cdn.example/large.jpg": _png_bytes((100, 100)),
    }

    async def fetch(asset: VisualAsset) -> bytes:
        return payloads[asset.url]

    result = asyncio.run(
        AssetMaterializer(fetch, max_pixels=100).materialize(
            [
                _asset("thumbnail", "https://cdn.example/corrupt.jpg"),
                _asset("thumbnail", "https://cdn.example/large.jpg"),
            ],
            max_assets=30,
            max_bytes=1024 * 1024,
        )
    )

    assert result.assets == ()
    assert result.discarded_count == 2
    assert result.bytes_downloaded == len(payloads["https://cdn.example/corrupt.jpg"]) + len(
        payloads["https://cdn.example/large.jpg"]
    )


def test_materializer_enforces_count_and_byte_budget_before_fetching_more() -> None:
    fetched: list[str] = []

    async def fetch(asset: VisualAsset) -> bytes:
        fetched.append(asset.url)
        return _png_bytes()

    assets = [
        _asset("thumbnail", "https://cdn.example/1.jpg"),
        _asset("thumbnail", "https://cdn.example/2.jpg"),
        _asset("thumbnail", "https://cdn.example/3.jpg"),
    ]
    result = asyncio.run(AssetMaterializer(fetch).materialize(assets, max_assets=2, max_bytes=1))

    assert result.assets == ()
    assert result.discarded_count == 3
    assert fetched == ["https://cdn.example/1.jpg"]
    assert result.bytes_downloaded == len(_png_bytes())


def test_materializer_closes_accepted_images_when_a_later_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted_image = TrackingImage(Image.new("RGB", (8, 8), (255, 0, 0)))
    calls = 0

    async def fetch(_asset: VisualAsset) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return b"first"
        raise RuntimeError("source unavailable")

    def fake_decode(_data: bytes, *, max_pixels: int) -> Image.Image:
        assert max_pixels > 0
        return accepted_image

    monkeypatch.setattr("xtrace_api.refinement.assets._decode_image", fake_decode)
    with pytest.raises(RuntimeError):
        asyncio.run(
            AssetMaterializer(fetch).materialize(
                [
                    _asset("thumbnail", "https://cdn.example/first.jpg"),
                    _asset("thumbnail", "https://cdn.example/second.jpg"),
                ],
                max_assets=30,
                max_bytes=1024,
            )
        )
    assert accepted_image.closed


def test_materializer_discards_pillow_decompression_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(_asset: VisualAsset) -> bytes:
        return b"bomb"

    def bomb_decoder(_data: bytes, *, max_pixels: int) -> Image.Image:
        del max_pixels
        raise Image.DecompressionBombError("declared dimensions are unsafe")

    monkeypatch.setattr("xtrace_api.refinement.assets._decode_image", bomb_decoder)
    result = asyncio.run(
        AssetMaterializer(fetch).materialize(
            [_asset("thumbnail", "https://cdn.example/bomb.png")],
            max_assets=30,
            max_bytes=1024,
        )
    )

    assert result.assets == ()
    assert result.discarded_count == 1


def test_materialization_result_exposes_explicit_success_cleanup() -> None:
    accepted_image = TrackingImage(Image.new("RGB", (8, 8), (255, 0, 0)))

    async def fetch(_asset: VisualAsset) -> bytes:
        return b"first"

    def fake_decode(_data: bytes, *, max_pixels: int) -> Image.Image:
        del max_pixels
        return accepted_image  # type: ignore[return-value]

    import xtrace_api.refinement.assets as assets_module

    original = assets_module._decode_image
    assets_module._decode_image = fake_decode
    try:
        result = asyncio.run(
            AssetMaterializer(fetch).materialize(
                [_asset("thumbnail", "https://cdn.example/first.jpg")],
                max_assets=30,
                max_bytes=1024,
            )
        )
    finally:
        assets_module._decode_image = original

    assert not accepted_image.closed
    result.close()
    assert accepted_image.closed
