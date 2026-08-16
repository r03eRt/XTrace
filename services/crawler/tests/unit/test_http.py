"""Tests del cliente HTTP seguro del crawler (PR-024 · SEC-001 · plan §Security strategy).

Validan la superficie SSRF contenida: allowlist de hosts por adapter, https
forzado salvo flag dev explícito, bloqueo de redirects fuera del allowlist,
timeout configurable, User-Agent declarado y descarga de bytes a buffer/archivo
temporal. Todo con `httpx.MockTransport` (sin red real, NFR-003).

Requisitos: SEC-001 (no saltarse protecciones ni acceder a recursos no
permitidos; solo recursos públicos legalmente accesibles) y el §Security
strategy del plan (anti-SSRF). La limpieza de temporales valida FR-015
(sin artefactos temporales tras fallo).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from xtrace_crawler.crawling.http import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    DownloadTooLargeError,
    HostNotAllowedError,
    SafeHTTPClient,
    SchemeNotAllowedError,
)


def _handler(
    *,
    status: int = 200,
    content: bytes = b"ok",
    location: str | None = None,
    record: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Handler de MockTransport: responde y opcionalmente registra cada request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        headers = {"location": location} if location else None
        return httpx.Response(status, headers=headers, content=content, request=request)

    return handler


def _run(coro: Callable[[], object]) -> None:
    """Ejecuta el escenario async sin dependencia de pytest-asyncio (determinista)."""
    asyncio.run(coro())


# --- Allowlist de hosts (SEC-001 / plan §Security strategy) ---


def test_host_fuera_de_allowlist_rechazado() -> None:
    """SEC-001: un host fuera de la allowlist se rechaza antes de tocar la red."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            with pytest.raises(HostNotAllowedError):
                await client.get_bytes("https://evil.example.com/x")

    _run(scenario)
    # Nunca llega un request al transporte: validación previa a la red.
    assert seen == []


def test_host_en_allowlist_aceptado() -> None:
    """SEC-001: el host permitido se descarga con normalidad."""
    body: bytes = b""

    async def scenario() -> None:
        nonlocal body
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"ok-body")),
        ) as client:
            body = await client.get_bytes("https://example.com/video")

    _run(scenario)
    assert body == b"ok-body"


def test_allowlist_case_insensitive_y_puerto_ignorado() -> None:
    """SEC-001: el match es por host (case-insensitive) sin importar el puerto."""

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler()),
        ) as client:
            await client.get_bytes("https://EXAMPLE.com:8443/a")

    _run(scenario)


def test_subdominio_no_implicitamente_permitido() -> None:
    """SEC-001: el match es exacto; un subdominio no hereda la allowlist."""

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler()),
        ) as client:
            with pytest.raises(HostNotAllowedError):
                await client.get_bytes("https://sub.example.com/a")

    _run(scenario)


# --- https forzado salvo flag dev (SEC-001 / plan §Security strategy) ---


def test_http_rechazado_sin_flag_dev() -> None:
    """SEC-001: http está prohibido por defecto (solo https)."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            with pytest.raises(SchemeNotAllowedError):
                await client.get_bytes("http://example.com/a")

    _run(scenario)
    assert seen == []


def test_http_permitido_con_flag_dev_explicito() -> None:
    """plan §Security strategy: http solo con el flag dev explícito (allow_http)."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            allow_http=True,
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            await client.get_bytes("http://example.com/a")

    _run(scenario)
    assert len(seen) == 1
    assert seen[0].url.scheme == "http"


def test_esquema_no_http_rechazado_siempre() -> None:
    """SEC-001: esquemas ajenos a http/https se rechazan incluso con allow_http."""

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            allow_http=True,
            transport=httpx.MockTransport(_handler()),
        ) as client:
            with pytest.raises(SchemeNotAllowedError):
                await client.get_bytes("ftp://example.com/a")

    _run(scenario)


def test_userinfo_rechazado() -> None:
    """SEC-001 (anti-SSRF): userinfo en la URL se rechaza (ofusca el host real)."""

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler()),
        ) as client:
            with pytest.raises(ValueError):
                await client.get_bytes("https://user:pass@example.com/a")

    _run(scenario)


# --- Redirects (SEC-001 / plan §Security strategy) ---


