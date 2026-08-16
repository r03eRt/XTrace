"""Tests del adapter xvideos + fixtures sintéticos
(PR-031 · FR-004 · SEC-001/002/004 · SC-006/007 · ADR-0009).

Trazabilidad (constitución §3): cada test marca el requisito que valida:

- FR-004: método de acceso "html" documentado en el manifest; `discover()` con
  cursor de paginación y `get_video()` sobre HTML de xvideos.
- FR-002 (soporte): `VideoSource` normalizado poblado desde el parseo.
- FR-005/SC-006: `get_visual_assets` solo storyboard/thumbnail (nunca el vídeo
  completo); el manifest declara exactamente `["storyboard", "thumbnail"]`.
- SEC-001: el adapter solo usa el cliente HTTP seguro (allowlist
  xvideos.com/www.xvideos.com); ningún test toca la red (`httpx.MockTransport`).
- SEC-002: el manifest está **revisado por el operador** (`robots_reviewed=True`,
  `terms_reviewed=True`, `review_date="2026-08-16"`, aprobación en modo prueba,
  PR-042) — la habilitación **efectiva** sigue exigiendo el gate del registry
  (PR-028): manifest conforme Y `sources.enabled=true` en BD (SEC-002).
- SEC-004: los fixtures son sintéticos (dominio `xvideos.invalid`, títulos
  anonimizados; ningún `xvideos.com` real en los fixtures).
- SC-007: el core no importa el adapter (añadir esta fuente no toca el core).

Estructura HTML asumida: ver `tests/fixtures/xvideos/README.md`. La captura real
la hará el operador en PR-033; los fixtures congelan la estructura asumida y los
tests fallan con mensaje claro si un selector clave cambia (regresión).
"""

from __future__ import annotations

import ast
import asyncio
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


