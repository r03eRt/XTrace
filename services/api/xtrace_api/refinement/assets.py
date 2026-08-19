"""Safe, in-memory materialization of public visual assets."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError
from xtrace_crawler.adapters.models import VisualAsset  # type: ignore[import-untyped]

FetchAssetBytes = Callable[[VisualAsset], Awaitable[bytes]]


@dataclass(frozen=True)
class MaterializedAsset:
    """Decoded image owned by one refinement call; never persisted."""

    asset: VisualAsset
    image: Image.Image
    byte_count: int

    def close(self) -> None:
        """Release the decoded image once the evaluator is finished with it."""

        self.image.close()


@dataclass(frozen=True)
class MaterializationResult:
    """Successful assets plus bounded cost/discard counters."""

    assets: tuple[MaterializedAsset, ...]
    discarded_count: int
    bytes_downloaded: int

    def close(self) -> None:
        """Release every accepted image transferred to the caller."""

        for item in self.assets:
            item.close()

    def __enter__(self) -> MaterializationResult:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


class AssetMaterializer:
    """Decode allowlisted visual bytes without creating a persistent temp file."""

    def __init__(self, fetch_bytes: FetchAssetBytes, *, max_pixels: int = 25_000_000) -> None:
        if max_pixels <= 0:
            raise ValueError("max_pixels debe ser > 0")
        self._fetch_bytes = fetch_bytes
        self._max_pixels = max_pixels

    async def materialize(
        self,
        assets: Sequence[VisualAsset],
        *,
        max_assets: int,
        max_bytes: int,
    ) -> MaterializationResult:
        """Fetch and decode at most the configured asset/byte budget.

        Invalid kinds, duplicates, corrupt images and pixel bombs are discarded.
        Once the aggregate byte budget is exceeded no further asset is fetched.
        """

        if max_assets <= 0 or max_bytes <= 0:
            raise ValueError("max_assets y max_bytes deben ser > 0")

        accepted: list[MaterializedAsset] = []
        seen: set[tuple[str, int | None]] = set()
        discarded = 0
        bytes_downloaded = 0
        attempts = 0

        try:
            for index, asset in enumerate(assets):
                if attempts >= max_assets:
                    discarded += len(assets) - index
                    break
                if asset.kind not in ("thumbnail", "storyboard"):
                    discarded += 1
                    continue
                key = (asset.url, asset.timestamp_ms)
                if key in seen:
                    discarded += 1
                    continue
                seen.add(key)
                attempts += 1
                try:
                    data = await self._fetch_bytes(asset)
                    bytes_downloaded += len(data)
                    if bytes_downloaded > max_bytes:
                        discarded += len(assets) - index
                        break
                    image = _decode_image(data, max_pixels=self._max_pixels)
                except (
                    Image.DecompressionBombError,
                    OSError,
                    ValueError,
                    UnidentifiedImageError,
                ):
                    discarded += 1
                    continue
                accepted.append(MaterializedAsset(asset=asset, image=image, byte_count=len(data)))
        except BaseException:
            for item in accepted:
                item.close()
            raise

        return MaterializationResult(
            assets=tuple(accepted),
            discarded_count=discarded,
            bytes_downloaded=bytes_downloaded,
        )


def _decode_image(data: bytes, *, max_pixels: int) -> Image.Image:
    if not data:
        raise ValueError("asset vacío")
    with Image.open(BytesIO(data)) as opened:
        if opened.width * opened.height > max_pixels:
            raise ValueError("asset supera el límite de píxeles")
        opened.load()
        return opened.convert("RGB").copy()
