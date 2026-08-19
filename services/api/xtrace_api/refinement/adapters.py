"""Adapter bridge for refinement; the crawler remains the compliance boundary."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlsplit

from xtrace_crawler.adapters.base import SourceAdapter  # type: ignore[import-untyped]
from xtrace_crawler.adapters.models import (  # type: ignore[import-untyped]
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.adapters.registry import (  # type: ignore[import-untyped]
    AdapterNotEnabledError,
    AdapterRegistry,
)
from xtrace_crawler.crawling.http import (  # type: ignore[import-untyped]
    HostNotAllowedError,
    NoAssetHostsError,
    SafeHTTPClient,
)
from xtrace_crawler.crawling.ratelimit import RateLimiter  # type: ignore[import-untyped]


class RefinementAdapterBridge:
    """Resolve only adapters already registered and approved by the crawler."""

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry
        self._limiters: dict[str, RateLimiter] = {}
        # The bridge is the capability boundary for the refinement path.  A
        # caller must first obtain an adapter through ``resolve`` (which
        # applies the registry/DB gate) before it can invoke any adapter method
        # or in-process byte provider.  Tracking identity rather than source
        # name prevents a raw, unregistered instance from bypassing the gate.
        self._approved_adapter_ids: set[int] = set()

    def resolve(self, source: str, *, enabled_in_db: bool) -> SourceAdapter:
        """Apply registry compliance and database enablement gates."""

        if not source.strip():
            raise ValueError("source no puede estar vacío")
        adapter = self._registry.get_enabled(source.strip().lower(), enabled_in_db=enabled_in_db)
        self._approved_adapter_ids.add(id(adapter))
        return adapter

    def resolve_optional(self, source: str | None, *, enabled_in_db: bool) -> SourceAdapter | None:
        """Return no adapter for local candidates instead of attempting network."""

        if source is None:
            return None
        return self.resolve(source, enabled_in_db=enabled_in_db)

    async def get_video(
        self,
        source: str,
        external_id: str,
        *,
        page_url: str | None,
        enabled_in_db: bool,
    ) -> VideoSource | None:
        """Fetch normalized metadata through the approved adapter only."""

        adapter = self.resolve(source, enabled_in_db=enabled_in_db)
        await self._acquire(adapter)
        return await adapter.get_video(external_id, page_url=page_url)

    async def get_visual_assets(
        self, adapter: SourceAdapter, video: VideoSource
    ) -> list[VisualAsset]:
        """Return only assets that have an in-process provider or allowlisted host."""

        self._require_approved(adapter)
        await self._acquire(adapter)
        assets = await adapter.get_visual_assets(video)
        has_in_process = callable(getattr(adapter, "fetch_asset_bytes", None))
        allowed_hosts = _asset_hosts(adapter)
        if has_in_process:
            return list(assets)
        if not allowed_hosts:
            raise NoAssetHostsError(
                f"adapter {video.source!r} no declara asset_hosts para descarga HTTP"
            )
        return [asset for asset in assets if _host_for_asset(asset) in allowed_hosts]

    async def fetch_asset_bytes(
        self,
        adapter: SourceAdapter,
        asset: VisualAsset,
        *,
        max_bytes: int,
    ) -> bytes:
        """Use adapter in-process bytes, otherwise the crawler SafeHTTPClient."""

        self._require_approved(adapter)
        in_process = getattr(adapter, "fetch_asset_bytes", None)
        if callable(in_process):
            data = await in_process(asset.url)
            if data is not None:
                return cast(bytes, data)

        await self._acquire(adapter)
        allowed_hosts = _asset_hosts(adapter)
        host = _host_for_asset(asset)
        if not allowed_hosts:
            raise NoAssetHostsError("no hay allowlist de hosts para el asset")
        if host not in allowed_hosts:
            raise HostNotAllowedError(f"host de asset fuera de allowlist: {host!r}")
        async with SafeHTTPClient(
            allowed_hosts=allowed_hosts,
            validate_resolved_ip=True,
        ) as client:
            return cast(bytes, await client.get_bytes(asset.url, max_bytes=max_bytes))

    async def _acquire(self, adapter: SourceAdapter) -> None:
        """Respect the adapter manifest rate limit before a source request.

        Synthetic adapters that provide in-process bytes and declare no HTTP
        asset hosts do not incur a network wait. Real adapters always use their
        manifest's conservative limiter; the orchestrator's timeout remains
        the upper bound for a candidate.
        """

        if callable(getattr(adapter, "fetch_asset_bytes", None)) and not _asset_hosts(adapter):
            return
        name = adapter.manifest.source.strip().lower()
        limiter = self._limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(adapter.manifest.rate_limit, source=name, jitter_factor=0.0)
            self._limiters[name] = limiter
        await limiter.acquire()

    async def aclose(self) -> None:
        """Close adapter clients owned by this bridge, if they expose one."""

        for name in self._registry.names():
            adapter = self._registry.get(name)
            close = getattr(adapter, "aclose", None)
            if not callable(close):
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                # Cleanup must not turn an already valid base result into a
                # request error, and the adapters never log response payloads.
                continue

    def _require_approved(self, adapter: SourceAdapter) -> None:
        """Reject a raw adapter instance that did not pass ``resolve``.

        Keeping this check on both asset paths matters for adapters exposing an
        in-process byte method: without it a caller could skip the registry's
        ``sources.enabled`` and manifest checks while never opening a socket.
        """

        if id(adapter) not in self._approved_adapter_ids:
            source = getattr(getattr(adapter, "manifest", None), "source", "unknown")
            raise AdapterNotEnabledError(
                str(source), ["adapter no resuelto por el gate de RefinementAdapterBridge"]
            )


def _asset_hosts(adapter: SourceAdapter) -> set[str]:
    raw_hosts = getattr(adapter, "asset_hosts", None)
    if raw_hosts is None:
        return set()
    return {str(host).lower() for host in raw_hosts if str(host).strip()}


def _host_for_asset(asset: VisualAsset) -> str:
    parsed = urlsplit(asset.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HostNotAllowedError(f"URL de asset inválida: {asset.url!r}")
    return cast(str, parsed.hostname).lower()