def _fixture(name: str) -> str:
    """Lee un fixture sintético de `tests/fixtures/xvideos/` (SEC-004)."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _run(coro: Callable[[], object]) -> None:
    """Ejecuta el escenario async sin dependencia de pytest-asyncio (determinista)."""
    asyncio.run(coro())


def _fixture_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Transporte mock: sirve los fixtures por path, sin red (NFR-003).

    - `/video10000001/...` → `video_page_full.html`
    - `/video10000002/...` → `video_page_minimal.html`
    - `/video50000000/...` → 200 con HTML sin estructura de vídeo (estructura cambiada)
    - `/video99999999/...` → 404 (vídeo retirado)
    - `/` → `listing_page_1.html`; `/best/2` → `listing_page_2.html`
    - cualquier otro path → 500 (error transitorio del sitio)
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: bytes
        if path.startswith("/video10000001"):
            body = _fixture("video_page_full.html").encode("utf-8")
        elif path.startswith("/video10000002"):
            body = _fixture("video_page_minimal.html").encode("utf-8")
        elif path.startswith("/video50000000"):
            body = b"<html><body>captcha o estructura totalmente distinta</body></html>"
        elif path.startswith("/video99999999"):
            return httpx.Response(404, content=b"", request=request)
        elif path == "/":
            body = _fixture("listing_page_1.html").encode("utf-8")
        elif path == "/best/2":
            body = _fixture("listing_page_2.html").encode("utf-8")
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
    assert manifest.assets_accessed == ["storyboard", "thumbnail"]  # SC-006
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


def test_asset_hosts_provisionales_no_vacios_y_solo_hosts() -> None:
    """PR-040 · SEC-001 · contracts §1: `asset_hosts` no vacío y solo hosts.

    La allowlist de assets es **PROVISIONAL** — validar contra la estructura
    real en PR-033 (captura real del operador). No vacía (sin allowlist el
    pipeline no descargaría nada, fail-closed) y con **solo hosts**: sin
    esquemas, rutas, query ni fragmentos — el `SafeHTTPClient` (PR-036) hace
    match exacto de host y nunca recibe URLs completas.
    """
    hosts = XvideosAdapter.asset_hosts
    assert hosts  # PROVISIONAL — no vacía
    for host in hosts:
        assert "://" not in host, f"esquema en asset_host: {host!r}"
        assert "/" not in host, f"ruta en asset_host: {host!r}"
        assert "?" not in host and "#" not in host, f"query/fragmento en asset_host: {host!r}"
        assert host.strip() == host, f"espacios en asset_host: {host!r}"


# ---------------------------------------------------------------------------
# FR-002/FR-004 · Parseo de la página de vídeo (selectolax)
# ---------------------------------------------------------------------------


def test_parse_video_page_full_fixture_metadatos_completos() -> None:
    """FR-004/FR-002: el fixture completo produce VideoSource con todos los campos.

    title, duration_ms, thumbnail_url, preview_url, storyboard_urls y
    published_at derivados del canonical, `h2.page-title` y `flashvars`;
    tags de `div.video-tags-list a`; page_url = canonical.
    """
    video = parse_video_page(
        _fixture("video_page_full.html"), page_url="https://www.xvideos.com/video10000001/"
    )
    assert video.source == "xvideos"
    assert video.external_id == "10000001"
    assert video.title == "Fixture video sample 0001"
    assert video.duration_ms == 337_000  # "337" segundos en flashvars → ms
    assert video.thumbnail_url == "https://th-01.xvideos.invalid/thumbs/10000001.jpg"
    assert video.preview_url == "https://vd-01.xvideos.invalid/previews/10000001.mp4"
    assert video.storyboard_urls == ["https://th-01.xvideos.invalid/sprites/10000001.jpg"]
    assert video.tags == ["fixture tag one", "fixture tag two"]
    assert video.published_at == datetime.fromtimestamp(1_755_200_000, tz=UTC)
    assert video.page_url == "https://www.xvideos.invalid/video10000001/fixture-video-sample-0001"


def test_parse_video_page_minimal_fixture_opcionales_none() -> None:
    """Spec edge case: sin flashvars ni tags, los campos opcionales son None/[].

    El vídeo sigue procesándose (metadatos incompletos no bloquean).
    """
    video = parse_video_page(
        _fixture("video_page_minimal.html"), page_url="https://www.xvideos.com/video10000002/"
    )
    assert video.external_id == "10000002"
    assert video.title == "Fixture video sample 0002"
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.preview_url is None
    assert video.storyboard_urls == []
    assert video.tags == []
    assert video.published_at is None
    assert video.page_url == "https://www.xvideos.invalid/video10000002/fixture-video-sample-0002"


def test_parse_video_page_sin_patron_de_video_error_claro() -> None:
    """Regresión de estructura: sin canonical ni patrón `/video<id>/` → error claro.

    Si xvideos cambia un selector clave (p. ej. el canonical), el parseo falla
    con mensaje que identifica el patrón esperado en lugar de devolver datos
    basura (edge case "HTML cambia sin aviso" de la spec).
    """
    html = "<html><body><h2 class='page-title'>Algo</h2></body></html>"
    with pytest.raises(XvideosParseError, match="patrón de vídeo"):
        parse_video_page(html, page_url="https://www.xvideos.com/random")


def test_parse_video_page_flashvars_invalidos_no_revientan() -> None:
    """Edge: `flashvars` presente pero con JSON inválido → opcionales None (sin crash)."""
    html = (
        "<html><head><link rel='canonical' href='https://www.xvideos.invalid/video10000003/x'>"
        "</head><body><h2 class='page-title'>X</h2>"
        "<script>var flashvars = {not: valid json};</script></body></html>"
    )
    video = parse_video_page(html, page_url="https://www.xvideos.com/video10000003/")
    assert video.external_id == "10000003"
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.storyboard_urls == []
    assert video.published_at is None


# ---------------------------------------------------------------------------
# FR-004 · Parseo de la página de listado (discover: IDs + cursor)
# ---------------------------------------------------------------------------


def test_parse_listing_page_ids_y_cursor_con_dedup() -> None:
    """FR-004: el listado produce IDs únicos (dedup) y el cursor de paginación."""
    page = parse_listing_page(_fixture("listing_page_1.html"))
    # El thumb 10000001 tiene dos enlaces (imagen + overlay): se deduplica.
    assert page.external_ids == ["10000001", "10000002", "10000003"]
    assert page.next_cursor == "/best/2"


def test_parse_listing_page_fin_de_paginacion() -> None:
    """FR-004: sin enlace `next-page`, el cursor es None (última página)."""
    page = parse_listing_page(_fixture("listing_page_2.html"))
    assert page.external_ids == ["10000004"]
    assert page.next_cursor is None


def test_parse_listing_page_href_absoluto_normalizado_a_path() -> None:
    """FR-004: el enlace `next-page` absoluto se normaliza a path como cursor."""
    html = (
        "<html><body><div class='thumb'><a href='/video10000009/x'>v</a></div>"
        "<div class='pagination'><a class='next-page' "
        "href='https://www.xvideos.com/best/3'>Next</a></div></body></html>"
    )
    page = parse_listing_page(html)
    assert page.next_cursor == "/best/3"


def test_parse_listing_page_estructura_cambiada_devuelve_vacio() -> None:
    """Edge: si el listado cambia de estructura, discover devuelve vacío sin crashear.

    El fallo queda aislado en el adapter (SC-008): el llamador ve una página
    vacía y no se corrompe el flujo; los tests de regresión sobre los fixtures
    señalan el cambio de selector.
    """
    page = parse_listing_page("<html><body><div class='otra-estructura'>x</div></body></html>")
    assert page.external_ids == []
    assert page.next_cursor is None


# ---------------------------------------------------------------------------
# FR-004 · discover() con transporte mock (sin red)
# ---------------------------------------------------------------------------


def test_discover_primera_pagina_ids_y_cursor() -> None:
    """FR-004: discover sin cursor devuelve los IDs y el cursor siguiente."""
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor=None, limit=100)

    _run(scenario)
    assert page.external_ids == ["10000001", "10000002", "10000003"]
    assert page.next_cursor == "/best/2"


def test_discover_segunda_pagina_fin_de_paginacion() -> None:
    """FR-004: discover con cursor avanza a la página siguiente (sin más páginas)."""
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor="/best/2", limit=100)

    _run(scenario)
    assert page.external_ids == ["10000004"]
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
    devuelve todos los IDs y el **cursor real** (`/best/2`), sin repetir el
    recibido (`None`): el llamador avanza de página con normalidad.
    """
    page: DiscoverPage

    async def scenario() -> None:
        nonlocal page
        page = await _adapter().discover(cursor=None, limit=3)

    _run(scenario)
    assert page.external_ids == ["10000001", "10000002", "10000003"]
    assert page.next_cursor == "/best/2"


