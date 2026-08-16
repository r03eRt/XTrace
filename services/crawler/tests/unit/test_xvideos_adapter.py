"""Tests del adapter xvideos + fixtures sintéticos de la estructura OBSERVADA
(PR-031/PR-043 · FR-004 · SEC-001/002/004 · SC-006/007 · ADR-0009).

Trazabilidad (constitución §3): cada test marca el requisito que valida:

- FR-004: método de acceso "html" documentado en el manifest; `discover()` con
  cursor de paginación y **protección anti-bucle** (hallazgo de la validación
  real PR-033: 0 IDs nuevos → fin; cursor repetido → fin; cursor desde la URL
  FINAL de la respuesta tras redirects) y `get_video()` sobre HTML de xvideos.
- PR-044: la HOME real NO usa `a.thumb-link` — los enlaces viven en
  `div.thumb > a[href^="/video."]` **sin clase** y la home NO tiene paginación
  `a.dir.next` (hallazgo de la 2a validación real, 2026-08-16); el selector
  ampliado cubre la home y `/best/...` (donde sí existe `a.thumb-link`).
- PR-047: `get_visual_assets` y `check_availability` usan `video.page_url`
  (URL canónica con slug) para sus peticiones — hallazgo de la 5a validación
  real (2026-08-16): sin el slug, la reconstrucción `/video.<id>/` devuelve
  404 en INDEX_VIDEO y `removed` falso en CHECK_AVAILABILITY; fallback a la
  plantilla solo con `page_url` vacío.
- PR-049: `discover` con `section` (ruta de categoría/tag, p. ej. `/tags/x`)
  arranca en `https://www.xvideos.com<section>` en vez de la home; con cursor,
  la URL sale del cursor (la sección solo fija el arranque) y la paginación
  sigue a `a.dir.next` igual que siempre; una sección sin '/' inicial es error
  del llamador.
- PR-052: los TAGS NO usan `a.dir.next` (hallazgo de la prueba del tag
  `/tags/buttfucking`, 7a validación real, 2026-08-16): pagan con una LISTA
  NUMERADA `<div class="pagination "><ul><li><a class="active" href="">1</a>
  </li><li><a href="/tags/xxx/1">2</a></li>…</ul></div>` — la página 1 es la
  URL base (`/tags/xxx`) y la página N+1 es `/tags/xxx/N` (numeración
  0-indexada en la URL). El cursor es el href del LI siguiente al `a.active`
  (el enlace "Next" `no-page next-page` no cuenta); activo al final →
  `next_cursor=None`. `a.dir.next`, cuando existe, manda (prioridad).
- PR-053 (8a validación real · hallazgo de la validación de capturas del
  operador, 2026-08-16): los `timestamp_ms` de la galería usaban como
  denominador el **número de assets conservados** (`total + 1`) y, con
  posiciones dispersas (galería real hasta `xv_30_t.jpg` con pocos thumbs en
  el markup), excedían la duración hasta 5x (p. ej. ts=2.400.000ms en un
  vídeo de 480.000ms). La galería está distribuida uniformemente sobre el
  vídeo: `timestamp_ms = round(N/K*duration_ms)` con `K = MÁXIMA posición
  observada`, **clamp** a `[0, duration_ms]` (N==K → duración exacta) y
  `None` defensivo (sin duración o K<=0).
- FR-002 (soporte): `VideoSource` normalizado poblado desde el parseo
  (og:*, JSON-LD).
- FR-005/SC-006: `get_visual_assets` devuelve la galería de thumbnails
  `xv_<N>_t.jpg` (nunca el vídeo completo); el manifest declara exactamente
  `["thumbnail"]` (sin sprite real detectado).
- SEC-001: el adapter solo usa el cliente HTTP seguro (allowlist
  xvideos.com/www.xvideos.com); ningún test toca la red (`httpx.MockTransport`).
- SEC-002: el manifest está **revisado por el operador** (`robots_reviewed=True`,
  `terms_reviewed=True`, `review_date="2026-08-16"`, aprobación en modo prueba,
  PR-042) — la habilitación **efectiva** sigue exigiendo el gate del registry
  (PR-028): manifest conforme Y `sources.enabled=true` en BD (SEC-002).
- SEC-004: los fixtures son sintéticos (dominio `xvideos.invalid`, títulos
  anonimizados "Titulo de ejemplo N", IDs `video.synth000NN`; ningún
  `xvideos.com` real en los fixtures).
- SC-007: el core no importa el adapter (añadir esta fuente no toca el core).

Estructura HTML observada: ver `tests/fixtures/xvideos/README.md` (validación
real 2026-08-16; la estructura asumida de PR-031 quedó descartada). Los
fixtures congelan la estructura observada y los tests fallan con mensaje claro
si un selector clave cambia (regresión).
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
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
from xtrace_crawler.adapters.registry import AdapterNotEnabledError, AdapterRegistry
from xtrace_crawler.adapters.xvideos import (
    XvideosAdapter,
    XvideosParseError,
    parse_listing_page,
    parse_video_page,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "xvideos"

#: Thumb CDN sintético de los fixtures (estructura real → host .invalid, SEC-004).
FIXTURE_THUMB_BASE = "https://thumb-cdn77.xvideos.invalid/11111111-2222-4333-8444-555555555555/3"

#: Página de vídeo con `og:image` que NO sigue el patrón de galería
#: (`mozaique_listing.jpg`, nombre observado en la captura real — no es un
#: `xv_<N>_t.jpg`): el adapter degrada a la miniatura única (jerarquía de
#: assets, FR-005).
_FALLBACK_VIDEO_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta property="og:title" content="Titulo de ejemplo 7" />
  <meta property="og:url"
        content="https://www.xvideos.invalid/video.synth00007/titulo-de-ejemplo-7" />
  <meta property="og:image" content="{FIXTURE_THUMB_BASE}/mozaique_listing.jpg" />
</head>
<body>
  <h2 class="page-title">Titulo de ejemplo 7</h2>
</body>
</html>"""

#: Página de listado cuyo `a.dir.next` apunta a su propio path (anti-bucle).
_LOOP_CURSOR_HTML = (
    "<html><body>"
    "<div class='thumb'>"
    "<a class='thumb-link' href='/video.synth00006/titulo-de-ejemplo-6'>v</a>"
    "</div>"
    "<a href='/best/2026-07/3' class='dir next'>Next</a>"
    "</body></html>"
)

#: Página de vídeo del `video.synth00013` (PR-045): se sirve en la ruta
#: `/video.synth00013/...` del transporte mock — la URL COMPLETA con slug que
#: `get_video(page_url=...)` debe pedir (sintética, SEC-004).
_SYNTH13_VIDEO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta property="og:title" content="Titulo de ejemplo 13" />
  <meta property="og:url"
        content="https://www.xvideos.invalid/video.synth00013/titulo-de-ejemplo-13" />
</head>
<body>
  <h2 class="page-title">Titulo de ejemplo 13</h2>
</body>
</html>"""


def _fixture(name: str) -> str:
    """Lee un fixture sintético de `tests/fixtures/xvideos/` (SEC-004)."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _run(coro: Callable[[], object]) -> None:
    """Ejecuta el escenario async sin dependencia de pytest-asyncio (determinista)."""
    asyncio.run(coro())


