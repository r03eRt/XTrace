"""Tests del adapter xhamster + fixtures sintéticos de la estructura OBSERVADA
(PR-062 · FR-001…FR-006 · SEC-001/003/004 · NFR-003 · SC-001/004/005 · ADR-0015).

Trazabilidad (constitución §3): cada test marca el requisito que valida:

- FR-001: el adapter cumple el protocolo `SourceAdapter` (discover/get_video/
  get_visual_assets/check_availability + manifest) y el manifest documenta el
  compliance (SEC-002).
- FR-002: método de acceso "html" documentado en el manifest (sin API/feed ni
  sitemap accesible; prospección 2026-08-19).
- FR-003: `discover()` con `section` OBLIGATORIO (Decisión D2: sin sección no
  se explora la home → error tipado fail-fast), paginación por cursor
  (`a.page-button-link` → path del enlace siguiente al activo), salto de
  numeración (`/16828`) y protección anti-bucle (cursor repetido / 0 IDs
  nuevos → fin); truncación no soportada → `XhamsterParseError`.
- FR-004: `get_video()` normaliza desde la página de vídeo (og:* +
  `window.initials.videoModel`), con `external_id` estable desde la URL
  canónica `/videos/<slug>-<id>` (formas numérica y alfanumérica), `page_urls`
  con href completo (paridad PR-045) y campos opcionales nulos.
- FR-005: `get_visual_assets()` devuelve UN storyboard (sprite del vídeo
  principal, desde `spriteLoader.template` — los `data-sprite` del HTML son de
  vídeos RELACIONADOS y NO se usan) + UN thumbnail; sin sprite → solo
  thumbnail; `preview_url=None` SIEMPRE (Decisión D3 · SC-004: los mp4 de
  preview/trailer no se exponen).
- FR-006/SEC-001: `asset_hosts` PROVISIONAL con los hosts observados
  (`thumb-v0..9.xhcdn.com`, `ic-vt-nss.xhcdn.com`), solo hosts, nunca derivada
  de las URLs parseadas; allowlist de página `xhamster.com`/`www.xhamster.com`/
  `es.xhamster.com` (D1 + corrección A1: `es.*` como objetivo de redirect/URL
  canónica) con `httpx.MockTransport` (sin red, NFR-003).
- SEC-003: el adapter activa la validación anti-DNS-rebinding del
  `SafeHTTPClient` sin red real (resolver inyectable).
- SEC-004: los fixtures son sintéticos (dominios `xhamster.invalid`/
  `xhcdn.invalid`, títulos anonimizados "Titulo de ejemplo N", IDs sintéticos;
  ningún `xhamster.com` real en los fixtures).
- SC-001: el flujo completo (discover → get_video → get_visual_assets →
  check_availability) se ejecuta con fixtures, sin red, de forma determinista.
- SC-004: 0 assets mp4 (solo storyboard/thumbnail); SC-005: rate limit
  conservador declarado (2000 ms / 0.5 rps).
- SC-007: el core no importa el adapter (añadir esta fuente no toca el core;
  test AST).

Estructura HTML observada: ver `tests/fixtures/xhamster/README.md`
(prospección 2026-08-19). Los fixtures congelan la estructura observada y los
tests fallan con mensaje claro si un selector clave cambia (regresión).
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from xtrace_crawler import __file__ as _crawler_pkg_init
from xtrace_crawler.adapters.base import SourceAdapter
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.adapters.xhamster import (
    XH_VIDEO_HOSTS,
    XhamsterAdapter,
    XhamsterParseError,
    parse_listing_page,
    parse_video_page,
    storyboard_grid,
)
from xtrace_crawler.crawling.http import HostNotAllowedError, PrivateIPError, SafeHTTPClient

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "xhamster"

#: Sprite del vídeo principal (fixture full; ADR-0015 §3: `160x160.50.s.jpg` →
#: fichero real 8000×131 → 50 tiles de 160×131, spriteCount=50).
FIXTURE_SPRITE_TEMPLATE = (
    "https://thumb-v7.xhcdn.invalid/a/tokenejemplo0001/002/533/587/160x160.50.s.jpg"
)

#: Hover sprite de un vídeo RELACIONADO (fixture full): `526x298.s.webp` →
#: 5260×298 → 20 tiles de 263×298; NO se usa como storyboard del vídeo.
FIXTURE_RELATED_SPRITE = (
    "https://thumb-v8.xhcdn.invalid/a/tokenejemplo0002/017/608/858/526x298.s.webp"
)

#: Thumbnail og:image del fixture full (host `ic-vt-nss.xhcdn.invalid`).
FIXTURE_THUMBNAIL = "https://ic-vt-nss.xhcdn.invalid/a/tokenejemplo0001/002/533/587/1280x720.6.jpg"

#: Página de vídeo servida en `/categories/amateur/16828` (numeración alta): el
#: activo es 16828 y el siguiente salta a la última página (anti-bucle: el
#: `page-limit-button` duplica el enlace de la última página).
_JUMP_PAGE_HTML = """<html><body>
<div class="thumb-list">
  <div class="video-thumb" data-video-id="s000006">
    <a class="video-thumb__image-container" data-role="thumb-link"
       href="/videos/titulo-de-ejemplo-6-3000006">v</a>
  </div>
</div>
<ol class="page-list">
  <li class="page-button">
    <a class="page-button-link page-button-link--active"
       href="/categories/amateur/16828">16828</a>
  </li>
  <li class="page-button">
    <a class="page-button-link" href="/categories/amateur/33654">33654</a>
  </li>
</ol>
</body></html>"""

#: ÚLTIMA página (`/categories/amateur/33654`): el activo es el último enlace
#: numerado y el `page-limit-button` repite el path actual → fin (None).
_LAST_PAGE_HTML = """<html><body>
<div class="thumb-list">
  <div class="video-thumb" data-video-id="s000007">
    <a class="video-thumb__image-container" data-role="thumb-link"
       href="/videos/titulo-de-ejemplo-7-3000007">v</a>
  </div>
</div>
<ol class="page-list">
  <li class="page-button">
    <a class="page-button-link page-button-link--active"
       href="/categories/amateur/33654">33654</a>
  </li>
</ol>
<div class="page-limit-button page-limit-button--right">
  <a class="page-button-link" href="/categories/amateur/33654">33654</a>
