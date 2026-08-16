"""Cliente HTTP seguro del crawler (PR-024 · SEC-001 · plan §Security strategy).

Superficie SSRF contenida: el cliente solo habla con **https** (http solo con el
flag dev explícito `allow_http=True`) contra **hosts en allowlist por adapter**,
aborta cualquier redirect fuera del allowlist, declara un **User-Agent** propio,
permite **timeout configurable** y descarga bytes a buffer o archivo temporal.

Todas las peticiones pasan por un request-hook del `httpx.AsyncClient` que
revalida la URL en cada salto del redirect loop, de modo que ni la petición
inicial ni ningún redirect pueden escapar de la política (SEC-001). El cliente
es testeable con `httpx.MockTransport` (sin red real, NFR-003).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

import httpx

DEFAULT_USER_AGENT = "XTraceCrawler/0.1.0 (+https://github.com/r03eRt/XTrace; crawler spec 002)"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 10


class HostNotAllowedError(ValueError):
    """La URL apunta a un host que no está en la allowlist del adapter."""


class SchemeNotAllowedError(ValueError):
    """Esquema no permitido: solo https (http solo con `allow_http=True` en dev)."""


class DownloadTooLargeError(ValueError):
    """La descarga superó el límite `max_bytes`."""


class SafeHTTPClient:
    """Wrapper de `httpx.AsyncClient` con política de seguridad anti-SSRF.

    Uso recomendado como context manager async:

        async with SafeHTTPClient(allowed_hosts={"example.com"}) as client:
            body = await client.get_bytes("https://example.com/asset")
    """

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allow_http: bool = False,
        max_redirects: int = MAX_REDIRECTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Crea el cliente con la política de seguridad fijada.

        Args:
            allowed_hosts: hosts permitidos para este adapter (match exacto,
                case-insensitive; el puerto no forma parte del match).
            user_agent: User-Agent declarado en cada request.
            timeout_seconds: timeout total (connect/read/write/pool) en segundos.
            allow_http: flag **dev explícito**; `False` (default) fuerza https.
            max_redirects: máximo de redirects seguidos (todos validados).
            transport: transporte inyectable (`httpx.MockTransport` en tests).
        """
        self._allowed_hosts = frozenset(h.lower().removesuffix(".") for h in allowed_hosts)
        self._allow_http = allow_http

        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            max_redirects=max_redirects,
            headers={"User-Agent": user_agent},
            event_hooks={"request": [self._validate_request]},
        )

    # -- Política de seguridad -------------------------------------------------

    def _validate_url(self, url: str) -> None:
        """Valida la URL contra la política (esquema, userinfo, host allowlist)."""
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise SchemeNotAllowedError(
                f"esquema '{parsed.scheme}' no permitido (solo http/https): {url}"
            )
        if parsed.scheme == "http" and not self._allow_http:
            raise SchemeNotAllowedError(
                f"http solo permitido con allow_http=True (flag dev explícito): {url}"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"URL con userinfo no permitida (anti-SSRF): {url}")
        host = (parsed.hostname or "").lower().removesuffix(".")
        if not host:
            raise ValueError(f"URL sin host válido: {url}")
        if host not in self._allowed_hosts:
            raise HostNotAllowedError(f"host '{host}' no está en la allowlist del adapter: {url}")

    async def _validate_request(self, request: httpx.Request) -> None:
        """Request-hook: revalida cada petición, incluidos los redirects."""
        self._validate_url(str(request.url))

    # -- Operaciones -----------------------------------------------------------

    async def get(self, url: str) -> httpx.Response:
        """GET validado; devuelve la respuesta para que el adapter inspeccione
        estado/headers (p. ej. 404 → vídeo no disponible).
        """
        self._validate_url(url)
        return await self._client.get(url)

    async def get_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes:
        """Descarga la URL a un buffer en memoria (bytes), en streaming.

        Exige status 2xx (`httpx.HTTPStatusError` en caso contrario) y aborta
        con `DownloadTooLargeError` si el body supera `max_bytes`.
        """
        self._validate_url(url)
        chunks: list[bytes] = []
        total = 0
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise DownloadTooLargeError(
                        f"descarga de {url} supera max_bytes={max_bytes} (recibidos {total})"
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    async def download_to_temp(self, url: str, *, max_bytes: int | None = None) -> Path:
        """Descarga la URL a un archivo temporal y devuelve su `Path`.

        El archivo vive en un directorio temporal dedicado
        (`tempfile.mkdtemp(prefix="xtrace-crawler-download-")`), fuera del
        repositorio. Si la descarga falla (error HTTP, límite superado,
        cancelación), el directorio temporal se elimina y la excepción se
        propaga: nunca quedan artefactos temporales (FR-015).

        El llamador es responsable de borrar `path.parent` con `try/finally`
        cuando termine de usar el asset (cleanup de pipeline, FR-015).
        """
        self._validate_url(url)
        tmp_dir = Path(tempfile.mkdtemp(prefix="xtrace-crawler-download-"))
        try:
            target = tmp_dir / "asset"
            async with self._client.stream("GET", url) as response:
                response.raise_for_status()
                total = 0
                with target.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            raise DownloadTooLargeError(
                                f"descarga de {url} supera max_bytes={max_bytes} "
                                f"(recibidos {total})"
                            )
                        fh.write(chunk)
            return target
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    # -- Ciclo de vida ---------------------------------------------------------

    async def aclose(self) -> None:
        """Cierra el cliente y libera la conexión subyacente."""
        await self._client.aclose()

    async def __aenter__(self) -> SafeHTTPClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