def _fixture_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Transporte mock: sirve los fixtures por path, sin red (NFR-003).

    - `/video.synth00001/...` → `video_page_full.html` (galería xv_1..xv_6)
    - `/video.synth00002/...` → `video_page_minimal.html` (opcionales None)
    - `/video.synth00007/...` → `_FALLBACK_VIDEO_HTML` (og:image sin galería)
    - `/video.synth00013/...` → `_SYNTH13_VIDEO_HTML` (PR-045: URL completa con slug)
    - `/video.synth00021/...` → `video_page_sparse_gallery.html`
      (PR-053: galería dispersa xv_3/12/15/28/30_t.jpg, K=30)
    - `/video.synth50000/...` → 200 con HTML sin estructura de vídeo (estructura cambiada)
    - `/video.synth99999/...` → 404 (vídeo retirado)
    - `/` → `listing_page_1.html` (ids 1..3, `dir.next` → `/best/2026-07/1`)
    - `/home` → `home_page.html` (HOME real: `div.thumb` SIN `a.thumb-link`,
      sin paginación → ids 10..12 y fin)
    - `/best/1` → 302 → `/best/2026-07` (redirect canónico del cursor)
    - `/best/2026-07` → `listing_page_1.html` (URL FINAL tras el redirect)
    - `/best/2026-07/1` → `listing_page_2.html` (id 4, `dir.next` → `/best/2026-07/2`)
    - `/best/2026-07/2` → `listing_page_3.html` (id 5, sin `dir.next` → fin)
    - `/best/2026-07/3` → `_LOOP_CURSOR_HTML` (dir.next = path actual)
    - `/best/2026-07/99` → `listing_page_1.html` de nuevo (0 IDs nuevos → fin)
    - `/tags/xxx` → `listing_tag_page_1.html` (PR-052: lista numerada, activo
      =1 → cursor `/tags/xxx/1`)
    - `/tags/xxx/1` → `listing_tag_page_2.html` (activo=2 → `/tags/xxx/2`)
    - `/tags/xxx/2` → `listing_tag_page_3.html` (activo=3 → `/tags/xxx/3`)
    - `/tags/xxx/3` → `listing_tag_page_4.html` (activo=4, ÚLTIMO → fin)
    - cualquier otro path → 500 (error transitorio del sitio)
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: bytes
        if path.startswith("/video.synth00001"):
            body = _fixture("video_page_full.html").encode("utf-8")
        elif path.startswith("/video.synth00002"):
            body = _fixture("video_page_minimal.html").encode("utf-8")
        elif path.startswith("/video.synth00007"):
            body = _FALLBACK_VIDEO_HTML.encode("utf-8")
        elif path.startswith("/video.synth00013"):
            body = _SYNTH13_VIDEO_HTML.encode("utf-8")
        elif path.startswith("/video.synth00021"):
            body = _fixture("video_page_sparse_gallery.html").encode("utf-8")
        elif path.startswith("/video.synth50000"):
            body = b"<html><body>captcha o estructura totalmente distinta</body></html>"
        elif path.startswith("/video.synth99999"):
            return httpx.Response(404, content=b"", request=request)
        elif path == "/":
            body = _fixture("listing_page_1.html").encode("utf-8")
        elif path == "/home":
            body = _fixture("home_page.html").encode("utf-8")
        elif path == "/best/1":
            return httpx.Response(302, headers={"location": "/best/2026-07"}, request=request)
        elif path == "/best/2026-07":
            body = _fixture("listing_page_1.html").encode("utf-8")
        elif path == "/best/2026-07/1":
            body = _fixture("listing_page_2.html").encode("utf-8")
        elif path == "/best/2026-07/2":
            body = _fixture("listing_page_3.html").encode("utf-8")
        elif path == "/best/2026-07/3":
            body = _LOOP_CURSOR_HTML.encode("utf-8")
        elif path == "/best/2026-07/99":
            body = _fixture("listing_page_1.html").encode("utf-8")
        elif path == "/tags/xxx":
            body = _fixture("listing_tag_page_1.html").encode("utf-8")
        elif path == "/tags/xxx/1":
            body = _fixture("listing_tag_page_2.html").encode("utf-8")
        elif path == "/tags/xxx/2":
            body = _fixture("listing_tag_page_3.html").encode("utf-8")
        elif path == "/tags/xxx/3":
            body = _fixture("listing_tag_page_4.html").encode("utf-8")
        else:
            return httpx.Response(500, content=b"boom", request=request)
        return httpx.Response(200, content=body, request=request)

    return handler


def _adapter() -> XvideosAdapter:
    """Adapter con transporte mock: ningún test toca la red (NFR-003, SEC-001)."""
    return XvideosAdapter(transport=httpx.MockTransport(_fixture_handler()))


# ---------------------------------------------------------------------------
# SEC-002 · Manifest revisado por el operador (2026-08-16) + gate del registry
# ---------------------------------------------------------------------------


def test_manifest_revisado_por_el_operador_y_gate_del_registry() -> None:
    """SEC-002: el manifest está revisado (aprobación del operador, 2026-08-16).

    `robots_reviewed=True`, `terms_reviewed=True` y `review_date="2026-08-16"`
    (aprobación explícita del operador, modo prueba, PR-042): el manifest YA
    no es la condición que bloquea la habilitación. La habilitación **efectiva**
    sigue dependiendo del gate del registry (PR-028): manifest conforme Y
    `sources.enabled=true` en BD — sin `enabled`, el registry rechaza la fuente
    con `AdapterNotEnabledError` (razón única: `sources.enabled=false`); con
    `enabled=true`, la resuelve. El rate limit es conservador (FR-009, D5).
    """
    manifest = XvideosAdapter.manifest
    assert manifest.source == "xvideos"
    assert manifest.access_method == "html"  # FR-004: jerarquía documentada
    assert manifest.assets_accessed == ["thumbnail"]  # SC-006/PR-043: sin sprite real
    assert manifest.robots_reviewed is True
    assert manifest.terms_reviewed is True
    assert manifest.review_date == "2026-08-16"
    assert manifest.rate_limit.min_interval_ms == 2_000
    assert manifest.rate_limit.max_rps == 0.5

    # Gate SEC-002 (PR-028 · contracts §1): la aprobación humana final vive en
    # `sources.enabled`; el manifest revisado por sí solo no habilita.
    registry = AdapterRegistry()
    registry.register(_adapter(), real=True)
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("xvideos", enabled_in_db=False)
    assert any("sources.enabled=false" in reason for reason in excinfo.value.reasons)
    assert registry.get_enabled("xvideos", enabled_in_db=True).manifest.source == "xvideos"


def test_adapter_satisface_protocolo_source_adapter() -> None:
    """FR-001: XvideosAdapter cumple estructuralmente el protocolo SourceAdapter."""
    adapter = _adapter()
    # Chequeo estático del protocolo (mypy valida la firma en compilación).
    protocol_adapter: SourceAdapter = adapter
    assert isinstance(protocol_adapter, SourceAdapter)