</div>
</body></html>"""


def _initials_script(payload: dict[str, object]) -> str:
    """`<script>` de `window.initials` con JSON válido (páginas inline sintéticas).

    Evita f-strings con llaves literales (PEP 701) y garantiza JSON parseable.
    """
    return "<script id='initials-script'>window.initials=" + json.dumps(payload) + "</script>"


def _fixture(name: str) -> str:
    """Lee un fixture sintético de `tests/fixtures/xhamster/` (SEC-004)."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _run(coro: Callable[[], Coroutine[object, object, object]]) -> None:
    """Ejecuta el escenario async sin dependencia de pytest-asyncio (determinista)."""
    asyncio.run(coro())


def _fixture_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Transporte mock: sirve los fixtures por path, sin red (NFR-003).

    - `/categories/amateur` → `category_page_1.html` (ids 3000001/xhT0001/
      3000003, cursor → `/categories/amateur/2`)
    - `/categories/amateur/2` → `category_page_2.html` (ids 3000004/xhT0005,
      cursor → `/categories/amateur/16828` — salto de numeración)
    - `/categories/amateur/16828` → `_JUMP_PAGE_HTML` (activo=16828 →
      cursor `/categories/amateur/33654`)
    - `/categories/amateur/33654` → `_LAST_PAGE_HTML` (última página → fin)
    - `/categories/amateur/99` → `category_page_1.html` de nuevo (0 IDs
      nuevos → fin, anti-bucle)
    - `/videos/titulo-de-ejemplo-1-3000001` y `/videos/x-3000001` →
      `video_page_full.html`
    - `/videos/titulo-de-ejemplo-2-xhT0001` y `/videos/x-xhT0001` →
      `video_page_minimal.html`
    - `/videos/titulo-de-ejemplo-3-3000003` y `/videos/x-3000003` →
      `video_page_sin_sprite.html`
    - `/videos/x-9000000` → 404 (vídeo retirado)
    - `/videos/x-9000001` → 200 con HTML sin estructura de vídeo (estructura
      cambiada)
    - cualquier otro path → 500 (error transitorio del sitio)
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: bytes
        if path == "/categories/amateur":
            body = _fixture("category_page_1.html").encode("utf-8")
        elif path == "/categories/amateur/2":
            body = _fixture("category_page_2.html").encode("utf-8")
        elif path == "/categories/amateur/16828":
            body = _JUMP_PAGE_HTML.encode("utf-8")
        elif path == "/categories/amateur/33654":
            body = _LAST_PAGE_HTML.encode("utf-8")
        elif path == "/categories/amateur/99":
            body = _fixture("category_page_1.html").encode("utf-8")
        elif path in ("/videos/titulo-de-ejemplo-1-3000001", "/videos/x-3000001"):
            body = _fixture("video_page_full.html").encode("utf-8")
        elif path in ("/videos/titulo-de-ejemplo-2-xhT0001", "/videos/x-xhT0001"):
            body = _fixture("video_page_minimal.html").encode("utf-8")
        elif path in ("/videos/titulo-de-ejemplo-3-3000003", "/videos/x-3000003"):
            body = _fixture("video_page_sin_sprite.html").encode("utf-8")
        elif path == "/videos/x-9000000":
            return httpx.Response(404, content=b"", request=request)
        elif path == "/videos/x-9000001":
            body = b"<html><body>captcha o estructura totalmente distinta</body></html>"
        else:
            return httpx.Response(500, content=b"boom", request=request)
        return httpx.Response(200, content=body, request=request)

    return handler


