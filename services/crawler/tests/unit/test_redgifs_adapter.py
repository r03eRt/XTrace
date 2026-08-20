"""Tests del adapter redgifs + fixtures sintéticos de la estructura OBSERVADA
(PR-066 · FR-001…FR-006, FR-008, FR-010 · SEC-001/003/004/005 · NFR-003/004 ·
SC-001/004/005 · ADR-0016).

Trazabilidad (constitución §3): cada test marca el requisito que valida:

- FR-001: el adapter cumple el protocolo `SourceAdapter` (discover/get_video/
  get_visual_assets/check_availability + manifest); el manifest documenta el
  compliance (SEC-002).
- FR-002/FR-004: método de acceso "api" (primer adapter de este nivel);
  `www.redgifs.com` nunca se fetchea.
- FR-003: `discover()` con `section` OBLIGATORIO con prefijo `/niches/`
  (Decisión D2, fail-fast); paginación por `page` con anti-bucle (0 IDs
  nuevos / `page>=pages` → fin); truncación no soportada →
  `RedgifsParseError`.
- FR-004: `get_video()` normaliza el objeto gif (wrapper `{"gif": {...}}`),
  `external_id` lowercase estable, campos opcionales nulos, posts de imagen
  (`duration=null`) sin fallar; `page_url` fijo, nunca fetcheado (D5).
- FR-005: `get_visual_assets()` devuelve solo thumbnail + poster
  (`kind="thumbnail"`, sin timestamp); nunca `sd`/`hd`/`silent` (SC-004/006).
- FR-006/SEC-001/003: `asset_hosts=["media.redgifs.com"]`; allowlist de la
  API `api.redgifs.com`, con `httpx.MockTransport` (sin red, NFR-003).
- SEC-005: el token temporal se renueva ante 401 y nunca aparece en errores.
- SC-001: flujo completo con fixtures, sin red, determinista.
- SC-007: el core no importa el adapter (test AST).

Estructura de API observada: ver `tests/fixtures/redgifs/README.md`
(prospección 2026-08-19). Los fixtures congelan la estructura observada; los
tests fallan con mensaje claro si el envelope/objeto cambia (regresión).
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from xtrace_crawler import __file__ as _crawler_pkg_init
from xtrace_crawler.adapters.base import SourceAdapter
from xtrace_crawler.adapters.models import VideoAvailability, VideoSource, VisualAsset
from xtrace_crawler.adapters.redgifs import (
    RG_API_HOSTS,
    RG_ASSET_HOSTS,
    RedgifsAdapter,
    RedgifsAuthError,
    RedgifsParseError,
    parse_gif_object,
    parse_gif_response,
    parse_niche_gifs_envelope,
)
from xtrace_crawler.crawling.http import HostNotAllowedError, SafeHTTPClient

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "redgifs"


def _fixture_json(name: str) -> dict[str, Any]:
    """Lee y parsea un fixture sintético de `tests/fixtures/redgifs/` (SEC-004)."""
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _run(coro: Callable[[], Coroutine[object, object, object]]) -> None:
    """Ejecuta el escenario async sin dependencia de pytest-asyncio (determinista)."""
    asyncio.run(coro())


def _json_response(
    payload: dict[str, Any], *, status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, request=request)

    return handler


def _page_param(request: httpx.Request) -> str | None:
    query = parse_qs(urlsplit(str(request.url)).query)
    values = query.get("page")
    return values[0] if values else None


def _fixture_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Transporte mock: sirve los fixtures por path (+ `page` para el listado).

    - `GET /v2/auth/temporary` → `auth_temporary.json`.
    - `GET /v2/niches/homemade/gifs` con `page=1|2|3` → páginas 1/2/vacía.
    - `GET /v2/niches/other-niche/gifs` con `page=1` → **el mismo** fixture de
      la página 1 de homemade (deliberado: prueba el aislamiento del
      anti-bucle entre nichos — regresión de la revisión independiente,
      2026-08-20).
    - `GET /v2/gifs/abchomemadeone` → `gif_object.json`.
    - `GET /v2/gifs/jklimagepostfour` → `gif_object_image_post.json`.
    - `GET /v2/gifs/removeditem` → 404 `gif_not_found_404.json`.
    - cualquier otro path → 500 (error transitorio del servicio).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/auth/temporary":
            return httpx.Response(200, json=_fixture_json("auth_temporary.json"), request=request)
        if path == "/v2/niches/homemade/gifs":
            page = _page_param(request)
            if page == "1":
                body = _fixture_json("niche_gifs_page_1.json")
                return httpx.Response(200, json=body, request=request)
            if page == "2":
                body = _fixture_json("niche_gifs_page_2.json")
                return httpx.Response(200, json=body, request=request)
            if page == "3":
                body = _fixture_json("niche_gifs_empty.json")
                return httpx.Response(200, json=body, request=request)
        if path == "/v2/niches/other-niche/gifs" and _page_param(request) == "1":
            # Mismos IDs que homemade página 1, a propósito (ver docstring).
            body = _fixture_json("niche_gifs_page_1.json")
            return httpx.Response(200, json=body, request=request)
        if path == "/v2/gifs/abchomemadeone":
            return httpx.Response(200, json=_fixture_json("gif_object.json"), request=request)
        if path == "/v2/gifs/jklimagepostfour":
            return httpx.Response(
                200, json=_fixture_json("gif_object_image_post.json"), request=request
            )
        if path == "/v2/gifs/removeditem":
            body = _fixture_json("gif_not_found_404.json")
            return httpx.Response(404, json=body, request=request)
        return httpx.Response(500, json={"error": {"code": "Boom"}}, request=request)

    return handler


def _tracking_handler(
    requested: list[str], handler: Callable[[httpx.Request], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """Envuelve un handler registrando cada URL pedida (para asserts de peticiones)."""

    def tracked(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    return tracked


def _adapter(handler: Callable[[httpx.Request], httpx.Response] | None = None) -> RedgifsAdapter:
    """Adapter con transporte mock: ningún test toca la red (NFR-003, SEC-001)."""
    return RedgifsAdapter(transport=httpx.MockTransport(handler or _fixture_handler()))


# ---------------------------------------------------------------------------
# FR-001 · protocolo + manifest
# ---------------------------------------------------------------------------


def test_manifest_declara_compliance_en_modo_prueba() -> None:
    """SEC-002 · Decisión D4: manifest revisado (robots/terms/review_date)."""
    manifest = RedgifsAdapter.manifest
    assert manifest.source == "redgifs"
    assert manifest.access_method == "api"
    assert manifest.assets_accessed == ["thumbnail"]
    assert manifest.robots_reviewed is True
    assert manifest.terms_reviewed is True
    assert manifest.review_date == "2026-08-19"
    assert manifest.rate_limit.min_interval_ms == 2_000
    assert manifest.rate_limit.max_rps == 0.5


def test_adapter_satisface_el_protocolo_source_adapter() -> None:
    """FR-001: `RedgifsAdapter` es una instancia estructural de `SourceAdapter`."""
    assert isinstance(_adapter(), SourceAdapter)


def test_asset_hosts_declarados() -> None:
    """FR-006/SEC-001: allowlist de assets = `media.redgifs.com` (fail-closed)."""
    assert RedgifsAdapter.asset_hosts == RG_ASSET_HOSTS
    assert RG_ASSET_HOSTS == ["media.redgifs.com"]


def test_allowlist_de_host_de_api_rechaza_host_ajeno() -> None:
    """SEC-001/003: `SafeHTTPClient` con la allowlist del adapter rechaza `www.redgifs.com`."""
    assert RG_API_HOSTS == frozenset({"api.redgifs.com"})

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts=RG_API_HOSTS, transport=httpx.MockTransport(_fixture_handler())
        ) as client:
            with pytest.raises(HostNotAllowedError):
                await client.get("https://www.redgifs.com/watch/x")

    _run(scenario)


# ---------------------------------------------------------------------------
# Parsers puros: objeto gif (FR-004)
# ---------------------------------------------------------------------------


def test_parse_gif_object_campos_completos() -> None:
    gif = _fixture_json("gif_object.json")["gif"]
    video = parse_gif_object(gif)
    assert video.source == "redgifs"
    assert video.external_id == "abchomemadeone"  # normalizado a lowercase
    assert video.title == "fixture gif one"
    assert video.page_url == "https://www.redgifs.com/watch/abchomemadeone"
    assert video.duration_ms == 12_500
    assert video.thumbnail_url == "https://media.redgifs.invalid/AbcHomemadeOne-mobile.jpg"
    assert video.preview_url is None  # SC-006: nunca se exponen los mp4
    assert video.storyboard_urls == ["https://media.redgifs.invalid/AbcHomemadeOne-poster.jpg"]
    assert video.tags == ["amateur", "homemade"]
    assert video.published_at is not None


def test_parse_gif_object_id_ya_en_lowercase_no_cambia() -> None:
    gif = _fixture_json("niche_gifs_page_2.json")["gifs"][0]
    video = parse_gif_object(gif)
    assert video.external_id == "ghihomemadethree"


def test_parse_gif_object_campos_opcionales_ausentes_no_fallan() -> None:
    """Edge case de la spec: sin `description`/`tags`/`createDate` → nulos, sin error."""
    gif = _fixture_json("niche_gifs_page_1.json")["gifs"][1]  # "def-homemade-two"
    video = parse_gif_object(gif)
    assert video.title is None
    assert video.tags == []
    assert video.published_at is not None  # createDate SÍ está en este fixture
    assert video.storyboard_urls == []  # sin poster


def test_parse_gif_object_post_de_imagen_duration_null() -> None:
    """Edge case de la spec: `type=2`/`duration=null`/`hasAudio=false` → duration_ms=None."""
    gif = _fixture_json("gif_object_image_post.json")["gif"]
    video = parse_gif_object(gif)
    assert video.external_id == "jklimagepostfour"
    assert video.duration_ms is None
    assert video.thumbnail_url is not None
    assert video.storyboard_urls == ["https://media.redgifs.invalid/JklImagePostFour-poster.jpg"]


def test_parse_gif_object_sin_id_valido_lanza_error_tipado() -> None:
    with pytest.raises(RedgifsParseError):
        parse_gif_object({"description": "sin id"})
    with pytest.raises(RedgifsParseError):
        parse_gif_object({"id": ""})


def test_parse_gif_object_nunca_lee_urls_sd_hd_silent_hacia_otros_campos() -> None:
    """SC-004/SC-006: los mp4 completos no acaban en ningún campo del `VideoSource`."""
    gif = _fixture_json("gif_object.json")["gif"]
    video = parse_gif_object(gif)
    forbidden_fragments = ("-sd.mp4", "-hd.mp4", "-silent.mp4")
    all_urls = [video.thumbnail_url, video.preview_url, *video.storyboard_urls]
    for url in all_urls:
        if url is not None:
            assert not any(fragment in url for fragment in forbidden_fragments)


def test_parse_gif_response_wrapper_completo() -> None:
    payload = _fixture_json("gif_object.json")
    video = parse_gif_response(payload)
    assert video.external_id == "abchomemadeone"


def test_parse_gif_response_sin_campo_gif_lanza_error_tipado() -> None:
    with pytest.raises(RedgifsParseError):
        parse_gif_response({"error": {"code": "GifNotFound"}})


# ---------------------------------------------------------------------------
# Parsers puros: envelope de listado (FR-003)
# ---------------------------------------------------------------------------


def test_parse_niche_gifs_envelope_pagina_completa() -> None:
    payload = _fixture_json("niche_gifs_page_1.json")
    page = parse_niche_gifs_envelope(payload)
    assert page.external_ids == ["abchomemadeone", "def-homemade-two"]
    assert set(page.gifs_by_id) == set(page.external_ids)
    assert page.page == 1
    assert page.pages == 2


def test_parse_niche_gifs_envelope_dedup_ids_repetidos() -> None:
    gif = _fixture_json("niche_gifs_page_1.json")["gifs"][0]
    payload = {"gifs": [gif, gif], "page": 1, "pages": 1, "total": 1}
    page = parse_niche_gifs_envelope(payload)
    assert page.external_ids == ["abchomemadeone"]


def test_parse_niche_gifs_envelope_vacio_con_pages_total_fin_sin_error() -> None:
    """Edge case: `gifs` vacío con `pages`/`total` presentes → fin, sin error."""
    payload = _fixture_json("niche_gifs_empty.json")
    page = parse_niche_gifs_envelope(payload)
    assert page.external_ids == []
    assert page.gifs_by_id == {}
    assert page.page == 3
    assert page.pages == 2


def test_parse_niche_gifs_envelope_vacio_sin_pages_total_lanza_error_tipado() -> None:
    """Edge case: `gifs` vacío Y `pages`/`total` ausentes (respuesta count=0 inválida)."""
    with pytest.raises(RedgifsParseError):
        parse_niche_gifs_envelope({"gifs": []})
    with pytest.raises(RedgifsParseError):
        parse_niche_gifs_envelope({})


def test_parse_niche_gifs_envelope_item_sin_id_se_ignora() -> None:
    good = _fixture_json("niche_gifs_page_1.json")["gifs"][0]
    payload = {"gifs": [{"description": "sin id"}, good], "page": 1, "pages": 1, "total": 1}
    page = parse_niche_gifs_envelope(payload)
    assert page.external_ids == ["abchomemadeone"]


# ---------------------------------------------------------------------------
# FR-003 · discover()
# ---------------------------------------------------------------------------


def test_discover_sin_section_falla_fail_fast() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(ValueError, match="niches"):
            await adapter.discover(cursor=None, limit=100, section=None)
        await adapter.aclose()

    _run(scenario)


def test_discover_con_section_invalida_falla_fail_fast() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(ValueError, match="niches"):
            await adapter.discover(cursor=None, limit=100, section="/tags/homemade")
        await adapter.aclose()

    _run(scenario)


def test_discover_primera_pagina() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        page = await adapter.discover(cursor=None, limit=100, section="/niches/homemade")
        assert page.external_ids == ["abchomemadeone", "def-homemade-two"]
        assert page.next_cursor == "2"
        await adapter.aclose()

    _run(scenario)


def test_discover_ultima_pagina_por_page_pages() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        page = await adapter.discover(cursor="2", limit=100, section="/niches/homemade")
        assert page.external_ids == ["ghihomemadethree"]
        assert page.next_cursor is None  # page(2) >= pages(2)
        await adapter.aclose()

    _run(scenario)


def test_discover_pagina_vacia_legitima_fin_sin_error() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        page = await adapter.discover(cursor="3", limit=100, section="/niches/homemade")
        assert page.external_ids == []
        assert page.next_cursor is None
        await adapter.aclose()

    _run(scenario)


def test_discover_anti_bucle_cero_ids_nuevos() -> None:
    """Repetir la primera página tras haberla visto → 0 IDs nuevos → fin."""

    async def scenario() -> None:
        adapter = _adapter()
        first = await adapter.discover(cursor=None, limit=100, section="/niches/homemade")
        assert first.next_cursor == "2"
        repeat = await adapter.discover(cursor="1", limit=100, section="/niches/homemade")
        assert repeat.next_cursor is None
        await adapter.aclose()

    _run(scenario)


def test_discover_truncacion_no_soportada_lanza_error_tipado() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(RedgifsParseError):
            await adapter.discover(cursor=None, limit=1, section="/niches/homemade")
        await adapter.aclose()

    _run(scenario)


def test_discover_limit_menor_que_1_falla_fail_fast() -> None:
    """Regresión (revisión independiente, 2026-08-20): `limit<=0` ya no cae
    al tamaño de página fijo de 100 (que reproduciría la trampa de
    truncación con `limit=0`); se rechaza explícitamente."""

    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(ValueError, match="limit"):
            await adapter.discover(cursor=None, limit=0, section="/niches/homemade")
        with pytest.raises(ValueError, match="limit"):
            await adapter.discover(cursor=None, limit=-1, section="/niches/homemade")
        await adapter.aclose()

    _run(scenario)


def test_discover_section_con_segmentos_extra_falla_fail_fast() -> None:
    """Regresión (revisión independiente, 2026-08-20): `/niches/<id>/algo`
    ya no se resuelve silenciosamente al primer segmento (D2 fail-fast)."""

    async def scenario() -> None:
        adapter = _adapter()
        with pytest.raises(ValueError, match="niches"):
            await adapter.discover(cursor=None, limit=100, section="/niches/homemade/videos")
        await adapter.aclose()

    _run(scenario)


def test_discover_section_con_barra_final_se_acepta() -> None:
    """Un '/' final es tolerado (no es un segmento extra real)."""

    async def scenario() -> None:
        adapter = _adapter()
        page = await adapter.discover(cursor=None, limit=100, section="/niches/homemade/")
        assert page.external_ids == ["abchomemadeone", "def-homemade-two"]
        await adapter.aclose()

    _run(scenario)


def test_discover_anti_bucle_aislado_por_nicho() -> None:
    """Regresión (revisión independiente, 2026-08-20): el anti-bucle de un
    nicho no contamina el de otro en la misma instancia (singleton
    compartido por todos los jobs de la fuente, PR-067).

    `other-niche` devuelve deliberadamente los MISMOS IDs que la página 1 de
    `homemade` (ver `_fixture_handler`): antes del fix, como
    `_seen_external_ids` era un único set compartido, la primera página de
    `other-niche` se habría visto como "0 IDs nuevos" (contaminada por
    `homemade`) y habría cortado la cadena en el acto (`next_cursor=None`).
    """

    async def scenario() -> None:
        adapter = _adapter()
        first = await adapter.discover(cursor=None, limit=100, section="/niches/homemade")
        assert first.external_ids == ["abchomemadeone", "def-homemade-two"]
        assert first.next_cursor == "2"

        other = await adapter.discover(cursor=None, limit=100, section="/niches/other-niche")
        assert other.external_ids == ["abchomemadeone", "def-homemade-two"]
        assert other.next_cursor == "2"  # NO "0 IDs nuevos" por culpa de homemade
        await adapter.aclose()

    _run(scenario)


def test_get_video_cache_sobrevive_a_dos_lecturas_pipeline_real() -> None:
    """Regresión (revisión independiente, 2026-08-20): el pipeline real llama
    a `get_video` DOS veces por vídeo (dentro de `discover()` y de nuevo en
    el job `FETCH_METADATA` posterior) — la cache NO puede consumirse de un
    solo uso; ambas lecturas deben usarla sin GET adicional."""

    async def scenario() -> None:
        requested: list[str] = []
        adapter = _adapter(_tracking_handler(requested, _fixture_handler()))
        await adapter.discover(cursor=None, limit=100, section="/niches/homemade")
        before = len(requested)
        first = await adapter.get_video("AbcHomemadeOne")
        second = await adapter.get_video("AbcHomemadeOne")
        assert first is not None and second is not None
        assert first.external_id == second.external_id == "abchomemadeone"
        assert len(requested) == before  # ninguna de las dos hizo un GET adicional
        await adapter.aclose()

    _run(scenario)


def test_gif_cache_se_acota_con_evicción_fifo() -> None:
    """Regresión (revisión independiente, 2026-08-20): `_gif_cache` no crece
    sin límite en un worker de larga duración — al superar el tope, la
    entrada más antigua se descarta primero (FIFO)."""
    from xtrace_crawler.adapters.redgifs import _GIF_CACHE_MAX_ENTRIES

    adapter = _adapter()
    base_video = VideoSource(
        source="redgifs",
        external_id="placeholder",
        page_url="https://www.redgifs.com/watch/placeholder",
    )
    for index in range(_GIF_CACHE_MAX_ENTRIES + 5):
        adapter._cache_gif(f"id-{index:05d}", base_video)
    assert len(adapter._gif_cache) == _GIF_CACHE_MAX_ENTRIES
    assert "id-00000" not in adapter._gif_cache  # las 5 más antiguas se descartaron
    assert "id-00004" not in adapter._gif_cache
    assert "id-00005" in adapter._gif_cache
    assert f"id-{_GIF_CACHE_MAX_ENTRIES + 4:05d}" in adapter._gif_cache


def test_discover_usa_count_igual_al_limit_cuando_limit_menor_que_100() -> None:
    """Regresión (validación real 2026-08-20, PR-069): `count` debe seguir a
    `limit`, nunca quedar fijo en 100. Un `count=100` fijo con el `limit`
    real usado por el CLI (`backfill_default_limit=50`, sin `--limit`
    explícito) hacía que la API devolviera 100 IDs y el adapter los
    rechazara como "más IDs que limit" (`RedgifsParseError`) — bug
    encontrado en el primer backfill real contra `/niches/homemade`.
    """
    requested: list[str] = []

    async def scenario() -> None:
        adapter = _adapter(_tracking_handler(requested, _fixture_handler()))
        await adapter.discover(cursor=None, limit=50, section="/niches/homemade")
        await adapter.aclose()

    _run(scenario)
    listing_calls = [url for url in requested if "/v2/niches/" in url]
    assert listing_calls, requested
    assert "count=50" in listing_calls[0]


def test_discover_usa_count_100_como_tope_maximo_con_limit_mayor() -> None:
    """Con `limit` >= 100, `count` se acota a 100 (máximo aceptado por la API)."""
    requested: list[str] = []

    async def scenario() -> None:
        adapter = _adapter(_tracking_handler(requested, _fixture_handler()))
        await adapter.discover(cursor=None, limit=500, section="/niches/homemade")
        await adapter.aclose()

    _run(scenario)
    listing_calls = [url for url in requested if "/v2/niches/" in url]
    assert listing_calls, requested
    assert "count=100" in listing_calls[0]


# ---------------------------------------------------------------------------
# FR-004 · get_video()
# ---------------------------------------------------------------------------


def test_get_video_desde_discover_usa_cache_sin_segunda_peticion() -> None:
    requested: list[str] = []

    async def scenario() -> None:
        adapter = _adapter(_tracking_handler(requested, _fixture_handler()))
        await adapter.discover(cursor=None, limit=100, section="/niches/homemade")
        before = len(requested)
        video = await adapter.get_video("AbcHomemadeOne")
        assert video is not None
        assert video.external_id == "abchomemadeone"
        assert len(requested) == before  # cache: sin GET adicional
        await adapter.aclose()

    _run(scenario)


def test_get_video_sin_cache_hace_get_directo() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("jklImagePostFour")
        assert video is not None
        assert video.external_id == "jklimagepostfour"
        assert video.duration_ms is None
        await adapter.aclose()

    _run(scenario)


def test_get_video_404_devuelve_none() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("removedItem")
        assert video is None
        await adapter.aclose()

    _run(scenario)


def test_get_video_ignora_page_url_siempre() -> None:
    """Decisión D5: `page_url` nunca se fetchea; el resultado no depende de él."""

    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video(
            "jklImagePostFour", page_url="https://www.redgifs.com/watch/otra-cosa"
        )
        assert video is not None
        assert video.external_id == "jklimagepostfour"
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-005 · get_visual_assets() (SC-004/SC-006)
# ---------------------------------------------------------------------------


def test_get_visual_assets_thumbnail_y_poster_sin_timestamp() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("AbcHomemadeOne")
        assert video is not None
        assets = await adapter.get_visual_assets(video)
        assert len(assets) == 2
        assert all(asset.kind == "thumbnail" for asset in assets)
        assert all(asset.timestamp_ms is None and asset.position is None for asset in assets)
        urls = {asset.url for asset in assets}
        assert urls == {video.thumbnail_url, video.storyboard_urls[0]}
        await adapter.aclose()

    _run(scenario)


def test_get_visual_assets_sin_poster_degrada_a_solo_thumbnail() -> None:
    video = VideoSource(
        source="redgifs",
        external_id="sinposter",
        page_url="https://www.redgifs.com/watch/sinposter",
        thumbnail_url="https://media.redgifs.invalid/sinposter-mobile.jpg",
        storyboard_urls=[],
    )

    async def scenario() -> None:
        adapter = _adapter()
        assets = await adapter.get_visual_assets(video)
        assert len(assets) == 1
        assert assets[0].kind == "thumbnail"
        await adapter.aclose()

    _run(scenario)


def test_get_visual_assets_sin_ninguno_devuelve_vacio() -> None:
    video = VideoSource(
        source="redgifs",
        external_id="sinnada",
        page_url="https://www.redgifs.com/watch/sinnada",
    )

    async def scenario() -> None:
        adapter = _adapter()
        assets = await adapter.get_visual_assets(video)
        assert assets == []
        await adapter.aclose()

    _run(scenario)


def test_get_visual_assets_nunca_expone_mp4() -> None:
    forbidden = ("sd.mp4", "hd.mp4", "silent.mp4")

    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("AbcHomemadeOne")
        assert video is not None
        assets: list[VisualAsset] = await adapter.get_visual_assets(video)
        for asset in assets:
            assert not any(fragment in asset.url for fragment in forbidden)
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# FR-001 · check_availability()
# ---------------------------------------------------------------------------


def test_check_availability_available() -> None:
    async def scenario() -> None:
        adapter = _adapter()
        video = await adapter.get_video("AbcHomemadeOne")
        assert video is not None
        assert await adapter.check_availability(video) == VideoAvailability.AVAILABLE
        await adapter.aclose()

    _run(scenario)


def test_check_availability_removed() -> None:
    video = VideoSource(
        source="redgifs",
        external_id="removeditem",
        page_url="https://www.redgifs.com/watch/removeditem",
    )

    async def scenario() -> None:
        adapter = _adapter()
        assert await adapter.check_availability(video) == VideoAvailability.REMOVED
        await adapter.aclose()

    _run(scenario)


def test_check_availability_unavailable_en_error_no_404() -> None:
    video = VideoSource(
        source="redgifs",
        external_id="cualquiera",
        page_url="https://www.redgifs.com/watch/cualquiera",
    )

    async def scenario() -> None:
        adapter = _adapter()  # el handler por defecto devuelve 500 para paths desconocidos
        assert await adapter.check_availability(video) == VideoAvailability.UNAVAILABLE
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# SEC-005 · token temporal: obtención, renovación ante 401, fallo persistente
# ---------------------------------------------------------------------------


def _renewal_handler(
    *, persistent_failure: bool = False
) -> Callable[[httpx.Request], httpx.Response]:
    """Handler con estado: la 1a petición al gif usa un token viejo → 401;
    tras renovar, la 2a usa el token nuevo → 200 (o también 401 si
    `persistent_failure=True`, para probar el fallo persistente)."""
    calls = {"auth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/auth/temporary":
            calls["auth"] += 1
            token = "old-token" if calls["auth"] == 1 else "new-token"
            return httpx.Response(200, json={"token": token}, request=request)
        if path == "/v2/gifs/abchomemadeone":
            auth = request.headers.get("authorization")
            if auth == "Bearer new-token" and not persistent_failure:
                return httpx.Response(200, json=_fixture_json("gif_object.json"), request=request)
            return httpx.Response(
                401, json={"error": {"code": "AuthenticationError"}}, request=request
            )
        return httpx.Response(500, request=request)

    return handler


def test_token_se_obtiene_una_vez_y_se_reutiliza() -> None:
    requested: list[str] = []

    async def scenario() -> None:
        adapter = _adapter(_tracking_handler(requested, _fixture_handler()))
        await adapter.get_video("AbcHomemadeOne")
        await adapter.get_video("jklImagePostFour")
        await adapter.aclose()

    _run(scenario)
    auth_calls = [url for url in requested if "/v2/auth/temporary" in url]
    assert len(auth_calls) == 1  # una sola obtención de token, reutilizado


def test_token_se_renueva_ante_401_y_completa_la_peticion() -> None:
    async def scenario() -> None:
        adapter = _adapter(_renewal_handler())
        video = await adapter.get_video("AbcHomemadeOne")
        assert video is not None
        assert video.external_id == "abchomemadeone"
        await adapter.aclose()

    _run(scenario)


def test_token_fallo_persistente_se_propaga_sin_fugar_el_valor() -> None:
    async def scenario() -> None:
        adapter = _adapter(_renewal_handler(persistent_failure=True))
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await adapter.get_video("AbcHomemadeOne")
        message = str(exc_info.value)
        assert "old-token" not in message
        assert "new-token" not in message
        await adapter.aclose()

    _run(scenario)


def test_fetch_token_sin_campo_token_lanza_redgifs_auth_error() -> None:
    async def scenario() -> None:
        adapter = _adapter(_json_response({"scope": "read"}))
        with pytest.raises(RedgifsAuthError):
            await adapter.get_video("AbcHomemadeOne")
        await adapter.aclose()

    _run(scenario)


# ---------------------------------------------------------------------------
# SC-007 · Añadir esta fuente no toca el core
# ---------------------------------------------------------------------------


def _imported_module_names(path: Path) -> list[str]:
    """Nombres de módulos importados por un fichero (paridad con xhamster/xvideos)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
            names.extend(alias.name for alias in node.names)
    return names


def test_core_no_importa_el_adapter_redgifs() -> None:
    """SC-007: ningún módulo del core importa el adapter redgifs.

    Añadir esta fuente solo añade ficheros del adapter (y su registro en
    PR-067); el core (`xtrace_crawler` fuera de `adapters/`) no conoce
    `redgifs` todavía en este PR.
    """
    package_root = Path(_crawler_pkg_init).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if "adapters" in path.parts:
            continue
        for module in _imported_module_names(path):
            parts = module.split(".")
            if "redgifs" in parts:
                offenders.append(f"{path.relative_to(package_root)} importa {module}")
    assert offenders == [], f"SC-007 violado: el core importa el adapter redgifs: {offenders}"


# ---------------------------------------------------------------------------
# SEC-004 · fixtures anonimizados
# ---------------------------------------------------------------------------


def test_fixtures_usan_dominio_invalid_sin_media_real() -> None:
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        content = path.read_text(encoding="utf-8")
        if "media.redgifs" in content:
            assert "media.redgifs.invalid" in content, (
                f"SEC-004 violado: {path.name} no usa el dominio reservado .invalid"
            )
        assert "redgifs.com" not in content, f"SEC-004 violado: {path.name} referencia un host real"