def test_asset_hosts_observados_y_sin_hosts_inventados() -> None:
    """PR-043/051 · SEC-001 · contracts §1: `asset_hosts` = hosts OBSERVADOS, solo hosts.

    La allowlist se actualizó a los hosts de la validación real de 2026-08-16
    (PR-033): `thumb-cdn77.xvideos-cdn.com` (CDN de thumbnails/galería) y
    `assets-cdn77.xvideos-cdn.com` (CDN de assets del reproductor), además de
    los dominios de página. **PR-051 (1a ejecución real con SigLIP, tag
    /tags/buttfucking)**: los thumbs de algunos vídeos viven en
    `thumbs-gcore.xvideos-cdn.com` (misma familia xvideos-cdn.com) y, al no
    estar en la lista, **todos los assets degradaban (frames=0)** → se añade a
    la allowlist (regresión del hallazgo). Los hosts inventados de la
    estructura asumida de PR-031 quedaron **fuera** (`thumbs2.xvideos.com`,
    `cdn77.io`, patrones `th-01`/`vd-01`). Sigue marcada **PROVISIONAL**
    (validar en backfills reales). Solo hosts: sin esquemas, rutas, query ni
    fragmentos (shape de la lista intacta con el host nuevo).
    """
    hosts = XvideosAdapter.asset_hosts
    assert hosts  # PROVISIONAL — no vacía
    assert "thumb-cdn77.xvideos-cdn.com" in hosts  # observado (PR-033)
    assert "assets-cdn77.xvideos-cdn.com" in hosts  # observado (PR-033)
    assert "thumbs-gcore.xvideos-cdn.com" in hosts  # observado (PR-051, SigLIP real)
    assert "www.xvideos.com" in hosts
    for invented in (
        "thumbs2.xvideos.com",
        "cdn77.io",
        "th-01.xvideos.com",
        "vd-01.xvideos.com",
    ):
        assert invented not in hosts, f"host inventado en asset_hosts: {invented!r}"
    for host in hosts:
        assert "://" not in host, f"esquema en asset_host: {host!r}"
        assert "/" not in host, f"ruta en asset_host: {host!r}"
        assert "?" not in host and "#" not in host, f"query/fragmento en asset_host: {host!r}"
        assert host.strip() == host, f"espacios en asset_host: {host!r}"


# ---------------------------------------------------------------------------
# FR-002/FR-004 · Parseo de la página de vídeo (estructura real: og:* + JSON-LD)
# ---------------------------------------------------------------------------


def test_parse_video_page_full_fixture_metadatos_completos() -> None:
    """FR-004/FR-002: el fixture completo produce VideoSource con todos los campos.

    title/duration_ms/thumbnail_url de og:*; published_at (tz-aware) y tags
    del JSON-LD; page_url = og:url; **preview_url=None** (mp4 completo
    prohibido, SC-006) y **storyboard_urls=[]** (sin sprite real, PR-043).
    """
    video = parse_video_page(
        _fixture("video_page_full.html"),
        page_url="https://www.xvideos.invalid/video.synth00001/",
    )
    assert video.source == "xvideos"
    assert video.external_id == "video.synth00001"
    assert video.title == "Titulo de ejemplo 1"
    assert video.duration_ms == 881_000  # og:duration "881" segundos → ms
    assert video.thumbnail_url == f"{FIXTURE_THUMB_BASE}/xv_1_t.jpg"
    assert video.preview_url is None  # SC-006: setVideoUrlLow presente pero PROHIBIDO
    assert video.storyboard_urls == []  # sin sprite real detectado (PR-043)
    assert video.tags == ["tag de ejemplo uno", "tag de ejemplo dos"]
    assert video.published_at == datetime(2026, 6, 15, 12, 30, tzinfo=UTC)
    assert video.page_url == "https://www.xvideos.invalid/video.synth00001/titulo-de-ejemplo-1"


def test_parse_video_page_minimal_fixture_opcionales_none() -> None:
    """Spec edge case: sin JSON-LD ni duración, los campos opcionales son None/[].

    El vídeo sigue procesándose (metadatos incompletos no bloquean).
    """
    video = parse_video_page(
        _fixture("video_page_minimal.html"),
        page_url="https://www.xvideos.invalid/video.synth00002/",
    )
    assert video.external_id == "video.synth00002"
    assert video.title == "Titulo de ejemplo 2"
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.preview_url is None
    assert video.storyboard_urls == []
    assert video.tags == []
    assert video.published_at is None
    assert video.page_url == "https://www.xvideos.invalid/video.synth00002/titulo-de-ejemplo-2"


def test_parse_video_page_sin_patron_de_video_error_claro() -> None:
    """Regresión de estructura: sin og:url ni patrón `/video.<encoded>/` → error claro.

    Si xvideos cambia un selector clave, el parseo falla con mensaje que
    identifica el patrón esperado en lugar de devolver datos basura (edge
    case "HTML cambia sin aviso" de la spec).
    """
    html = "<html><body><h2 class='page-title'>Algo</h2></body></html>"
    with pytest.raises(XvideosParseError, match="patrón de vídeo"):
        parse_video_page(html, page_url="https://www.xvideos.invalid/random")


def test_parse_video_page_sin_senales_de_video_error_claro() -> None:
    """SEC-001: página sin og:* ni h2 (p. ej. captcha/anti-bot) → error claro."""
    html = "<html><body><div>otra estructura</div></body></html>"
    with pytest.raises(XvideosParseError, match="señales de vídeo"):
        parse_video_page(html, page_url="https://www.xvideos.invalid/video.synth00003/")


def test_parse_video_page_jsonld_invalido_no_revientan() -> None:
    """Edge: JSON-LD presente pero inválido (o duración no numérica) → opcionales None."""
    html = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://www.xvideos.invalid/video.synth00003/x' />"
        "<meta property='og:duration' content='not-a-number' />"
        "</head><body>"
        "<script type='application/ld+json'>{not: valid json}</script>"
        "</body></html>"
    )
    video = parse_video_page(html, page_url="https://www.xvideos.invalid/video.synth00003/")
    assert video.external_id == "video.synth00003"
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.tags == []
    assert video.published_at is None


def test_parse_video_page_uploaddate_sin_offset_se_asume_utc() -> None:
    """PR-043: `uploadDate` ISO sin offset → `published_at` tz-aware (UTC)."""
    html = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://www.xvideos.invalid/video.synth00004/x' />"
        "</head><body>"
        "<script type='application/ld+json'>"
        '{"@type":"VideoObject","uploadDate":"2026-01-02T03:04:05"}'
        "</script></body></html>"
    )
    video = parse_video_page(html, page_url="https://www.xvideos.invalid/video.synth00004/")
    assert video.published_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_parse_video_page_keywords_mas_de_20_se_recortan() -> None:
    """PR-043: `keywords` del JSON-LD → tags, con tope de 20 (máx. ~20)."""
    keywords = [f"tag-{index:02d}" for index in range(22)]
    jsonld = json.dumps({"@type": "VideoObject", "keywords": keywords})
    html = (
        "<html><head>"
        "<meta property='og:title' content='X' />"
        "<meta property='og:url' content='https://www.xvideos.invalid/video.synth00005/x' />"
        "</head><body>"
        f"<script type='application/ld+json'>{jsonld}</script>"
        "</body></html>"
    )
    video = parse_video_page(html, page_url="https://www.xvideos.invalid/video.synth00005/")
    assert video.tags == keywords[:20]
    assert len(video.tags) == 20