def _tracking_handler(
    requested: list[str], handler: Callable[[httpx.Request], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """Envuelve un handler registrando cada URL pedida (para asserts de peticiones)."""

    def tracked(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    return tracked


def _adapter() -> XhamsterAdapter:
    """Adapter con transporte mock: ningún test toca la red (NFR-003, SEC-001)."""
    return XhamsterAdapter(transport=httpx.MockTransport(_fixture_handler()))


def _full_video() -> VideoSource:
    return parse_video_page(
        _fixture("video_page_full.html"),
        page_url="https://xhamster.invalid/videos/titulo-de-ejemplo-1-3000001",
    )


# ---------------------------------------------------------------------------
# FR-001/FR-002/SEC-002/D5 · Manifest + protocolo + asset_hosts (FR-006/SEC-001)
# ---------------------------------------------------------------------------


def test_manifest_revisado_por_el_operador_en_modo_prueba() -> None:
    """SEC-002 · Decisión D5 (2026-08-19): manifest REVISADO (modo prueba).

    `robots_reviewed=True`, `terms_reviewed=True` y `review_date="2026-08-19"`
    (OK del operador en modo prueba). La habilitación **efectiva** sigue
    exigiendo el gate del registry (PR-028): manifest conforme Y
    `sources.enabled=true` en BD (lo prueba PR-063 en `test_registry.py`).
    FR-002: método de acceso `html` (sin API/feed ni sitemap accesible).
    FR-005: assets_accessed = ["storyboard", "thumbnail"] (sin previews, D3).
    SC-005: rate limit conservador (2000 ms / 0.5 rps, D5).
    """
    manifest = XhamsterAdapter.manifest
    assert manifest.source == "xhamster"
    assert manifest.access_method == "html"  # FR-004: jerarquía documentada
    assert manifest.assets_accessed == ["storyboard", "thumbnail"]
    assert manifest.robots_reviewed is True
    assert manifest.terms_reviewed is True
    assert manifest.review_date == "2026-08-19"
    assert manifest.rate_limit.min_interval_ms == 2_000
    assert manifest.rate_limit.max_rps == 0.5


def test_adapter_satisface_protocolo_source_adapter() -> None:
    """FR-001: XhamsterAdapter cumple estructuralmente el protocolo SourceAdapter."""
    adapter = _adapter()
    # Chequeo estático del protocolo (mypy valida la firma en compilación).
    protocol_adapter: SourceAdapter = adapter
    assert isinstance(protocol_adapter, SourceAdapter)


def test_asset_hosts_provisionales_solo_hosts_observados() -> None:
    """FR-006 · SEC-001 · contracts §1: `asset_hosts` = hosts OBSERVADOS, solo hosts.

    Allowlist PROVISIONAL de la prospección 2026-08-19: `thumb-v0..v9.xhcdn.com`
    (CDN del sprite del player) y `ic-vt-nss.xhcdn.com` (thumbnail og:image).
    Nunca derivada de las URLs parseadas (fail-closed). Solo hosts: sin
    esquemas, rutas, query ni fragmentos.
    """
    hosts = XhamsterAdapter.asset_hosts
    assert hosts  # PROVISIONAL — no vacía
    for index in range(10):
        assert f"thumb-v{index}.xhcdn.com" in hosts
    assert "ic-vt-nss.xhcdn.com" in hosts
    for invented in ("thumb.xhcdn.com", "thumb-v10.xhcdn.com", "cdn.xhcdn.com"):
        assert invented not in hosts, f"host no observado en asset_hosts: {invented!r}"
    for host in hosts:
        assert "://" not in host, f"esquema en asset_host: {host!r}"
        assert "/" not in host, f"ruta en asset_host: {host!r}"
        assert "?" not in host and "#" not in host, f"query/fragmento en asset_host: {host!r}"
        assert host.strip() == host, f"espacios en asset_host: {host!r}"


# ---------------------------------------------------------------------------
# FR-004 · Parseo de la página de vídeo (og:* + window.initials.videoModel)
# ---------------------------------------------------------------------------


def test_parse_video_page_full_fixture_metadatos_completos() -> None:
    """FR-004/FR-002: el fixture completo produce VideoSource con todos los campos.

    title de og:title; duration_ms de `videoModel.duration` (234 s → ms);
    published_at (tz-aware UTC) de `videoModel.created` (epoch s); tags de
    `videoModel.tags` (`{name}`); thumbnail de og:image; **page_url = og:url**;
    **storyboard_urls = [spriteLoader.template]** (sprite del vídeo principal);
    **preview_url=None** (D3 · SC-004: los mp4 de preview/trailer no se exponen).
    """
    video = parse_video_page(
        _fixture("video_page_full.html"),
        page_url="https://xhamster.invalid/videos/titulo-de-ejemplo-1-3000001",
    )
    assert video.source == "xhamster"
    assert video.external_id == "3000001"  # forma numérica, último segmento del path
    assert video.title == "Titulo de ejemplo 1"
    assert video.duration_ms == 234_000  # videoModel.duration 234 s → ms
    assert video.thumbnail_url == FIXTURE_THUMBNAIL
    assert video.preview_url is None  # D3 · SC-004: previews mp4 no expuestos en v1
    assert video.storyboard_urls == [FIXTURE_SPRITE_TEMPLATE]
    assert video.tags == ["tag de ejemplo uno", "tag de ejemplo dos"]
    assert video.published_at == datetime(2013, 12, 24, 8, 1, 1, tzinfo=UTC)
    assert video.page_url == "https://xhamster.invalid/videos/titulo-de-ejemplo-1-3000001"


def test_parse_video_page_minimal_fixture_opcionales_none() -> None:
    """Spec edge case: sin duración/fecha/tags/sprite/thumbnail → None/[].

    Además cubre la forma ALFANUMÉRICA del id (`xhT0001`). El vídeo sigue
    procesándose (metadatos incompletos no bloquean).
    """
    video = parse_video_page(
        _fixture("video_page_minimal.html"),
        page_url="https://xhamster.invalid/videos/titulo-de-ejemplo-2-xhT0001",
    )
    assert video.external_id == "xhT0001"  # forma alfanumérica
    assert video.title == "Titulo de ejemplo 2"
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.preview_url is None
    assert video.storyboard_urls == []
    assert video.tags == []
    assert video.published_at is None
    assert video.page_url == "https://xhamster.invalid/videos/titulo-de-ejemplo-2-xhT0001"


def test_parse_video_page_sin_sprite_storyboard_vacio_thumbnail_presente() -> None:
    """FR-005: sin `spriteLoader.template` → `storyboard_urls=[]`.

    El fixture sin sprite conserva el resto de metadatos (duración, fecha,
    tags por fallback `keywords`) y el thumbnail (og:image): la degradación a
    thumbnail único ocurre en `get_visual_assets` sin perder el vídeo.
    """
    video = parse_video_page(
        _fixture("video_page_sin_sprite.html"),
        page_url="https://xhamster.invalid/videos/titulo-de-ejemplo-3-3000003",
    )
    assert video.external_id == "3000003"
    assert video.title == "Titulo de ejemplo 3"
    assert video.duration_ms == 120_000
    assert video.storyboard_urls == []
    assert video.thumbnail_url == (
        "https://ic-vt-nss.xhcdn.invalid/a/tokenejemplo0003/002/533/587/1280x720.6.jpg"
    )
    # Fallback `keywords` (string separada por comas, forma observada).
    assert video.tags == [
        "tag de ejemplo uno",
        "tag de ejemplo dos",
        "tag de ejemplo tres",
    ]
    assert video.published_at == datetime(2017, 7, 14, 2, 40, tzinfo=UTC)


def test_parse_video_page_sprite_template_elegido_y_data_sprite_ignorados() -> None:
    """FR-005/ADR-0015 §3: el sprite del VÍDEO PRINCIPAL sale del player config.

    El fixture completo incluye un `data-sprite` (hover 526x298.s.webp) de un
    vídeo RELACIONADO (path `/017/608/858/` distinto al del principal
    `/002/533/587/`): el parser debe elegir `spriteLoader.template` y NUNCA el
    `data-sprite` del HTML.
    """
    video = parse_video_page(
        _fixture("video_page_full.html"),
        page_url="https://xhamster.invalid/videos/titulo-de-ejemplo-1-3000001",
    )
    assert FIXTURE_RELATED_SPRITE not in video.storyboard_urls
    assert video.storyboard_urls == [FIXTURE_SPRITE_TEMPLATE]


def test_parse_video_page_template_anidado_de_la_captura_real() -> None:
    """Robustez (hallazgo de prospección 2026-08-19): template anidado aceptado.

    La captura real sirve el template en
    `window.initials.xplayerPluginSettings.spriteLoader.template` (y reflejado
    en `videoModel.spriteURL`): el parser acepta ambas formas — siempre desde
    `window.initials`, nunca desde atributos del HTML. La forma canónica de la
    spec (`spriteLoader.template` top-level) es la que cubren los fixtures.
    """
    nested = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://xhamster.invalid/videos/x-1234567' />"
        "</head><body>"
        + _initials_script(
            {"xplayerPluginSettings": {"spriteLoader": {"template": FIXTURE_SPRITE_TEMPLATE}}}
        )
        + "</body></html>"
    )
    video = parse_video_page(nested, page_url="https://xhamster.invalid/videos/x-1234567")
    assert video.storyboard_urls == [FIXTURE_SPRITE_TEMPLATE]

    # Fallback `videoModel.spriteURL` (mismo valor en la captura real).
    via_model = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://xhamster.invalid/videos/x-1234568' />"
        "</head><body>"
        + _initials_script({"videoModel": {"spriteURL": FIXTURE_SPRITE_TEMPLATE}})
        + "</body></html>"
    )
    video = parse_video_page(via_model, page_url="https://xhamster.invalid/videos/x-1234568")
    assert video.storyboard_urls == [FIXTURE_SPRITE_TEMPLATE]


def test_parse_video_page_sin_patron_de_video_error_claro() -> None:
    """Regresión de estructura: sin patrón `/videos/<slug>-<id>` → error claro."""
    html = (
        "<html><head>"
        "<meta property='og:title' content='Algo' />"
        "<meta property='og:url' content='https://xhamster.invalid/random' />"
        "</head><body></body></html>"
    )
    with pytest.raises(XhamsterParseError, match="patrón de vídeo"):
        parse_video_page(html, page_url="https://xhamster.invalid/random2")


def test_parse_video_page_sin_senales_de_video_error_claro() -> None:
    """SEC-001: página sin og:* ni videoModel (p. ej. captcha/anti-bot) → error claro."""
    html = "<html><body><div>otra estructura</div></body></html>"
    with pytest.raises(XhamsterParseError, match="señales de vídeo"):
        parse_video_page(html, page_url="https://xhamster.invalid/videos/x-1234567")


def test_parse_video_page_initials_invalido_no_revienta() -> None:
    """Edge: `window.initials` presente pero JSON inválido → opcionales None."""
    html = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://xhamster.invalid/videos/x-1234569' />"
        "</head><body>"
        "<script id='initials-script'>window.initials={not: valid json}</script>"
        "</body></html>"
    )
    video = parse_video_page(html, page_url="https://xhamster.invalid/videos/x-1234569")
    assert video.external_id == "1234569"
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.storyboard_urls == []
    assert video.tags == []
    assert video.published_at is None


