"""Tests del cliente HTTP seguro del crawler (PR-024 · SEC-001 · plan §Security strategy).

Validan la superficie SSRF contenida: allowlist de hosts por adapter, https
forzado salvo flag dev explícito, bloqueo de redirects fuera del allowlist,
timeout configurable, User-Agent declarado y descarga de bytes a buffer/archivo
temporal. Todo con `httpx.MockTransport` (sin red real, NFR-003).

**PR-036 · DNS rebinding (plan §Risks, mitigación)**: con
`validate_resolved_ip=True` el cliente resuelve el hostname (resolver
inyectable; el transporte mock no resuelve DNS real) y rechaza IPs
privadas/link-local/loopback/metadata (RFC1918, 169.254.0.0/16 —
incluida 169.254.169.254 —, 127.0.0.0/8, ::1, fc00::/7, fe80::/10) con
`PrivateIPError` antes de emitir la petición. `is_private_ip` es la función
pura validada directamente (unidad pura).

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
    PrivateIPError,
    SafeHTTPClient,
    SchemeNotAllowedError,
    is_private_ip,
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


def test_get_headers_opcionales_se_anaden_sin_sustituir_los_por_defecto() -> None:
    """PR-066: `get(..., headers=...)` añade headers (p. ej. Authorization) sin
    perder el User-Agent por defecto — necesario para el adapter redgifs
    (token temporal en `Authorization: Bearer <token>` por request, SEC-005).
    """
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            await client.get("https://example.com/a", headers={"Authorization": "Bearer tok123"})

    _run(scenario)
    assert seen[0].headers["Authorization"] == "Bearer tok123"
    assert seen[0].headers["User-Agent"] == DEFAULT_USER_AGENT


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


# --- DNS rebinding: validación de la IP resuelta (PR-036 · plan §Risks) --------


@pytest.mark.parametrize(
    "ip",
    [
        # RFC1918 (privadas).
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        # Loopback y unspecified.
        "127.0.0.1",
        "0.0.0.0",
        # Link-local IPv4 (incluye la IP de metadata de cloud 169.254.169.254).
        "169.254.0.1",
        "169.254.169.254",
        # IPv6: loopback, ULA (fc00::/7) y link-local (fe80::/10).
        "::1",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        # IPv4-mapped IPv6 de una IP privada (::ffff:10.0.0.1).
        "::ffff:10.0.0.1",
        # No parseable → fail-closed (tratada como insegura).
        "no-es-una-ip",
    ],
)
def test_is_private_ip_true_para_rangos_internos(ip: str) -> None:
    """PR-036: rangos internos/metadata/loopback/link-local se consideran privados.

    `169.254.169.254` (metadata de cloud) cae dentro de 169.254.0.0/16; una IP
    no parseable se rechaza por seguridad (fail-closed).
    """
    assert is_private_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "2001:4860:4860::8888",
        "2606:4700:4700::1111",
    ],
)
def test_is_private_ip_false_para_ip_publicas(ip: str) -> None:
    """PR-036: IPs públicas (v4/v6) no se consideran privadas."""
    assert is_private_ip(ip) is False


def test_resolved_private_ip_rejected_without_network() -> None:
    """PR-036: la IP resuelta privada se rechaza ANTES de emitir la petición.

    Resolver stub (sin DNS real, NFR-003): la validación aborta con
    `PrivateIPError` y ningún request llega al transporte.
    """
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            validate_resolved_ip=True,
            resolver=lambda host: ["10.0.0.5"],
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            with pytest.raises(PrivateIPError):
                await client.get_bytes("https://example.com/x")

    _run(scenario)
    assert seen == []


@pytest.mark.parametrize("ip", ["169.254.169.254", "127.0.0.1", "fe80::1", "fc00::1"])
def test_resolved_metadata_loopback_linklocal_rejected(ip: str) -> None:
    """PR-036: metadata de cloud (169.254.169.254), loopback y link-local rechazados."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            validate_resolved_ip=True,
            resolver=lambda host: [ip],
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            with pytest.raises(PrivateIPError):
                await client.get_bytes("https://example.com/x")

    _run(scenario)
    assert seen == []


def test_resolved_any_private_ip_rejects_even_with_public_ones() -> None:
    """PR-036: si CUALQUIER IP resuelta es privada se rechaza (la conexión podría
    ir a cualquiera de ellas — defensa DNS rebinding)."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            validate_resolved_ip=True,
            resolver=lambda host: ["93.184.216.34", "10.0.0.5"],
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            with pytest.raises(PrivateIPError):
                await client.get_bytes("https://example.com/x")

    _run(scenario)
    assert seen == []


def test_resolved_public_ip_allowed() -> None:
    """PR-036: una IP resuelta pública pasa la validación y la petición se emite."""
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            validate_resolved_ip=True,
            resolver=lambda host: ["93.184.216.34"],
            transport=httpx.MockTransport(_handler(record=seen)),
        ) as client:
            await client.get_bytes("https://example.com/a")

    _run(scenario)
    assert len(seen) == 1


def test_ip_validation_off_by_default_no_resolution() -> None:
    """PR-036: sin `validate_resolved_ip` no se resuelve NADA (transporte mock).

    El default es `False` para no introducir DNS real donde no se pide (los
    adapters/tests con transporte mock no resuelven; NFR-003).
    """

    def bomb_resolver(host: str) -> list[str]:
        raise AssertionError(f"no debe resolverse nada con la validación apagada: {host}")

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            resolver=bomb_resolver,
            transport=httpx.MockTransport(_handler(content=b"ok")),
        ) as client:
            await client.get_bytes("https://example.com/a")

    _run(scenario)
    # Si llegamos aquí sin AssertionError, el resolver no se invocó.


def test_redirect_hop_with_private_ip_rejected() -> None:
    """PR-036: cada redirect se revalida — un hop que resuelva a IP privada aborta.

    El request-hook revalida la URL en cada salto del redirect loop (SEC-001):
    la validación de IP resuelta también aplica a los hops (anti-rebinding en
    redirects).
    """
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                302,
                headers={"location": "https://example.com/private"},
                content=b"",
                request=request,
            )

        def resolver(host: str) -> list[str]:
            # Cada petición se valida 2 veces (pre-validación de `get_bytes` +
            # request-hook); el 3er intento es el hop del redirect, que resuelve
            # a una IP privada → aborta sin llegar al transporte.
            resolver.calls += 1  # type: ignore[attr-defined]
            return ["10.0.0.9"] if resolver.calls >= 3 else ["93.184.216.34"]  # type: ignore[attr-defined]

        resolver.calls = 0  # type: ignore[attr-defined]

        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            validate_resolved_ip=True,
            resolver=resolver,
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(PrivateIPError):
                await client.get_bytes("https://example.com/a")

    _run(scenario)
    # Solo el request inicial se emitió; el hop hacia la IP privada no llega al transporte.
    assert [r.url.path for r in seen] == ["/a"]