def test_parse_video_page_titulo_fallback_h2_sin_span_duracion() -> None:
    """FR-002: sin og:title, el título cae al `h2.page-title` sin el span.duration.

    En la estructura real el h2 incluye `<span class="duration">14 min</span>`
    (paridad con el fixture completo); el fallback debe descartarlo.
    """
    html = (
        "<html><head>"
        "<meta property='og:url' content='https://www.xvideos.invalid/video.synth00006/x' />"
        "</head><body>"
        "<h2 class='page-title'>Titulo de ejemplo 6 <span class='duration'>14 min</span></h2>"
        "</body></html>"
    )
    video = parse_video_page(html, page_url="https://www.xvideos.invalid/video.synth00006/")
    assert video.title == "Titulo de ejemplo 6"


# ---------------------------------------------------------------------------
# FR-004 · Parseo de la página de listado (discover: IDs + cursor + anti-bucle)
# ---------------------------------------------------------------------------


def test_parse_listing_page_ids_y_cursor_con_dedup() -> None:
    """FR-004: el listado produce IDs únicos (dedup) y el cursor de paginación."""
    page = parse_listing_page(_fixture("listing_page_1.html"))
    # El thumb synth00001 tiene dos `a.thumb-link` (imagen + overlay): se deduplica.
    assert page.external_ids == ["video.synth00001", "video.synth00002", "video.synth00003"]
    assert page.next_cursor == "/best/2026-07/1"


def test_parse_listing_page_fin_de_paginacion() -> None:
    """FR-004: sin enlace `dir.next`, el cursor es None (última página)."""
    page = parse_listing_page(_fixture("listing_page_3.html"))
    assert page.external_ids == ["video.synth00005"]
    assert page.next_cursor is None


def test_parse_listing_page_href_absoluto_normalizado_a_path() -> None:
    """FR-004: el enlace `dir.next` absoluto se normaliza a path como cursor."""
    html = (
        "<html><body><div class='thumb'>"
        "<a class='thumb-link' href='/video.synth00009/x'>v</a>"
        "</div>"
        "<a class='dir next' href='https://www.xvideos.invalid/best/3'>Next</a>"
        "</body></html>"
    )
    page = parse_listing_page(html)
    assert page.next_cursor == "/best/3"


def test_parse_listing_page_cursor_repite_path_actual_fin() -> None:
    """PR-043 anti-bucle: `dir.next` == path actual de la respuesta → fin (None)."""
    html = (
        "<html><body><div class='thumb'>"
        "<a class='thumb-link' href='/video.synth00006/x'>v</a>"
        "</div>"
        "<a href='/best/2026-07' class='dir next'>Next</a>"
        "</body></html>"
    )
    page = parse_listing_page(html, current_path="/best/2026-07")
    assert page.external_ids == ["video.synth00006"]
    assert page.next_cursor is None
    # Control: con el path actual distinto, el cursor sí avanza.
    page = parse_listing_page(html, current_path="/best/2026-07/1")
    assert page.next_cursor == "/best/2026-07"


def test_parse_listing_page_tag_paginacion_por_lista_numerada() -> None:
    """PR-052: los TAGS pagan con LISTA NUMERADA — el cursor es el LI siguiente
    al `a.active` (esquema real: página 1 = URL base, página N+1 = /tags/xxx/N).

    Hallazgo de la prueba del tag `/tags/buttfucking` (7a validación real,
    2026-08-16): la página de tag NO tiene `a.dir.next`; el markup real es
    `<div class="pagination "><ul><li><a class="active" href="">1</a></li>
    <li><a href="/tags/xxx/1">2</a></li>…</ul></div>`. El cursor de la página 1
    es el href del LI siguiente al activo: `/tags/xxx/1` (el enlace "Next"
    `no-page next-page`, que va después, no se confunde con un número).
    """
    page = parse_listing_page(_fixture("listing_tag_page_1.html"), current_path="/tags/xxx")
    assert page.external_ids == ["video.synth00014", "video.synth00015", "video.synth00016"]
    assert page.next_cursor == "/tags/xxx/1"


def test_parse_listing_page_tag_paginacion_activo_al_final_fin() -> None:
    """PR-052: última página de un tag — el `a.active` es el último LI numerado
    → `next_cursor=None` (el enlace "Next" no es un número de página)."""
    page = parse_listing_page(_fixture("listing_tag_page_4.html"), current_path="/tags/xxx/3")
    assert page.external_ids == ["video.synth00020"]
    assert page.next_cursor is None


def test_parse_listing_page_dir_next_prioritario_sobre_lista() -> None:
    """PR-052: si existe `a.dir.next`, manda sobre la lista numerada (prioridad).

    `/best` y `/c` también renderizan `div.pagination`, pero con `<a>` planos
    (clase `current`, sin `ul/li`); su `dir.next` sigue siendo la única fuente
    del cursor. Incluso en una página con AMBAS estructuras, `dir.next` gana.
    """
    html = (
        "<html><body><div class='thumb'>"
        "<a href='/video.synth00018/x'>v</a>"
        "</div>"
        "<a href='/best/2026-07/1' class='dir next'>Next</a>"
        "<div class='pagination '><ul>"
        "<li><a class='active' href=''>1</a></li>"
        "<li><a href='/tags/xxx/1'>2</a></li>"
        "</ul></div>"
        "</body></html>"
    )
    page = parse_listing_page(html, current_path="/best/2026-07")
    assert page.next_cursor == "/best/2026-07/1"


def test_parse_listing_page_lista_cursor_repite_path_actual_fin() -> None:
    """PR-052 anti-bucle: un LI siguiente cuyo href repite el path actual → fin.

    La misma protección que para `a.dir.next` (PR-043) aplica al cursor de la
    lista numerada: el candidato que apunta a la página actual no avanza.
    """
    html = (
        "<html><body><div class='thumb'>"
        "<a href='/video.synth00019/x'>v</a>"
        "</div>"
        "<div class='pagination '><ul>"
        "<li><a class='active' href=''>1</a></li>"
        "<li><a href='/tags/xxx'>2</a></li>"
        "</ul></div>"
        "</body></html>"
    )
    page = parse_listing_page(html, current_path="/tags/xxx")
    assert page.next_cursor is None
    # Control: con el path actual distinto, el cursor sí avanza.
    page = parse_listing_page(html, current_path="/tags/xxx/1")
    assert page.next_cursor == "/tags/xxx"


def test_parse_listing_page_estructura_cambiada_devuelve_vacio() -> None:
    """Edge: si el listado cambia de estructura, discover devuelve vacío sin crashear.

    El fallo queda aislado en el adapter (SC-008): el llamador ve una página
    vacía y no se corrompe el flujo; los tests de regresión sobre los fixtures
    señalan el cambio de selector.
    """
    page = parse_listing_page("<html><body><div class='otra-estructura'>x</div></body></html>")
    assert page.external_ids == []
    assert page.next_cursor is None