def test_parse_video_page_tags_keywords_mas_de_20_se_recortan() -> None:
    """PR-062: fallback `keywords` → tags, con tope de 20 (máx. ~20)."""
    keywords = ",".join(f"tag-{index:02d}" for index in range(22))
    html = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://xhamster.invalid/videos/x-1234570' />"
        "</head><body>"
        + _initials_script({"videoModel": {"keywords": keywords}})
        + "</body></html>"
    )
    video = parse_video_page(html, page_url="https://xhamster.invalid/videos/x-1234570")
    assert video.tags == [f"tag-{index:02d}" for index in range(20)]
    assert len(video.tags) == 20


def test_parse_video_page_duracion_no_numerica_none() -> None:
    """Edge: `videoModel.duration` no numérica → `duration_ms=None` (degradación)."""
    html = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://xhamster.invalid/videos/x-1234571' />"
        "</head><body>"
        + _initials_script({"videoModel": {"duration": "no-un-numero"}})
        + "</body></html>"
    )
    video = parse_video_page(html, page_url="https://xhamster.invalid/videos/x-1234571")
    assert video.duration_ms is None


def test_parse_video_page_titulo_fallback_video_model() -> None:
    """FR-004: sin og:title, el título cae a `videoModel.title`."""
    html = (
        "<html><head>"
        "<meta property='og:url' content='https://xhamster.invalid/videos/x-1234572' />"
        "</head><body>"
        + _initials_script({"videoModel": {"title": "Titulo de ejemplo 72"}})
        + "</body></html>"
    )
    video = parse_video_page(html, page_url="https://xhamster.invalid/videos/x-1234572")
    assert video.title == "Titulo de ejemplo 72"


def test_parse_video_page_preview_url_siempre_none() -> None:
    """D3 · SC-004: `preview_url` queda None aunque la página exponga mp4.

    El fixture completo expone `trailerURL` (mp4) y `data-previewvideo` (mp4)
    de relacionados: ninguno se expone en v1.
    """
    video = _full_video()
    assert video.preview_url is None
    assert all(not url.endswith(".mp4") for url in video.storyboard_urls)


# ---------------------------------------------------------------------------
# FR-003 · Parseo de la página de listado (IDs + cursor + anti-bucle)
# ---------------------------------------------------------------------------


def test_parse_listing_page_ids_dedup_ambas_formas_y_cursor() -> None:
    """FR-003/FR-004: el listado produce IDs únicos (dedup) y el cursor.

    El thumb 3000001 tiene dos enlaces (imagen + overlay): se deduplica. Cubre
    ambas formas de id: numérica (`3000001`, `3000003`) y alfanumérica
    (`xhT0001`). Cursor = path del enlace siguiente al activo (activo=1 →
    `/categories/amateur/2`).
    """
    page = parse_listing_page(_fixture("category_page_1.html"))
    assert page.external_ids == ["3000001", "xhT0001", "3000003"]
    assert page.next_cursor == "/categories/amateur/2"


def test_parse_listing_page_page_urls_con_href_completo() -> None:
    """PR-045 (paridad): `page_urls` guarda el **href completo** de cada vídeo."""
    page = parse_listing_page(_fixture("category_page_1.html"))
    assert page.page_urls == {
        "3000001": "/videos/titulo-de-ejemplo-1-3000001",
        "xhT0001": "/videos/titulo-de-ejemplo-2-xhT0001",
        "3000003": "/videos/titulo-de-ejemplo-3-3000003",
    }


def test_parse_listing_page_href_absoluto_de_la_estructura_real() -> None:
    """FR-003: href ABSOLUTO (estructura real) se parsea y se conserva completo.

    La captura real enlaza `https://es.xhamster.com/videos/<slug>-<id>`
    (absoluto): el ID sale del path y `page_urls` conserva el href verbatim
    (el host `es.*` se acepta como objetivo de URL canónica, corrección A1).
    """
    html = (
        "<html><body><div class='video-thumb' data-video-id='s000009'>"
        "<a class='video-thumb__image-container' data-role='thumb-link' "
        "href='https://es.xhamster.com/videos/titulo-de-ejemplo-9-3000009'>v</a>"
        "</div></body></html>"
    )
    page = parse_listing_page(html)
    assert page.external_ids == ["3000009"]
    assert page.page_urls == {
        "3000009": "https://es.xhamster.com/videos/titulo-de-ejemplo-9-3000009"
    }


