"""Descarga de visual assets permitidos (PR-029 · FR-005 · FR-015 · SC-006 · contracts §7).

Solo storyboard/thumbnail/preview: `AssetKind` no tiene un tipo "video", de
modo que la superficie de descarga excluye el vídeo completo **por
construcción** (SC-006: 0 descargas de vídeo completo). Cada descarga vive en
un directorio temporal dedicado (`tempfile.mkdtemp(prefix="xtrace-crawler-asset-")`)
que se elimina en `finally` tanto si el llamador termina con éxito como si
lanza (FR-015: sin artefactos temporales incluso cuando el job falla).

El límite `max_bytes` (default 10 MiB por asset) aborta descargas
desmesuradas; se aplica a través de `SafeHTTPClient.get_bytes` (PR-024), que
además exige status 2xx y valida host/esquema en cada petición (SEC-001).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from xtrace_crawler.adapters.models import AssetKind, VisualAsset
from xtrace_crawler.crawling.http import SafeHTTPClient

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB por asset (FR-005: descargas acotadas)

_SUFFIX_BY_KIND: dict[AssetKind, str] = {
    "storyboard": ".jpg",
    "thumbnail": ".jpg",
    "preview": ".mp4",
}


class AssetFetcher:
    """Descarga de assets permitidos con cleanup `try/finally` garantizado (FR-015).

    Uso:

        async with SafeHTTPClient(allowed_hosts={...}) as client:
            fetcher = AssetFetcher(client)
            async with fetcher.fetch(asset) as path:
                ...  # usar `path`; al salir del contexto el temporal se elimina
    """

    def __init__(
        self, client: SafeHTTPClient, *, default_max_bytes: int = DEFAULT_MAX_BYTES
    ) -> None:
        """Crea el fetcher sobre un `SafeHTTPClient` ya construido (PR-024).

        Args:
            client: cliente HTTP seguro del adapter (única vía de red, SEC-001).
            default_max_bytes: límite aplicado cuando `fetch()` no recibe uno.
        """
        if default_max_bytes <= 0:
            raise ValueError(f"default_max_bytes debe ser > 0, got {default_max_bytes}")
        self._client = client
        self._default_max_bytes = default_max_bytes

    @asynccontextmanager
    async def fetch(
        self, asset: VisualAsset, *, max_bytes: int | None = None
    ) -> AsyncIterator[Path]:
        """Descarga `asset` a un archivo temporal y garantiza su eliminación (FR-015).

        El límite efectivo es `max_bytes` o el default del fetcher. Los errores
        de descarga (HTTP, límite superado, host no permitido) se propagan con
        su tipo (p. ej. `httpx.HTTPStatusError`, `DownloadTooLargeError`) sin
        dejar artefactos temporales (errores contenidos, contracts §7).

        Args:
            asset: asset permitido (storyboard/thumbnail/preview) a descargar.
            max_bytes: límite por descarga; `None` usa el default del fetcher.
        """
        limit = self._default_max_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            raise ValueError(f"max_bytes debe ser > 0, got {limit}")
        tmp_dir = Path(tempfile.mkdtemp(prefix="xtrace-crawler-asset-"))
        try:
            target = tmp_dir / f"asset{_SUFFIX_BY_KIND[asset.kind]}"
            data = await self._client.get_bytes(asset.url, max_bytes=limit)
            target.write_bytes(data)
            yield target
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