def test_parse_listing_page_home_sin_clase_thumb_link_ids_y_sin_paginacion() -> None:
    """PR-044: la HOME real NO usa `a.thumb-link` — los enlaces viven en `div.thumb`.

    Hallazgo de la 2a validación real (2026-08-16): en la home los enlaces de
    vídeo son `div.thumb > a[href^="/video."]` **sin clase** (el `a.thumb-link`
    solo existe en `/best/...`), y la home NO tiene paginación `a.dir.next`
    (grid de una sola página). El selector ampliado parsea los IDs (dedup por
    href: el thumb synth00010 repite su enlace) y `next_cursor` queda `None`;
    el enlace del título (`div.thumb-under`, fuera de `div.thumb`) no cuenta
    dos veces.
    """
    page = parse_listing_page(_fixture("home_page.html"))
    assert page.external_ids == [
        "video.synth00010",
        "video.synth00011",
        "video.synth00012",
    ]
    assert page.next_cursor is None


def test_parse_listing_page_page_urls_con_href_completo() -> None:
    """PR-045: `page_urls` guarda el **href completo** de cada vídeo del listado.

    La 3a validación real (2026-08-16) confirmó que la URL canónica de un vídeo
    es `/video.<id>/<num>/<num>/<slug>` (sin el slug → 404): el href del
    listado es la ÚNICA fuente fiable del slug, así que `page_urls` lo conserva
    verbatim por external_id (mismo dedup que los IDs: primer href por vídeo).
    """
    page = parse_listing_page(_fixture("listing_page_1.html"))
    assert page.page_urls == {
        "video.synth00001": "/video.synth00001/titulo-de-ejemplo-1",
        "video.synth00002": "/video.synth00002/titulo-de-ejemplo-2",
        "video.synth00003": "/video.synth00003/titulo-de-ejemplo-3",
    }


def test_parse_listing_page_href_real_con_segmentos_numericos() -> None:
    """PR-045: el href REAL `/video.<id>/<num>/<num>/<slug>` se conserva completo.

    Estructura observada en la 3a validación real: el listado enlaza
    `/video.ID/123/456/slug-titulo` — el ID sale del primer segmento y
    `page_urls` guarda el path íntegro (sin truncar a `/video.<id>/`).
    """
    html = (
        "<html><body><div class='thumb'>"
        "<a href='/video.synth00013/123456/789/titulo-de-ejemplo-13'>v</a>"
        "</div></body></html>"
    )
    page = parse_listing_page(html)
    assert page.external_ids == ["video.synth00013"]
    assert page.page_urls == {
        "video.synth00013": "/video.synth00013/123456/789/titulo-de-ejemplo-13"
    }


def test_discover_home_page_urls_con_hrefs_completos() -> None:
    """PR-045: discover() de la home devuelve `page_urls` con los hrefs completos.

    La home real (2a validación) enlaza `/video.<id>/<slug>` sin clase; el
    adapter rellena `page_urls` igual que en /best — el pipeline puede pasar la
    URL completa a `get_video` sin volver a parsear nada.
    """
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor="/home", limit=100)

    _run(scenario)
    assert page.page_urls == {
        "video.synth00010": "/video.synth00010/titulo-de-ejemplo-10",
        "video.synth00011": "/video.synth00011/titulo-de-ejemplo-11",
        "video.synth00012": "/video.synth00012/titulo-de-ejemplo-12",
    }


# ---------------------------------------------------------------------------
# FR-004 · discover() con transporte mock (sin red) + protección anti-bucle
# ---------------------------------------------------------------------------


def test_discover_primera_pagina_ids_y_cursor() -> None:
    """FR-004: discover sin cursor devuelve los IDs y el cursor siguiente."""
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor=None, limit=100)

    _run(scenario)
    assert page.external_ids == ["video.synth00001", "video.synth00002", "video.synth00003"]
    assert page.next_cursor == "/best/2026-07/1"


def test_discover_home_sin_clase_thumb_link_ids_y_fin_sin_paginacion() -> None:
    """PR-044: discover contra la HOME real (sin `a.thumb-link` ni paginación).

    El flujo completo del hallazgo de la 2a validación real: la home devuelve
    los IDs con el selector ampliado `div.thumb a[href^="/video."]` y
    `next_cursor=None` — no hay `a.dir.next` (una sola página). El anti-bucle
    de 0 IDs nuevos (PR-043) ni siquiera se alcanza: la cadena termina en la
    propia home.
    """
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor="/home", limit=100)

    _run(scenario)
    assert page.external_ids == [
        "video.synth00010",
        "video.synth00011",
        "video.synth00012",
    ]
    assert page.next_cursor is None


def test_discover_cadena_de_paginacion_hasta_fin() -> None:
    """FR-004: la cadena avanza página a página hasta `dir.next` ausente (fin)."""
    pages: list[DiscoverPage] = []

    async def scenario() -> None:
        nonlocal pages
        adapter = _adapter()
        pages.append(await adapter.discover(cursor=None, limit=100))
        pages.append(await adapter.discover(cursor=pages[0].next_cursor, limit=100))
        pages.append(await adapter.discover(cursor=pages[1].next_cursor, limit=100))

    _run(scenario)
    assert pages[0].external_ids == [
        "video.synth00001",
        "video.synth00002",
        "video.synth00003",
    ]
    assert pages[0].next_cursor == "/best/2026-07/1"
    assert pages[1].external_ids == ["video.synth00004"]
    assert pages[1].next_cursor == "/best/2026-07/2"
    assert pages[2].external_ids == ["video.synth00005"]
    assert pages[2].next_cursor is None


def test_discover_tag_paginacion_por_lista_hasta_fin() -> None:
    """PR-052: la cadena de un TAG avanza por la LISTA NUMERADA hasta el final.

    Regresión end-to-end del hallazgo de la prueba del tag `/tags/buttfucking`
    (7a validación real, 2026-08-16): antes de PR-052 el discover solo
    indexaba la página 1 (27 vídeos) porque los tags no usan `a.dir.next`.
    Ahora el cursor sale del LI siguiente al `a.active` de `ul.pagination`
    (esquema: página 1 = `/tags/xxx`, página N+1 = `/tags/xxx/N`) y la cadena
    recorre las 4 páginas del fixture; en la última (activo al final, el
    enlace "Next" `no-page next-page` no cuenta) → `next_cursor=None`.
    """
    pages: list[DiscoverPage] = []

    async def scenario() -> None:
        nonlocal pages
        adapter = _adapter()
        pages.append(await adapter.discover(cursor=None, limit=100, section="/tags/xxx"))
        pages.append(await adapter.discover(cursor=pages[0].next_cursor, limit=100))
        pages.append(await adapter.discover(cursor=pages[1].next_cursor, limit=100))
        pages.append(await adapter.discover(cursor=pages[2].next_cursor, limit=100))

    _run(scenario)
    assert pages[0].external_ids == [
        "video.synth00014",
        "video.synth00015",
        "video.synth00016",
    ]
    assert pages[0].next_cursor == "/tags/xxx/1"
    assert pages[1].external_ids == ["video.synth00017", "video.synth00018"]
    assert pages[1].next_cursor == "/tags/xxx/2"
    assert pages[2].external_ids == ["video.synth00019"]
    assert pages[2].next_cursor == "/tags/xxx/3"
    assert pages[3].external_ids == ["video.synth00020"]
    assert pages[3].next_cursor is None