def test_parse_listing_page_paginacion_absoluta_normalizada_a_path() -> None:
    """FR-003: los hrefs ABSOLUTOS de `page-button-link` se normalizan a path.

    La captura real usa `https://es.xhamster.com/categories/amateur/N`: el
    cursor es el path del enlace siguiente al activo.
    """
    html = (
        "<html><body><div class='video-thumb' data-video-id='s000010'>"
        "<a class='video-thumb__image-container' data-role='thumb-link' "
        "href='/videos/titulo-de-ejemplo-10-3000010'>v</a>"
        "</div>"
        "<ol class='page-list'>"
        "<li class='page-button'><a class='page-button-link page-button-link--active' "
        "href='https://es.xhamster.com/categories/amateur'>1</a></li>"
        "<li class='page-button'><a class='page-button-link' "
        "href='https://es.xhamster.com/categories/amateur/2'>2</a></li>"
        "</ol></body></html>"
    )
    page = parse_listing_page(html, current_path="/categories/amateur")
    assert page.next_cursor == "/categories/amateur/2"


def test_parse_listing_page_paginacion_con_salto_de_numeracion() -> None:
    """FR-003: la numeración SALTA (activo=2 → `/16828`) y el cursor avanza.

    Estructura real: `/2, /3, …, /6` y luego numeración alta (`/16828`,
    `/33654`). El cursor es el href siguiente al activo, sea cual sea la
    numeración.
    """
    page = parse_listing_page(_fixture("category_page_2.html"))
    assert page.external_ids == ["3000004", "xhT0005"]
    assert page.next_cursor == "/categories/amateur/16828"


def test_parse_listing_page_fin_de_paginacion() -> None:
    """FR-003: sin enlace numerado siguiente al activo, el cursor es None."""
    html = (
        "<html><body><div class='video-thumb' data-video-id='s000011'>"
        "<a class='video-thumb__image-container' data-role='thumb-link' "
        "href='/videos/titulo-de-ejemplo-11-3000011'>v</a>"
        "</div>"
        "<ol class='page-list'>"
        "<li class='page-button'><a class='page-button-link page-button-link--active' "
        "href='/categories/amateur/33654'>33654</a></li>"
        "</ol></body></html>"
    )
    page = parse_listing_page(html, current_path="/categories/amateur/33654")
    assert page.next_cursor is None


def test_parse_listing_page_cursor_repite_path_actual_fin() -> None:
    """FR-003 anti-bucle: candidato que repite el path actual → fin (None).

    En la última página real el `page-limit-button` duplica el enlace de la
    última página (mismo path): el candidato se descarta → `next_cursor=None`.
    """
    page = parse_listing_page(_LAST_PAGE_HTML, current_path="/categories/amateur/33654")
    assert page.external_ids == ["3000007"]
    assert page.next_cursor is None
    # Control: con el path actual distinto, el cursor sí avanza.
    page = parse_listing_page(_LAST_PAGE_HTML, current_path="/categories/amateur/16828")
    assert page.next_cursor == "/categories/amateur/33654"


def test_parse_listing_page_sin_activo_cursor_none() -> None:
    """FR-003: paginación sin `page-button-link--active` → cursor None (fail-safe)."""
    html = (
        "<html><body><div class='video-thumb' data-video-id='s000012'>"
        "<a class='video-thumb__image-container' data-role='thumb-link' "
        "href='/videos/titulo-de-ejemplo-12-3000012'>v</a>"
        "</div>"
        "<ol class='page-list'>"
        "<li class='page-button'>"
        "<a class='page-button-link' href='/categories/amateur/2'>2</a>"
        "</li>"
        "</ol></body></html>"
    )
    page = parse_listing_page(html)
    assert page.external_ids == ["3000012"]
    assert page.next_cursor is None


def test_parse_listing_page_estructura_cambiada_devuelve_vacio() -> None:
    """Edge: si el listado cambia de estructura, devuelve vacío sin crashear.

    El fallo queda aislado en el adapter: el llamador ve una página vacía y no
    se corrompe el flujo; los tests de regresión sobre los fixtures señalan el
    cambio de selector.
    """
    page = parse_listing_page("<html><body><div class='otra-estructura'>x</div></body></html>")
    assert page.external_ids == []
    assert page.next_cursor is None


def test_parse_listing_page_ignora_enlaces_fuera_de_videos() -> None:
    """FR-003: solo cuentan los enlaces `/videos/<slug>-<id>` del ítem.

    Enlaces a otros contenidos (`/photos/...`), sin `data-role="thumb-link"`
    o sin href → ignorados. El selector no fija `[href^="/videos/"]` porque el
    href real es absoluto: el filtro es el patrón del path.
    """
    html = (
        "<html><body><div class='video-thumb' data-video-id='s000013'>"
        "<a class='video-thumb__image-container' data-role='thumb-link' "
        "href='/photos/album-de-ejemplo-1'>foto</a>"
        "<a class='video-thumb__image-container' "
        "href='/videos/titulo-de-ejemplo-13-3000013'>sin role</a>"
        "</div>"
        "<a class='video-thumb__image-container' data-role='thumb-link' "
        "href='/videos/titulo-de-ejemplo-fuera-3000014'>fuera del ítem</a>"
        "</body></html>"
    )
    page = parse_listing_page(html)
    assert page.external_ids == []


# ---------------------------------------------------------------------------
# ADR-0015 §3 · storyboard_grid (grid resolver del sprite)
# ---------------------------------------------------------------------------


def test_storyboard_grid_template_con_n_explicito() -> None:
    """ADR-0015 §3: `160x160.50.s.jpg` (.<N>.s.) → (50, 1) — 50 tiles de 160×131."""
    asset = VisualAsset(kind="storyboard", url=FIXTURE_SPRITE_TEMPLATE)
    assert storyboard_grid(asset) == (50, 1)