def test_discover_error_http_se_propaga() -> None:
    """Edge: un 500 del sitio se propaga (HTTPStatusError) para que la capa de jobs reintente."""

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await _adapter().discover(cursor="/ruta-desconocida", limit=10)

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-002/FR-004 · get_video() con transporte mock (sin red)
# ---------------------------------------------------------------------------


def test_get_video_metadatos_completos() -> None:
    """FR-004: get_video devuelve el VideoSource normalizado del fixture completo."""
    video: VideoSource | None = None

    async def scenario() -> None:
        nonlocal video
        video = await _adapter().get_video("10000001")

    _run(scenario)
    assert video is not None
    assert video.external_id == "10000001"
    assert video.title == "Fixture video sample 0001"
    assert video.duration_ms == 337_000


def test_get_video_404_devuelve_none() -> None:
    """Spec edge case: vídeo retirado (404) → None (sin reintentos infinitos)."""
    video: VideoSource | None

    async def scenario() -> None:
        nonlocal video
        video = await _adapter().get_video("99999999")

    _run(scenario)
    assert video is None


def test_get_video_estructura_cambiada_levanta_error() -> None:
    """Edge: HTML sin patrón de vídeo → XvideosParseError (el job queda failed con error)."""

    async def scenario() -> None:
        with pytest.raises(XvideosParseError):
            await _adapter().get_video("50000000")

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-005/SC-006 · get_visual_assets (nunca el vídeo completo)
# ---------------------------------------------------------------------------


def _full_video() -> VideoSource:
    return parse_video_page(
        _fixture("video_page_full.html"), page_url="https://www.xvideos.com/video10000001/"
    )


def test_get_visual_assets_storyboard_y_thumbnail_en_jerarquia() -> None:
    """FR-005: assets en orden de jerarquía (storyboard → thumbnail); nunca video (SC-006).

    El preview está parseado en el VideoSource pero NO se ofrece como asset:
    el manifest declara `assets_accessed=["storyboard","thumbnail"]` (SC-006;
    la revisión del operador de 2026-08-16 mantuvo el alcance — ampliarlo en
    el manifest bastaría).
    """
    assets: list[VisualAsset] = []

    async def scenario() -> None:
        nonlocal assets
        assets = await _adapter().get_visual_assets(_full_video())

    _run(scenario)
    assert [a.kind for a in assets] == ["storyboard", "thumbnail"]
    assert assets[0].url == "https://th-01.xvideos.invalid/sprites/10000001.jpg"
    assert assets[1].url == "https://th-01.xvideos.invalid/thumbs/10000001.jpg"
    assert all(a.kind in {"storyboard", "thumbnail", "preview"} for a in assets)


def test_get_visual_assets_video_sin_assets_devuelve_vacio() -> None:
    """Spec edge case: vídeo sin storyboard/thumbnail → lista vacía, sin fallar."""
    video = parse_video_page(
        _fixture("video_page_minimal.html"), page_url="https://www.xvideos.com/video10000002/"
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


def test_check_availability_404_removed() -> None:
    """Spec edge case: 404 → removed (estado terminal, sin reintentos)."""
    removed = _full_video().model_copy(update={"external_id": "99999999"})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(removed)

    _run(scenario)
    assert availability == VideoAvailability.REMOVED


def test_check_availability_estructura_cambiada_unavailable() -> None:
    """Edge: no se puede confirmar la disponibilidad → unavailable (sin crashear)."""
    changed = _full_video().model_copy(update={"external_id": "50000000"})
    availability: VideoAvailability

    async def scenario() -> None:
        nonlocal availability
        availability = await _adapter().check_availability(changed)

    _run(scenario)
    assert availability == VideoAvailability.UNAVAILABLE


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