def test_discover_redirect_canonico_cursor_desde_url_final() -> None:
    """PR-043: `/best/1` redirige a `/best/2026-07`; el cursor sale de la URL FINAL.

    Hallazgo de la validación real: el cursor debe tomarse de la página final
    (`response.url`), no del path pedido — aquí el `dir.next` de la página
    final es `/best/2026-07/1`, que difiere del path actual `/best/2026-07`.
    """
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor="/best/1", limit=100)

    _run(scenario)
    assert page.external_ids == ["video.synth00001", "video.synth00002", "video.synth00003"]
    assert page.next_cursor == "/best/2026-07/1"


def test_discover_cero_ids_nuevos_anti_bucle_fin() -> None:
    """PR-043 anti-bucle: página con 0 IDs NUEVOS (no vistos) → `next_cursor=None`.

    La protección que faltó en el backfill real (192 jobs DISCOVER en bucle):
    una página que solo repite IDs ya vistos por esta instancia termina la
    cadena de paginación (sin encolar la siguiente página).
    """
    first: DiscoverPage
    repeated: DiscoverPage

    async def scenario() -> None:
        nonlocal first, repeated
        adapter = _adapter()
        first = await adapter.discover(cursor=None, limit=100)
        repeated = await adapter.discover(cursor="/best/2026-07/99", limit=100)

    _run(scenario)
    assert first.external_ids == [
        "video.synth00001",
        "video.synth00002",
        "video.synth00003",
    ]
    assert repeated.external_ids == first.external_ids  # la página se devuelve…
    assert repeated.next_cursor is None  # …pero la cadena termina (0 nuevos)


def test_discover_cursor_repite_path_actual_fin() -> None:
    """PR-043 anti-bucle: `dir.next` == path actual → `next_cursor=None` (fin)."""
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor="/best/2026-07/3", limit=100)

    _run(scenario)
    assert page.external_ids == ["video.synth00006"]
    assert page.next_cursor is None


def test_discover_limit_menor_que_tamano_de_pagina_lanza_error() -> None:
    """FR-004: página con más IDs que `limit` → `XvideosParseError`, sin truncar.

    Truncar no está soportado: en vez de repetir el cursor recibido (bucle de
    paginación, en la primera página indistinguible de fin) o descartar IDs en
    silencio (páginas posteriores inalcanzables), `discover` falla con mensaje
    que informa de los tamaños reales para que el llamador ajuste `limit`.
    """

    async def scenario() -> None:
        with pytest.raises(XvideosParseError, match="3 IDs con limit=2"):
            await _adapter().discover(cursor=None, limit=2)

    _run(scenario)


def test_discover_limit_igual_al_tamano_de_pagina_devuelve_todo() -> None:
    """FR-004: borde del contrato — `limit` == tamaño de página no lanza error.

    Con `limit` igual al número de IDs deduplicados de la página, `discover`
    devuelve todos los IDs y el **cursor real** (`/best/2026-07/1`), sin
    repetir el recibido (`None`): el llamador avanza de página con normalidad.
    """
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor=None, limit=3)

    _run(scenario)
    assert page.external_ids == ["video.synth00001", "video.synth00002", "video.synth00003"]
    assert page.next_cursor == "/best/2026-07/1"


def test_discover_error_http_se_propaga() -> None:
    """Edge: un 500 del sitio se propaga (HTTPStatusError) para que la capa de jobs reintente."""

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await _adapter().discover(cursor="/ruta-desconocida", limit=10)

    _run(scenario)


def _section_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Transporte mock con la sección `/tags/x` servida con `listing_page_1.html`.

    El listado de la sección usa la MISMA estructura que el resto de fixtures
    (sintética, SEC-004) y su `a.dir.next` apunta a `/best/2026-07/1`: la
    paginación desde la sección recorre la cadena normal.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tags/x":
            return httpx.Response(
                200, content=_fixture("listing_page_1.html").encode("utf-8"), request=request
            )
        return _fixture_handler()(request)

    return handler


def test_discover_section_arranca_en_la_url_de_la_seccion() -> None:
    """PR-049: con `section`, discover arranca en `https://www.xvideos.com<section>`.

    La primera URL pedida es exactamente la de la sección (categoría/tag) en
    vez de la home; el parseo del listado y el cursor son los de siempre (los
    IDs y el `a.dir.next` de la página de la sección).
    """
    requested: list[str] = []
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal requested, page
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or _section_handler()(request)
            )
        )
        page = await adapter.discover(cursor=None, limit=100, section="/tags/x")

    _run(scenario)
    assert requested == ["https://www.xvideos.com/tags/x"]
    assert page.external_ids == ["video.synth00001", "video.synth00002", "video.synth00003"]
    assert page.next_cursor == "/best/2026-07/1"


def test_discover_section_pagina_dentro_de_la_seccion() -> None:
    """PR-049: la cadena de paginación sigue a `a.dir.next` DESDE la sección.

    Tras arrancar en la sección, la segunda página se pide con el cursor de la
    primera (`a.dir.next` de la página de la sección): la paginación y el
    anti-bucle son idénticos a los de la home.
    """
    requested: list[str] = []
    pages: list[DiscoverPage] = []

    async def scenario() -> None:
        nonlocal requested, pages
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or _section_handler()(request)
            )
        )
        pages.append(await adapter.discover(cursor=None, limit=100, section="/tags/x"))
        pages.append(await adapter.discover(cursor=pages[0].next_cursor, limit=100))

    _run(scenario)
    assert requested == [
        "https://www.xvideos.com/tags/x",
        "https://www.xvideos.com/best/2026-07/1",
    ]
    assert pages[0].external_ids == ["video.synth00001", "video.synth00002", "video.synth00003"]
    assert pages[1].external_ids == ["video.synth00004"]
    assert pages[1].next_cursor == "/best/2026-07/2"


def test_discover_section_se_ignora_cuando_hay_cursor() -> None:
    """PR-049: con cursor, la URL sale del cursor — la sección solo fija el arranque."""
    requested: list[str] = []

    async def scenario() -> None:
        nonlocal requested
        base = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or base(request)
            )
        )
        page = await adapter.discover(cursor="/best/2026-07/1", limit=100, section="/tags/x")
        assert page.external_ids == ["video.synth00004"]

    _run(scenario)
    assert requested == ["https://www.xvideos.com/best/2026-07/1"]


def test_discover_section_sin_barra_inicial_lanza_error() -> None:
    """PR-049: una sección sin '/' inicial es error del llamador (ValueError).

    El contrato exige rutas absolutas del sitio (empezando por '/'): cualquier
    otro valor no debe construir una URL de discover.
    """

    async def scenario() -> None:
        with pytest.raises(ValueError, match="empezar por '/'"):
            await _adapter().discover(cursor=None, limit=100, section="tags/x")

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-002/FR-004 · get_video() con transporte mock (sin red)
# ---------------------------------------------------------------------------