def test_storyboard_grid_hover_sprite_sin_n() -> None:
    """ADR-0015 §3: `526x298.s.webp` sin N → (20, 1) — 20 tiles de 263×298.

    El patrón de N exige un `.` ANTES de los dígitos: `526x298.s.webp` no puede
    interpretarse como `<526x29>.<8>.s.webp`.
    """
    hover = "https://thumb-v8.xhcdn.invalid/a/tokenejemplo0002/017/608/858/526x298.s.webp"
    assert storyboard_grid(VisualAsset(kind="storyboard", url=hover)) == (20, 1)


def test_storyboard_grid_urls_ajenas_devuelven_none() -> None:
    """ADR-0015 §3: URLs sin patrón de sprite (thumbnail, mp4, otros) → None."""
    assert storyboard_grid(VisualAsset(kind="thumbnail", url=FIXTURE_THUMBNAIL)) is None
    assert (
        storyboard_grid(
            VisualAsset(
                kind="storyboard",
                url="https://thumb-v7.xhcdn.invalid/a/x/002/533/587/526x298.78.t.mp4",
            )
        )
        is None
    )
    assert (
        storyboard_grid(
            VisualAsset(
                kind="storyboard",
                url="https://thumb-v7.xhcdn.invalid/a/x/002/533/587/526x298.85.3.t.av1.mp4",
            )
        )
        is None
    )
    assert (
        storyboard_grid(
            VisualAsset(kind="storyboard", url="https://cdn.example.invalid/random.jpg")
        )
        is None
    )


# ---------------------------------------------------------------------------
# FR-003 · discover() con transporte mock (sin red) + anti-bucle
# ---------------------------------------------------------------------------


def test_discover_section_obligatoria_sin_section_error() -> None:
    """D2 · FR-003: sin `section` el discover se RECHAZA (fail-fast).

    En v1 no se explora la home: `section=None` → error tipado claro. El
    adapter nunca construye una URL de discover sin sección.
    """

    async def scenario() -> None:
        with pytest.raises(ValueError, match="section"):
            await _adapter().discover(cursor=None, limit=100)

    _run(scenario)


def test_discover_section_sin_barra_inicial_error() -> None:
    """FR-003: una sección sin '/' inicial es error del llamador (ValueError)."""

    async def scenario() -> None:
        with pytest.raises(ValueError, match="empezar por '/'"):
            await _adapter().discover(cursor=None, limit=100, section="categories/amateur")

    _run(scenario)


def test_discover_primera_pagina_ids_y_cursor() -> None:
    """FR-003: discover con sección arranca en `https://xhamster.com<section>`.

    La primera URL pedida es exactamente la de la sección; devuelve los IDs
    deduplicados (ambas formas), el cursor siguiente y `page_urls` con los
    hrefs completos.
    """
    requested: list[str] = []
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal requested, page
        handler = _fixture_handler()
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(_tracking_handler(requested, handler))
        )
        page = await adapter.discover(cursor=None, limit=100, section="/categories/amateur")

    _run(scenario)
    assert requested == ["https://xhamster.com/categories/amateur"]
    assert page.external_ids == ["3000001", "xhT0001", "3000003"]
    assert page.next_cursor == "/categories/amateur/2"
    assert page.page_urls == {
        "3000001": "/videos/titulo-de-ejemplo-1-3000001",
        "xhT0001": "/videos/titulo-de-ejemplo-2-xhT0001",
        "3000003": "/videos/titulo-de-ejemplo-3-3000003",
    }


def test_discover_cadena_de_paginacion_con_salto_hasta_fin() -> None:
    """FR-003: la cadena avanza página a página, con SALTO de numeración, hasta fin.

    Página 1 → `/categories/amateur/2` → salto a `/categories/amateur/16828`
    → `/categories/amateur/33654` → fin (`None`): el cursor avanza por el href
    siguiente al activo sea cual sea la numeración (estructura real).
    """
    pages: list[DiscoverPage] = []

    async def scenario() -> None:
        nonlocal pages
        adapter = _adapter()
        pages.append(await adapter.discover(cursor=None, limit=100, section="/categories/amateur"))
        pages.append(
            await adapter.discover(
                cursor=pages[0].next_cursor, limit=100, section="/categories/amateur"
            )
        )
        pages.append(
            await adapter.discover(
                cursor=pages[1].next_cursor, limit=100, section="/categories/amateur"
            )
        )
        pages.append(
            await adapter.discover(
                cursor=pages[2].next_cursor, limit=100, section="/categories/amateur"
            )
        )

    _run(scenario)
    assert pages[0].external_ids == ["3000001", "xhT0001", "3000003"]
    assert pages[0].next_cursor == "/categories/amateur/2"
    assert pages[1].external_ids == ["3000004", "xhT0005"]
    assert pages[1].next_cursor == "/categories/amateur/16828"
    assert pages[2].external_ids == ["3000006"]
    assert pages[2].next_cursor == "/categories/amateur/33654"
    assert pages[3].external_ids == ["3000007"]
    assert pages[3].next_cursor is None


def test_discover_cero_ids_nuevos_anti_bucle_fin() -> None:
    """FR-003 anti-bucle: página con 0 IDs NUEVOS (no vistos) → `next_cursor=None`.

    La misma protección que xvideos (PR-043): una página que solo repite IDs
    ya vistos por esta instancia termina la cadena de paginación.
    """
    first: DiscoverPage
    repeated: DiscoverPage

    async def scenario() -> None:
        nonlocal first, repeated
        adapter = _adapter()
        first = await adapter.discover(cursor=None, limit=100, section="/categories/amateur")
        repeated = await adapter.discover(
            cursor="/categories/amateur/99", limit=100, section="/categories/amateur"
        )

    _run(scenario)
    assert first.external_ids == ["3000001", "xhT0001", "3000003"]
    assert repeated.external_ids == first.external_ids  # la página se devuelve…
    assert repeated.next_cursor is None  # …pero la cadena termina (0 nuevos)


def test_discover_limit_menor_que_tamano_de_pagina_lanza_error() -> None:
    """FR-003: página con más IDs que `limit` → `XhamsterParseError`, sin truncar.

    Truncar no está soportado: el adapter falla con mensaje que informa de los
    tamaños reales para que el llamador ajuste `limit` (el backfill real usa
    `--limit 64` ≥ página real de 46–51 ítems, corrección A2 de la spec).
    """

    async def scenario() -> None:
        with pytest.raises(XhamsterParseError, match="3 IDs con limit=2"):
            await _adapter().discover(cursor=None, limit=2, section="/categories/amateur")

    _run(scenario)


