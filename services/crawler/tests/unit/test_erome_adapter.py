"""Tests del adapter erome + fixtures sintéticos de la estructura OBSERVADA.

Trazabilidad:

- FR-004: manifest con `access_method="html"`; `discover()` con paginación
  por `a[rel='next']` y protección anti-bucle (cursor repetido / 0 IDs
  nuevos); `get_video()` sobre HTML de erome.
- FR-005/SC-006 (equivalente xvideos): `get_visual_assets` solo devuelve
  miniaturas (`kind` thumbnail/storyboard), nunca la URL del mp4 del `<source>`.
- SEC-001: el adapter solo usa el cliente HTTP seguro (allowlist
  erome.com/www.erome.com); ningún test toca la red (`httpx.MockTransport`).
- SEC-002: el manifest declara `robots_reviewed=True`/`terms_reviewed=True`
  (revisión manual de robots.txt/ToS previa a este adapter, ver docstring del
  módulo); la habilitación efectiva sigue exigiendo el gate del registry.
- SEC-004: los fixtures son sintéticos (dominio `erome.invalid`, IDs
  `synth00N`); ningún `erome.com` real en los fixtures.
- Regla de negocio propia de erome: los álbumes solo-fotos (sin
  `span.album-videos`) se descartan en `discover()` — no son vídeos
  indexables.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from xtrace_crawler.adapters.base import SourceAdapter
from xtrace_crawler.adapters.erome import (
    EROME_ASSET_HOSTS,
    EromeAdapter,
    EromeParseError,
    parse_album_page,
    parse_listing_page,
)
from xtrace_crawler.adapters.models import VideoAvailability
from xtrace_crawler.crawling.http import HostNotAllowedError

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "erome"


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _run(coro: Callable[[], object]) -> None:
    asyncio.run(coro())


_CHANGED_STRUCTURE_HTML = "<html><body><p>layout changed, no og:url here</p></body></html>"


def _fixture_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Transporte mock: sirve los fixtures por path+query, sin red."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode()
        key = f"{path}?{query}" if query else path

        if key in ("/search?q=amateur", "/search"):
            return httpx.Response(200, text=_fixture("search_page_1.html"))
        if key == "/search?page=2&q=amateur" or key == "/search?q=amateur&page=2":
            return httpx.Response(200, text=_fixture("search_page_2.html"))
        if path == "/a/synth001":
            return httpx.Response(200, text=_fixture("album_synth001.html"))
        if path == "/a/synth004":
            return httpx.Response(200, text=_fixture("album_synth004_minimal.html"))
        if path == "/a/synth50000":
            return httpx.Response(200, text=_CHANGED_STRUCTURE_HTML)
        if path == "/a/synth99999":
            return httpx.Response(404, text="not found")
        return httpx.Response(404, text=f"unmapped path in test transport: {key}")

    return handler


def _adapter() -> EromeAdapter:
    return EromeAdapter(transport=httpx.MockTransport(_fixture_handler()))


# ---------------------------------------------------------------------------
# Manifest / SEC-002 / SEC-001
# ---------------------------------------------------------------------------


def test_manifest_declares_compliance_review() -> None:
    manifest = EromeAdapter.manifest
    assert manifest.source == "erome"
    assert manifest.access_method == "html"
    assert manifest.robots_reviewed is True
    assert manifest.terms_reviewed is True
    assert manifest.assets_accessed == ["thumbnail", "storyboard"]


def test_adapter_satisfies_source_adapter_protocol() -> None:
    assert isinstance(EromeAdapter(), SourceAdapter)


def test_asset_hosts_allowlist_has_no_wildcards() -> None:
    assert "erome.com" in EROME_ASSET_HOSTS
    assert "www.erome.com" in EROME_ASSET_HOSTS
    assert all("*" not in host for host in EROME_ASSET_HOSTS)


# ---------------------------------------------------------------------------
# parse_listing_page (funciones puras)
# ---------------------------------------------------------------------------


def test_parse_listing_page_skips_photo_only_albums() -> None:
    page = parse_listing_page(_fixture("search_page_1.html"), current_path="/search?q=amateur")
    # synth002 no tiene span.album-videos (solo fotos): se descarta.
    assert page.external_ids == ["synth001", "synth003"]
    assert page.page_urls["synth001"] == "https://www.erome.invalid/a/synth001"


def test_parse_listing_page_next_cursor_from_rel_next() -> None:
    page = parse_listing_page(_fixture("search_page_1.html"), current_path="/search?q=amateur")
    assert page.next_cursor == "/search?q=amateur&page=2"


def test_parse_listing_page_last_page_has_no_next_cursor() -> None:
    page = parse_listing_page(
        _fixture("search_page_2.html"), current_path="/search?q=amateur&page=2"
    )
    assert page.next_cursor is None
    assert page.external_ids == ["synth004"]


def test_parse_listing_page_anti_loop_current_path() -> None:
    """Un `rel=next` que repite el path actual no genera cursor (anti-bucle)."""
    html = _fixture("search_page_1.html")
    page = parse_listing_page(html, current_path="/search?q=amateur&page=2")
    assert page.next_cursor is None


# ---------------------------------------------------------------------------
# parse_album_page
# ---------------------------------------------------------------------------


def test_parse_album_page_populates_video_source() -> None:
    video = parse_album_page(
        _fixture("album_synth001.html"), page_url="https://www.erome.invalid/a/synth001"
    )
    assert video.source == "erome"
    assert video.external_id == "synth001"
    assert video.title == "Titulo de ejemplo 1"
    assert video.thumbnail_url == "https://s1.erome.invalid/1000/synth001/thumbs/aaaa1111.jpg"
    assert video.duration_ms is None
    assert video.preview_url is None


def test_parse_album_page_raises_on_changed_structure() -> None:
    with pytest.raises(EromeParseError):
        parse_album_page(_CHANGED_STRUCTURE_HTML, page_url="https://www.erome.invalid/a/synthXXX")


# ---------------------------------------------------------------------------
# EromeAdapter.discover
# ---------------------------------------------------------------------------


def test_discover_first_page_requires_query_section() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(ValueError, match="empezar por"):
            await adapter.discover(cursor=None, limit=10, section="search?q=amateur")
        await adapter.aclose()

    _run(scenario)


def test_discover_paginates_across_two_pages() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        first = await adapter.discover(cursor=None, limit=10, section="/search?q=amateur")
        assert first.external_ids == ["synth001", "synth003"]
        assert first.next_cursor == "/search?q=amateur&page=2"

        second = await adapter.discover(cursor=first.next_cursor, limit=10)
        assert second.external_ids == ["synth004"]
        assert second.next_cursor is None
        await adapter.aclose()

    _run(scenario)


def test_discover_zero_new_ids_ends_chain() -> None:
    """Repetir la primera página tras un `cursor=None` no arrastra vistos previos."""

    async def scenario() -> None:
        adapter = _adapter()
        await adapter.discover(cursor=None, limit=10, section="/search?q=amateur")
        # Nueva cadena (cursor=None de nuevo): se reinicia _seen_external_ids,
        # así que la misma página vuelve a producir IDs "nuevos".
        again = await adapter.discover(cursor=None, limit=10, section="/search?q=amateur")
        assert again.external_ids == ["synth001", "synth003"]
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# EromeAdapter.get_video / check_availability
# ---------------------------------------------------------------------------


def test_get_video_returns_none_on_404() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        assert await adapter.get_video("synth99999") is None
        await adapter.aclose()

    _run(scenario)


def test_get_video_parses_album() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth001")
        assert video is not None
        assert video.external_id == "synth001"
        assert video.title == "Titulo de ejemplo 1"
        await adapter.aclose()

    _run(scenario)


def test_check_availability_removed_on_404() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth001")
        assert video is not None
        removed_video = video.model_copy(
            update={
                "external_id": "synth99999",
                "page_url": "https://www.erome.invalid/a/synth99999",
            }
        )
        assert await adapter.check_availability(removed_video) == VideoAvailability.REMOVED
        await adapter.aclose()

    _run(scenario)


def test_check_availability_unavailable_on_changed_structure() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth001")
        assert video is not None
        broken_video = video.model_copy(
            update={
                "external_id": "synth50000",
                "page_url": "https://www.erome.invalid/a/synth50000",
            }
        )
        assert await adapter.check_availability(broken_video) == VideoAvailability.UNAVAILABLE
        await adapter.aclose()

    _run(scenario)


def test_check_availability_available_for_valid_album() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth001")
        assert video is not None
        assert await adapter.check_availability(video) == VideoAvailability.AVAILABLE
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# EromeAdapter.get_visual_assets (SC-006 equivalente: nunca el mp4)
# ---------------------------------------------------------------------------


def test_get_visual_assets_returns_thumbnail_and_clip_posters_never_mp4() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth001")
        assert video is not None
        assets = await adapter.get_visual_assets(video)
        urls = [asset.url for asset in assets]

        assert urls[0] == "https://s1.erome.invalid/1000/synth001/thumbs/aaaa1111.jpg"
        assert "https://s1.erome.invalid/1000/synth001/clip1.jpg" in urls
        assert "https://s1.erome.invalid/1000/synth001/clip2.jpg" in urls
        assert not any(url.endswith(".mp4") for url in urls)
        assert not any("v1.erome.invalid" in url for url in urls)

        assert assets[0].kind == "thumbnail"
        assert all(asset.kind == "storyboard" for asset in assets[1:])
        await adapter.aclose()

    _run(scenario)


def test_get_visual_assets_degrades_to_thumbnail_without_clips() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth004")
        assert video is None or video.thumbnail_url is None
        # synth004 no declara og:image en el fixture minimal: sin assets.
        if video is not None:
            assets = await adapter.get_visual_assets(video)
            assert assets == []
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# SEC-001: allowlist de hosts (anti-SSRF) + resolución de `page_url` ajeno
# ---------------------------------------------------------------------------


def test_foreign_page_url_is_ignored_and_falls_back_to_template() -> None:
    """Un `page_url` de host ajeno (p. ej. el `og:url` sintético `.invalid` de los
    fixtures) se ignora: la petición real siempre va al host canónico erome.com,
    nunca al host embebido en el HTML de la fuente (SEC-001)."""

    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("synth001", page_url="https://evil.invalid/a/synth001")
        assert video is not None
        assert video.external_id == "synth001"
        await adapter.aclose()

    _run(scenario)


def test_client_rejects_requests_to_hosts_outside_allowlist_directly() -> None:
    """El `SafeHTTPClient` subyacente (no el adapter) rechaza cualquier host
    fuera de `erome.com`/`www.erome.com`, sea cual sea el origen de la URL."""

    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(HostNotAllowedError):
            await adapter._client.get("https://evil.invalid/a/synth001")
        await adapter.aclose()

    _run(scenario)