def test_get_video_metadatos_completos() -> None:
    """FR-004: get_video devuelve el VideoSource normalizado del fixture completo.

    Además verifica SC-006: el mp4 completo existe en la página del fixture
    (`setVideoUrlLow`) pero `preview_url` queda `None` (prohibido exponerlo).
    """
    video: VideoSource | None = None

    async def scenario() -> None:
        nonlocal video
        video = await _adapter().get_video("video.synth00001")

    _run(scenario)
    assert video is not None
    assert video.external_id == "video.synth00001"
    assert video.title == "Titulo de ejemplo 1"
    assert video.duration_ms == 881_000
    assert video.preview_url is None  # SC-006: mp4 completo nunca expuesto


def test_get_video_404_devuelve_none() -> None:
    """Spec edge case: vídeo retirado (404) → None (sin reintentos infinitos)."""
    video: VideoSource | None

    async def scenario() -> None:
        nonlocal video
        video = await _adapter().get_video("video.synth99999")

    _run(scenario)
    assert video is None


def test_get_video_estructura_cambiada_levanta_error() -> None:
    """Edge: HTML sin patrón de vídeo → XvideosParseError (el job queda failed con error)."""

    async def scenario() -> None:
        with pytest.raises(XvideosParseError):
            await _adapter().get_video("video.synth50000")

    _run(scenario)


def test_get_video_con_page_url_usa_la_url_completa_del_listado() -> None:
    """PR-045 (3a validación): con `page_url`, get_video pide EXACTAMENTE la URL completa.

    Hallazgo de la 3a validación real: reconstruir `https://www.xvideos.com/
    video.<id>/` SIN el slug devuelve 404 en todos los vídeos de la home; el
    href real del listado es `/video.<id>/<num>/<num>/<slug>`. Con `page_url`,
    el adapter resuelve el href contra el host canónico y hace GET de la URL
    íntegra — la única que la fuente acepta.
    """
    requested: list[str] = []

    async def scenario() -> None:
        nonlocal requested
        handler = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or handler(request)
            )
        )
        video = await adapter.get_video(
            "video.synth00013",
            page_url="/video.synth00013/123456/789/titulo-de-ejemplo-13",
        )
        assert video is not None
        assert video.external_id == "video.synth00013"

    _run(scenario)
    assert requested == ["https://www.xvideos.com/video.synth00013/123456/789/titulo-de-ejemplo-13"]


def test_get_video_sin_page_url_fallback_a_la_plantilla() -> None:
    """PR-045: sin `page_url` (None), get_video reconstruye `/video.<id>/` como antes.

    Retrocompatibilidad: los llamadores que no disponen del href del listado
    (p. ej. FETCH_METADATA) mantienen el comportamiento previo al PR-045.
    """
    requested: list[str] = []

    async def scenario() -> None:
        nonlocal requested
        handler = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or handler(request)
            )
        )
        video = await adapter.get_video("video.synth00013", page_url=None)
        assert video is not None

    _run(scenario)
    assert requested == ["https://www.xvideos.com/video.synth00013/"]


def test_get_video_page_url_ajena_al_host_no_se_usa() -> None:
    """PR-045 · SEC-001: un `page_url` fuera del host canónico NO se usa (fallback).

    Solo se aceptan paths relativos (`/video.…`) o URLs http(s) de
    `xvideos.com`/`www.xvideos.com`: cualquier otro valor (host ajeno, esquema
    no http) cae a la plantilla — el adapter nunca pide URLs fuera de su
    dominio (el `SafeHTTPClient` con allowlist sería la segunda barrera).
    """

    async def scenario() -> None:
        video = await _adapter().get_video(
            "video.synth00013", page_url="https://evil.invalid/video.synth00013/x"
        )
        assert video is not None

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-005/SC-006 · get_visual_assets (galería xv_N_t.jpg; nunca el vídeo completo)
# ---------------------------------------------------------------------------


def _full_video() -> VideoSource:
    return parse_video_page(
        _fixture("video_page_full.html"),
        page_url="https://www.xvideos.invalid/video.synth00001/",
    )


def test_get_visual_assets_galeria_thumbnails_con_timestamps() -> None:
    """FR-005/PR-043: galería `xv_1..xv_6_t.jpg` con position y timestamp aproximado.

    Los thumbs de la galería (JSON-escapados en el script del reproductor) se
    parsean del mismo path CDN que `og:image`; `kind="thumbnail"`,
    `position=N` y `timestamp_ms = round(N/K*duration_ms)` con
    `K = MÁXIMA posición` (PR-053: galería contigua 1..6 → N/6, no N/7 como
    antes del fix; N==K → la duración exacta, clamp). Nunca un
    mp4 completo (SC-006) ni storyboard (sin sprite real, PR-043).
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        adapter = _adapter()
        video = await adapter.get_video("video.synth00001")
        assert video is not None
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert len(assets) == 6
    assert [a.kind for a in assets] == ["thumbnail"] * 6
    assert [a.position for a in assets] == [1, 2, 3, 4, 5, 6]
    assert [a.timestamp_ms for a in assets] == [
        146_833,  # round(1/6 * 881_000)
        293_667,  # round(2/6 * 881_000)
        440_500,  # round(3/6 * 881_000)
        587_333,  # round(4/6 * 881_000)
        734_167,  # round(5/6 * 881_000)
        881_000,  # round(6/6 * 881_000) == duración (clamp N==K)
    ]
    assert assets[0].url == f"{FIXTURE_THUMB_BASE}/xv_1_t.jpg"
    assert assets[-1].url == f"{FIXTURE_THUMB_BASE}/xv_6_t.jpg"


def test_get_visual_assets_galeria_posiciones_dispersas_timestamps_correctos() -> None:
    """PR-053 (8a validación real · validación de capturas del operador): regresión del bug.

    La galería real llega hasta `xv_30_t.jpg` (K=30) pero el markup solo
    expone posiciones DISPERSAS (3, 12, 15, 28, 30): el timestamp es
    `round(N/K*duration_ms)` — uniforme sobre el vídeo — y NO
    `round(N/(total+1)*duration_ms)` (el bug: el denominador era el nº de
    assets conservados, 5, y N=30 daba 6x la duración — p. ej.
    ts=2.400.000ms en un vídeo de 480.000ms). N=15 → 0.5*duración; N==K (30)
    → la duración exacta (clamp a `[0, duration_ms]`).
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        adapter = _adapter()
        video = await adapter.get_video("video.synth00021")
        assert video is not None
        assert video.duration_ms == 30_000  # og:duration "30" segundos → ms
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert len(assets) == 5
    assert [a.position for a in assets] == [3, 12, 15, 28, 30]
    assert [a.timestamp_ms for a in assets] == [
        3_000,  # round(3/30 * 30_000) = 0.1 * duración
        12_000,  # round(12/30 * 30_000) = 0.4 * duración
        15_000,  # round(15/30 * 30_000) = 0.5 * duración
        28_000,  # round(28/30 * 30_000) ≈ 0.933 * duración
        30_000,  # round(30/30 * 30_000) == duración (N==K, clamp)
    ]
    assert all(
        0 <= ts <= 30_000 for asset in assets for ts in (asset.timestamp_ms,) if ts is not None
    )  # clamp: ningún timestamp excede la duración