def test_discover_limit_igual_al_tamano_de_pagina_devuelve_todo() -> None:
    """FR-003: borde del contrato — `limit` == tamaño de página no lanza error."""

    async def scenario() -> None:
        page = await _adapter().discover(cursor=None, limit=3, section="/categories/amateur")
        assert page.external_ids == ["3000001", "xhT0001", "3000003"]
        assert page.next_cursor == "/categories/amateur/2"

    _run(scenario)


def test_discover_error_http_se_propaga() -> None:
    """Edge: un 500 del sitio se propaga (HTTPStatusError) para que la capa de jobs reintente."""

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await _adapter().discover(
                cursor="/ruta-desconocida", limit=10, section="/categories/amateur"
            )

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-004 · get_video() con transporte mock (sin red)
# ---------------------------------------------------------------------------


def test_get_video_metadatos_completos() -> None:
    """FR-004: get_video devuelve el VideoSource normalizado del fixture completo.

    Además verifica SC-004: el mp4 existe en la página del fixture
    (`trailerURL`/`data-previewvideo`) pero `preview_url` queda `None`
    (prohibido exponerlo en v1, D3).
    """
    video: VideoSource | None = None

    async def scenario() -> None:
        nonlocal video
        video = await _adapter().get_video("3000001")

    _run(scenario)
    assert video is not None
    assert video.external_id == "3000001"
    assert video.title == "Titulo de ejemplo 1"
    assert video.duration_ms == 234_000
    assert video.storyboard_urls == [FIXTURE_SPRITE_TEMPLATE]
    assert video.preview_url is None  # D3 · SC-004: mp4 nunca expuesto


def test_get_video_404_devuelve_none() -> None:
    """Spec edge case: vídeo retirado (404) → None (sin reintentos infinitos)."""
    video: VideoSource | None

    async def scenario() -> None:
        nonlocal video
        video = await _adapter().get_video("9000000")

    _run(scenario)
    assert video is None


def test_get_video_estructura_cambiada_levanta_error() -> None:
    """Edge: HTML sin señales de vídeo → XhamsterParseError (el job queda failed)."""

    async def scenario() -> None:
        with pytest.raises(XhamsterParseError):
            await _adapter().get_video("9000001")

    _run(scenario)


def test_get_video_con_page_url_usa_la_url_completa_del_listado() -> None:
    """PR-045 (paridad): con `page_url`, get_video pide EXACTAMENTE la URL completa.

    El href del listado (`/videos/<slug>-<id>`) se resuelve contra el host
    canónico y se usa tal cual — la URL que la fuente acepta.
    """
    requested: list[str] = []

    async def scenario() -> None:
        nonlocal requested
        handler = _fixture_handler()
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(_tracking_handler(requested, handler))
        )
        video = await adapter.get_video("3000001", page_url="/videos/titulo-de-ejemplo-1-3000001")
        assert video is not None
        assert video.external_id == "3000001"

    _run(scenario)
    assert requested == ["https://xhamster.com/videos/titulo-de-ejemplo-1-3000001"]


def test_get_video_sin_page_url_fallback_a_la_plantilla() -> None:
    """PR-045 (paridad): sin `page_url` (None), get_video usa la plantilla.

    Retrocompatibilidad: los llamadores que no disponen del href del listado
    (p. ej. FETCH_METADATA) reconstruyen `https://xhamster.com/videos/x-<id>`.
    """
    requested: list[str] = []

    async def scenario() -> None:
        nonlocal requested
        handler = _fixture_handler()
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(_tracking_handler(requested, handler))
        )
        video = await adapter.get_video("3000001", page_url=None)
        assert video is not None
        assert video.external_id == "3000001"

    _run(scenario)
    assert requested == ["https://xhamster.com/videos/x-3000001"]


def test_get_video_page_url_absoluta_es_aceptada() -> None:
    """SEC-003 · corrección A1: `page_url` absoluta en `es.xhamster.com` se usa.

    Con IP española la URL canónica puede servirse en `es.*`: se acepta como
    objetivo de URL canónica (no como base del discover).
    """
    requested: list[str] = []
    page_url = "https://es.xhamster.com/videos/titulo-de-ejemplo-1-3000001"

    async def scenario() -> None:
        nonlocal requested
        handler = _fixture_handler()
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(_tracking_handler(requested, handler))
        )
        video = await adapter.get_video("3000001", page_url=page_url)
        assert video is not None

    _run(scenario)
    assert requested == [page_url]


def test_get_video_page_url_ajena_al_host_no_se_usa() -> None:
    """PR-045 · SEC-001: un `page_url` fuera de la allowlist NO se usa (fallback).

    Solo se aceptan paths relativos (`/videos/...`) o URLs http(s) de
    `xhamster.com`/`www.xhamster.com`/`es.xhamster.com`: cualquier otro valor
    (host ajeno) cae a la plantilla — el adapter nunca pide URLs fuera de su
    dominio (el `SafeHTTPClient` con allowlist sería la segunda barrera).
    """
    requested: list[str] = []

    async def scenario() -> None:
        nonlocal requested
        handler = _fixture_handler()
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(_tracking_handler(requested, handler))
        )
        video = await adapter.get_video(
            "3000001", page_url="https://evil.invalid/videos/titulo-de-ejemplo-1-3000001"
        )
        assert video is not None

    _run(scenario)
    assert requested == ["https://xhamster.com/videos/x-3000001"]


# ---------------------------------------------------------------------------
# FR-005/SC-004 · get_visual_assets (sprite + thumbnail; nunca mp4)
# ---------------------------------------------------------------------------