def test_redirect_fuera_de_allowlist_bloqueado() -> None:
    """SEC-001: un redirect a un host fuera de la allowlist aborta la petición."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                302,
                headers={"location": "https://evil.example.com/steal"},
                content=b"",
                request=request,
            )

        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(HostNotAllowedError):
                await client.get_bytes("https://example.com/a")

    _run(scenario)
    # Solo se emitió el request inicial; el hop hacia el host no permitido no llega al transporte.
    assert [r.url.host for r in seen] == ["example.com"]


def test_redirect_dentro_de_allowlist_se_sigue() -> None:
    """SEC-001: un redirect dentro de la allowlist se sigue y devuelve el body final."""
    seen: list[httpx.Request] = []
    body: bytes = b""

    async def scenario() -> None:
        nonlocal body

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/a":
                return httpx.Response(
                    302,
                    headers={"location": "https://example.com/b"},
                    content=b"",
                    request=request,
                )
            return httpx.Response(200, content=b"final", request=request)

        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(handler),
        ) as client:
            body = await client.get_bytes("https://example.com/a")

    _run(scenario)
    assert body == b"final"
    assert [r.url.path for r in seen] == ["/a", "/b"]


# --- Timeout configurable (plan §Security strategy) ---


@pytest.mark.parametrize(
    ("timeout_seconds", "expected"),
    [(5.0, 5.0), (DEFAULT_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)],
)
def test_timeout_configurable_se_propaga_al_transporte(
    timeout_seconds: float, expected: float
) -> None:
    """plan: el timeout configurado llega al transporte en cada request (sin red)."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            timeout_seconds=timeout_seconds,
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            await client.get_bytes("https://example.com/a")

    _run(scenario)
    # httpx 0.28 expone el timeout en extensions como dict con las fases
    # connect/read/write/pool (todas = timeout_seconds).
    assert seen[0].extensions["timeout"]["read"] == expected


# --- User-Agent declarado (plan §Security strategy / SEC-001) ---


def test_user_agent_declarado_por_defecto() -> None:
    """plan: cada request declara el User-Agent del crawler."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            await client.get_bytes("https://example.com/a")

    _run(scenario)
    assert seen[0].headers["User-Agent"] == DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.startswith("XTraceCrawler/")


def test_user_agent_override() -> None:
    """plan: el User-Agent se puede declarar explícitamente por adapter."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            user_agent="CustomUA/1.0",
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            await client.get_bytes("https://example.com/a")

    _run(scenario)
    assert seen[0].headers["User-Agent"] == "CustomUA/1.0"


# --- Descarga de bytes a buffer (plan) ---


def test_get_bytes_devuelve_contenido() -> None:
    """plan: get_bytes devuelve el body como bytes."""
    body: bytes = b""

    async def scenario() -> None:
        nonlocal body
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"\x00\x01\x02")),
        ) as client:
            body = await client.get_bytes("https://example.com/asset")

    _run(scenario)
    assert body == b"\x00\x01\x02"


def test_get_bytes_error_http_levanta() -> None:
    """plan: respuestas de error (p. ej. 404) levantan HTTPStatusError."""

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(status=404, content=b"")),
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_bytes("https://example.com/removed")

    _run(scenario)


def test_get_bytes_respeta_max_bytes() -> None:
    """plan: el límite max_bytes aborta descargas desmesuradas y el límite exacto pasa."""
    body: bytes = b""

    async def scenario() -> None:
        nonlocal body
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"x" * 100)),
        ) as client:
            with pytest.raises(DownloadTooLargeError):
                await client.get_bytes("https://example.com/big", max_bytes=50)
            body = await client.get_bytes("https://example.com/big", max_bytes=100)

    _run(scenario)
    assert body == b"x" * 100


# --- Descarga a archivo temporal (FR-015 / plan) ---


def test_download_to_temp_escribe_archivo_temporal() -> None:
    """FR-015: descarga a archivo temporal fuera del repositorio, con el contenido exacto."""
    path: Path

    async def scenario() -> None:
        nonlocal path
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"asset-bytes")),
        ) as client:
            path = await client.download_to_temp("https://example.com/asset")

    _run(scenario)
    try:
        assert path.is_file()
        assert path.read_bytes() == b"asset-bytes"
        # Vive en el directorio temporal del sistema, no en el repositorio
        # (comparación sin resolve(): macOS symlinkea /var -> /private/var).
        assert str(path.parent).startswith(tempfile.gettempdir())
        assert path.parent.name.startswith("xtrace-crawler-download-")
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def _leftover_temp_dirs() -> list[Path]:
    return [
        p
        for p in Path(tempfile.gettempdir()).iterdir()
        if p.is_dir() and p.name.startswith("xtrace-crawler-download-")
    ]


def test_download_to_temp_limpia_temporales_al_fallar() -> None:
    """FR-015: si la descarga falla (límite superado), no quedan artefactos temporales."""
    before = _leftover_temp_dirs()

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"y" * 100)),
        ) as client:
            with pytest.raises(DownloadTooLargeError):
                await client.download_to_temp("https://example.com/big", max_bytes=10)

    _run(scenario)
    assert _leftover_temp_dirs() == before


def test_download_to_temp_error_http_no_deja_temporales() -> None:
    """FR-015: un error HTTP (500) durante la descarga tampoco deja temporales."""
    before = _leftover_temp_dirs()

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(status=500, content=b"")),
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.download_to_temp("https://example.com/boom")

    _run(scenario)
    assert _leftover_temp_dirs() == before