def test_get_visual_assets_galeria_sin_duracion_timestamps_none() -> None:
    """PR-053: sin `duration_ms` (og:duration ausente) → `timestamp_ms=None`.

    Defensivo: la fuente no expone una referencia temporal fiable por thumb;
    los assets se conservan con `position` pero sin timestamp (el pipeline
    no puede ordenarlos temporalmente).
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        adapter = _adapter()
        video = _full_video().model_copy(update={"duration_ms": None})
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert len(assets) == 6
    assert [a.position for a in assets] == [1, 2, 3, 4, 5, 6]
    assert [a.timestamp_ms for a in assets] == [None] * 6


def test_get_visual_assets_degrada_a_miniatura_unica() -> None:
    """FR-005: sin galería en el reproductor → degrada a la miniatura única.

    El `thumbnail_url` (og:image) se mantiene como asset `thumbnail` sin
    posición ni timestamp (jerarquía de assets: no se pierde el vídeo).
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        adapter = _adapter()
        video = await adapter.get_video("video.synth00007")
        assert video is not None
        assert video.thumbnail_url is not None
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert assets == [
        VisualAsset(kind="thumbnail", url=f"{FIXTURE_THUMB_BASE}/mozaique_listing.jpg")
    ]


def test_get_visual_assets_video_sin_assets_devuelve_vacio() -> None:
    """Spec edge case: vídeo sin thumbnail ni galería → lista vacía, sin fallar."""
    video = parse_video_page(
        _fixture("video_page_minimal.html"),
        page_url="https://www.xvideos.invalid/video.synth00002/",
    )
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        assets = await _adapter().get_visual_assets(video)

    _run(scenario)
    assert assets == []


def test_get_visual_assets_con_page_url_usa_la_url_completa() -> None:
    """PR-047 (5a validación): con `page_url` completo, pide la URL canónica con slug.

    Hallazgo de la 5a validación real (2026-08-16): INDEX_VIDEO fallaba con 404
    porque el adapter reconstruía `/video.<id>/` SIN el slug aunque el
    `VideoSource` recibido SÍ llevaba el `page_url` completo (persistido por
    DISCOVER, PR-045/046, y mapeado por `video_source_from_record`). Con
    `page_url`, el adapter resuelve la URL completa contra el host canónico y
    pide exactamente esa — la única que la fuente acepta. Además se verifica
    que la galería de thumbnails (filtro por el path CDN de og:image, PR-043)
    sigue parseándose desde la página servida en la URL completa.
    """
    requested: list[str] = []
    page_url = "https://www.xvideos.com/video.synth00001/titulo-de-ejemplo-1"
    video = _full_video().model_copy(update={"page_url": page_url})
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        handler = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or handler(request)
            )
        )
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert requested == [page_url]
    assert len(assets) == 6  # PR-043: galería xv_1..xv_6_t.jpg desde la página completa


def test_get_visual_assets_page_url_vacio_fallback_a_la_plantilla() -> None:
    """PR-047: `page_url` vacío → fallback a `/video.<id>/` (retrocompatible).

    `VideoSource.page_url` es obligatoria en el contrato, pero el fallback se
    mantiene como red de seguridad (y para llamadores sin href canónico): sin
    URL con slug, el adapter reconstruye la plantilla como antes.
    """
    requested: list[str] = []
    video = _full_video().model_copy(update={"page_url": ""})
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        handler = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or handler(request)
            )
        )
        assets = await adapter.get_visual_assets(video)

    _run(scenario)
    assert requested == ["https://www.xvideos.com/video.synth00001/"]
    assert len(assets) == 6


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


def test_check_availability_404_removed() -> None:
    """Spec edge case: 404 → removed (estado terminal, sin reintentos)."""
    removed = _full_video().model_copy(update={"external_id": "video.synth99999"})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(removed)

    _run(scenario)
    assert availability == VideoAvailability.REMOVED


def test_check_availability_estructura_cambiada_unavailable() -> None:
    """Edge: no se puede confirmar la disponibilidad → unavailable (sin crashear)."""
    changed = _full_video().model_copy(update={"external_id": "video.synth50000"})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(changed)

    _run(scenario)
    assert availability == VideoAvailability.UNAVAILABLE


def test_check_availability_con_page_url_usa_la_url_completa() -> None:
    """PR-047 (5a validación): con `page_url`, check_availability pide la URL con slug.

    Mismo hallazgo que INDEX_VIDEO: reconstruir `/video.<id>/` sin el slug
    devuelve 404, que el adapter interpretaba como `removed` — falso negativo
    terminal (el vídeo sigue en la fuente). Con `page_url` completo se pide la
    URL canónica exacta y la disponibilidad se confirma.
    """
    requested: list[str] = []
    page_url = "https://www.xvideos.com/video.synth00001/titulo-de-ejemplo-1"
    video = _full_video().model_copy(update={"page_url": page_url})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        handler = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or handler(request)
            )
        )
        availability = await adapter.check_availability(video)

    _run(scenario)
    assert requested == [page_url]
    assert availability == VideoAvailability.AVAILABLE


def test_check_availability_page_url_vacio_fallback_a_la_plantilla() -> None:
    """PR-047: `page_url` vacío → fallback a `/video.<id>/` (retrocompatible)."""
    requested: list[str] = []
    video = _full_video().model_copy(update={"page_url": ""})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        handler = _fixture_handler()
        adapter = XvideosAdapter(
            transport=httpx.MockTransport(
                lambda request: requested.append(str(request.url)) or handler(request)
            )
        )
        availability = await adapter.check_availability(video)

    _run(scenario)
    assert requested == ["https://www.xvideos.com/video.synth00001/"]
    assert availability == VideoAvailability.AVAILABLE


# ---------------------------------------------------------------------------
# SEC-004 · Fixtures sintéticos (sin contenido real en el repo)
# ---------------------------------------------------------------------------


def test_fixtures_no_usan_dominio_real_xvideos_com() -> None:
    """SEC-004: los datos de los fixtures son sintéticos; solo `xvideos.invalid`.

    Se escanea el HTML de los fixtures (los datos): el dominio real
    `xvideos.com` queda prohibido; el README.md es documentación y puede
    mencionar la política (no es contenido de la fuente).
    """
    for path in sorted(FIXTURES_DIR.glob("*.html")):
        content = path.read_text(encoding="utf-8")
        assert "xvideos.com" not in content, (
            f"SEC-004 violado: {path.name} contiene el dominio real xvideos.com"
        )
        assert "xvideos.invalid" in content


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


def test_core_no_importa_el_adapter_xvideos() -> None:
    """SC-007: ningún módulo del core importa el adapter xvideos.

    Añadir esta fuente solo añade ficheros del adapter (y su registro futuro);
    el core (xtrace_crawler fuera de `adapters/`) no conoce xvideos. Si un
    selector o estructura cambia, el fallo queda contenido en el adapter.
    """
    package_root = Path(_crawler_pkg_init).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if "adapters" in path.parts:
            continue
        for module in _imported_module_names(path):
            parts = module.split(".")
            if "xvideos" in parts:
                offenders.append(f"{path.relative_to(package_root)} importa {module}")
    assert offenders == [], f"SC-007 violado: el core importa el adapter xvideos: {offenders}"