def test_get_visual_assets_sprite_y_thumbnail() -> None:
    """FR-005: UN storyboard (template del player, sin position/timestamp) + UN thumbnail.

    El sprite del vídeo principal ya viaja en `video.storyboard_urls[0]` (del
    `spriteLoader.template`): `get_visual_assets` no re-fetcha la página y
    emite los dos assets declarados en el manifest.
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        adapter = _adapter()
        video = await adapter.get_video("3000001")
        assert video is not None
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert assets == [
        VisualAsset(
            kind="storyboard",
            url=FIXTURE_SPRITE_TEMPLATE,
            position=None,
            timestamp_ms=None,
        ),
        VisualAsset(kind="thumbnail", url=FIXTURE_THUMBNAIL),
    ]
    # SC-004: ningún asset es mp4 ni kind "preview".
    assert all(asset.kind in ("storyboard", "thumbnail") for asset in assets)
    assert all(not asset.url.endswith(".mp4") for asset in assets)


def test_get_visual_assets_sin_sprite_degrada_a_thumbnail_unico() -> None:
    """FR-005: sin sprite (sin `spriteLoader.template`) → solo thumbnail.

    Jerarquía de assets: el vídeo no se pierde; el storyboard se omite.
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        adapter = _adapter()
        video = await adapter.get_video("3000003")
        assert video is not None
        assert video.storyboard_urls == []
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert assets == [
        VisualAsset(
            kind="thumbnail",
            url="https://ic-vt-nss.xhcdn.invalid/a/tokenejemplo0003/002/533/587/1280x720.6.jpg",
        )
    ]


def test_get_visual_assets_video_sin_assets_devuelve_vacio() -> None:
    """Spec edge case: vídeo sin thumbnail ni sprite → lista vacía, sin fallar."""
    video = parse_video_page(
        _fixture("video_page_minimal.html"),
        page_url="https://xhamster.invalid/videos/titulo-de-ejemplo-2-xhT0001",
    )
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        assets = await _adapter().get_visual_assets(video)

    _run(scenario)
    assert assets == []


# ---------------------------------------------------------------------------
# FR-001 · check_availability()
# ---------------------------------------------------------------------------


def test_check_availability_available() -> None:
    """FR-001: página válida → available."""
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(_full_video())

    _run(scenario)
    assert availability == VideoAvailability.AVAILABLE


def test_check_availability_con_page_url_usa_la_url_completa() -> None:
    """PR-047 (paridad): con `page_url` completo, pide la URL canónica con slug.

    Sin el slug, la reconstrucción `/videos/x-<id>` puede no servir la página
    y el 404 se interpretaría como `removed` (falso negativo terminal).
    """
    requested: list[str] = []
    page_url = "https://www.xhamster.com/videos/titulo-de-ejemplo-1-3000001"
    video = _full_video().model_copy(update={"page_url": page_url})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal requested, availability
        handler = _fixture_handler()
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(_tracking_handler(requested, handler))
        )
        availability = await adapter.check_availability(video)

    _run(scenario)
    assert requested == [page_url]
    assert availability == VideoAvailability.AVAILABLE


def test_check_availability_404_removed() -> None:
    """Spec edge case: 404 → removed (estado terminal, sin reintentos)."""
    removed = _full_video().model_copy(update={"external_id": "9000000"})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(removed)

    _run(scenario)
    assert availability == VideoAvailability.REMOVED


def test_check_availability_estructura_cambiada_unavailable() -> None:
    """Edge: no se puede confirmar la disponibilidad → unavailable (sin crashear)."""
    changed = _full_video().model_copy(update={"external_id": "9000001"})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(changed)

    _run(scenario)
    assert availability == VideoAvailability.UNAVAILABLE


# ---------------------------------------------------------------------------
# SEC-001/003 · Cliente HTTP seguro (allowlist + anti-DNS-rebinding)
# ---------------------------------------------------------------------------


def test_safe_http_client_allowlist_rechaza_host_ajeno() -> None:
    """SEC-001: la allowlist de página del adapter rechaza hosts ajenos.

    El `SafeHTTPClient` con la allowlist de xhamster (D1 + A1) aborta
    cualquier petición fuera de `xhamster.com`/`www.xhamster.com`/
    `es.xhamster.com` con `HostNotAllowedError` (fail-closed, sin red).
    """

    async def scenario() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        async with SafeHTTPClient(allowed_hosts=XH_VIDEO_HOSTS, transport=transport) as client:
            with pytest.raises(HostNotAllowedError):
                await client.get("https://evil.invalid/videos/x-1")

    _run(scenario)


def test_xhamster_activa_anti_dns_rebinding_sin_red_real() -> None:
    """SEC-003: el adapter valida IPs resueltas antes de usar el mock."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return _fixture_handler()(request)

    def private_resolver(host: str) -> list[str]:
        assert host == "xhamster.com"
        return ["192.168.1.10"]

    async def scenario() -> None:
        adapter = XhamsterAdapter(
            transport=httpx.MockTransport(handler),
            resolver=private_resolver,
        )
        with pytest.raises(PrivateIPError):
            await adapter.discover(cursor=None, limit=100, section="/categories/amateur")

    _run(scenario)
    assert requested == []


# ---------------------------------------------------------------------------
# SEC-004 · Fixtures sintéticos (sin contenido real en el repo)
# ---------------------------------------------------------------------------


def test_fixtures_no_usan_dominio_real_xhamster_com() -> None:
    """SEC-004: los datos de los fixtures son sintéticos; solo `xhamster.invalid`.

    Se escanea el HTML de los fixtures (los datos): el dominio real
    `xhamster.com` queda prohibido; el README.md es documentación y puede
    mencionar la política (no es contenido de la fuente).
    """
    for path in sorted(FIXTURES_DIR.glob("*.html")):
        content = path.read_text(encoding="utf-8")
        assert "xhamster.com" not in content, (
            f"SEC-004 violado: {path.name} contiene el dominio real xhamster.com"
        )
        assert "xhamster.invalid" in content or "xhcdn.invalid" in content, (
            f"SEC-004 violado: {path.name} no usa dominios reservados .invalid"
        )


# ---------------------------------------------------------------------------
# SC-007 · Añadir esta fuente no toca el core
# ---------------------------------------------------------------------------


def _imported_module_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_core_no_importa_el_adapter_xhamster() -> None:
    """SC-007: ningún módulo del core importa el adapter xhamster.

    Añadir esta fuente solo añade ficheros del adapter (y su registro futuro);
    el core (xtrace_crawler fuera de `adapters/`) no conoce xhamster. Si un
    selector o estructura cambia, el fallo queda contenido en el adapter.
    """
    package_root = Path(_crawler_pkg_init).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if "adapters" in path.parts:
            continue
        for module in _imported_module_names(path):
            parts = module.split(".")
            if "xhamster" in parts:
                offenders.append(f"{path.relative_to(package_root)} importa {module}")
    assert offenders == [], f"SC-007 violado: el core importa el adapter xhamster: {offenders}"
