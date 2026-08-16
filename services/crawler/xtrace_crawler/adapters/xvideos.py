"""Adapter de xvideos.com: parsing HTML con selectolax (PR-031 · FR-004 · SEC-001/002 · ADR-0009).

**DESHABILITADO POR DISEÑO** (SEC-002): el manifest declara
`robots_reviewed=False`, `terms_reviewed=False` y `review_date=None` — el
registry (PR-028) no podrá habilitar esta fuente hasta la revisión
legal/ToS/robots del humano y la aprobación explícita (PR-033).

Método de acceso (FR-004): **html** — xvideos no ofrece API/feed oficial ni
sitemap estable para vídeos; el manifest documenta la elección dentro de la
jerarquía api → sitemap → json → html → navegador.

Estructura HTML asumida (documentada en `tests/fixtures/xvideos/README.md`;
la captura real la hará el operador en PR-033):

- **Listado/discover**: ítems `div.thumb a[href^="/video"]` (ID externo en el
  href, regex `^/video(?P<id>\\d+)`, dedup preservando orden); paginación
  `div.pagination a.next-page[href]` → el **cursor es el path** del enlace
  siguiente (p. ej. `/best/2`), `None` en la última página.
- **Página de vídeo**: ID del `link[rel="canonical"]` (patrón `/video<id>/`);
  título `h2.page-title`; duración/thumbnail/sprite/preview/timestamp del
  bloque JSON `flashvars` embebido en un `<script>` (claves `duration` en
  segundos, `timestamp` unix, `thumb_url`, `thumb_sprite`, `preview_video`);
  tags de `div.video-tags-list a`. `flashvars` es el canal estable del
  reproductor (misma técnica que los extractores públicos); si falta o es JSON
  inválido, los campos opcionales quedan `None` (spec: metadatos incompletos no
  bloquean el vídeo).

Sin red en tests: toda petición pasa por `SafeHTTPClient` (PR-024, allowlist
`xvideos.com`/`www.xvideos.com`, SEC-001) con un `httpx.MockTransport`
inyectable; el adapter nunca construye URLs de assets propios más allá de lo
parseado (anti-SSRF).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.crawling.http import SafeHTTPClient

# Hosts permitidos para este adapter (SEC-001, anti-SSRF): solo xvideos.com y
# su subdominio www (los redirects entre ambos están permitidos).
XV_VIDEO_HOSTS = frozenset({"xvideos.com", "www.xvideos.com"})

XV_BASE_URL = "https://www.xvideos.com"
XV_VIDEO_URL_TEMPLATE = "https://www.xvideos.com/video{external_id}/"

# Selectores clave (regresión de estructura: si cambian, los tests de los
# fixtures fallan con mensaje claro).
_LISTING_ITEM_SELECTOR = "div.thumb a[href^='/video']"
_LISTING_NEXT_SELECTOR = "div.pagination a.next-page[href]"
_VIDEO_CANONICAL_SELECTOR = "link[rel='canonical']"
_VIDEO_TITLE_SELECTOR = "h2.page-title"
_VIDEO_TAGS_SELECTOR = "div.video-tags-list a"

# Patrón del ID externo en hrefs/canonical: /video<ID>/... (o /video<ID>).
_VIDEO_EXTERNAL_ID_RE = re.compile(r"^/video(?P<external_id>\d+)(?:/|$)")

# Bloque `flashvars` del reproductor: `var flashvars = { ... };`
_FLASHVARS_RE = re.compile(r"flashvars\s*=\s*(\{.*?\})\s*;", re.DOTALL)


class XvideosParseError(ValueError):
    """La estructura HTML de la página no coincide con la esperada.

    Se lanza solo cuando no se puede identificar siquiera el patrón de vídeo
    (canonical o `/video<id>/`): es la señal de "el HTML cambió" para que el
    job quede `failed` con un error legible (edge case de la spec). Los campos
    opcionales ausentes no lanzan error (degradación).
    """


# ---------------------------------------------------------------------------
# Helpers de parseo (funciones puras: sin red, testables sobre fixtures)
# ---------------------------------------------------------------------------


def _flashvars(html: str) -> dict[str, Any] | None:
    """Devuelve el dict `flashvars` del reproductor, o None si no es parseable."""
    match = _FLASHVARS_RE.search(html)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _str_field(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _int_field(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _external_id_from_url(url: str) -> str | None:
    """Extrae el ID externo de una URL/path `/video<ID>/...` (o None)."""
    match = _VIDEO_EXTERNAL_ID_RE.match(urlsplit(url).path)
    return match.group("external_id") if match else None


def _cursor_from_href(href: str) -> str:
    """Normaliza el href del enlace siguiente a un cursor (path)."""
    if href.startswith("/"):
        return href
    return urlsplit(href).path or "/"


def parse_listing_page(html: str) -> DiscoverPage:
    """Parsea una página de listado/discover: IDs externos (dedup) + cursor.

    Si la estructura cambia y no hay ítems, devuelve una página vacía sin
    crashear (aislamiento SC-008); los tests de regresión sobre los fixtures
    señalan el cambio de selector.
    """
    tree = HTMLParser(html)
    external_ids: list[str] = []
    for node in tree.css(_LISTING_ITEM_SELECTOR):
        href = node.attributes.get("href")
        if not href:
            continue
        external_id = _external_id_from_url(href)
        if external_id is not None:
            external_ids.append(external_id)
    external_ids = list(dict.fromkeys(external_ids))  # dedup preservando orden

    next_cursor: str | None = None
    next_node = tree.css_first(_LISTING_NEXT_SELECTOR)
    if next_node is not None:
        href = next_node.attributes.get("href")
        if href:
            next_cursor = _cursor_from_href(href)
    return DiscoverPage(external_ids=external_ids, next_cursor=next_cursor)


def parse_video_page(html: str, *, page_url: str) -> VideoSource:
    """Parsea una página de vídeo a `VideoSource` normalizado (FR-002).

    El ID externo se toma del canonical (patrón `/video<id>/`); si no hay
    canonical, se deduce de `page_url`. Sin patrón → `XvideosParseError` con
    mensaje claro (regresión de estructura).
    """
    tree = HTMLParser(html)

    canonical: str | None = None
    canonical_node = tree.css_first(_VIDEO_CANONICAL_SELECTOR)
    if canonical_node is not None:
        canonical = canonical_node.attributes.get("href")

    data_raw = _flashvars(html)
    data = data_raw or {}

    title: str | None = None
    title_node = tree.css_first(_VIDEO_TITLE_SELECTOR)
    if title_node is not None:
        title = title_node.text(strip=True) or None

    # Guarda anti-regresión: una página sin ninguna señal de vídeo (canonical,
    # título ni flashvars) no es una página de vídeo (p. ej. captcha/anti-bot
    # o HTML cambiado): fallamos con error claro en vez de devolver un vídeo
    # vacío (SEC-001: no se intenta saltar protecciones; edge case "HTML cambia").
    if canonical is None and title is None and data_raw is None:
        raise XvideosParseError(
            "página sin señales de vídeo (canonical, título ni flashvars); "
            "¿cambió la estructura HTML de xvideos o hay una página de protección?"
        )

    external_id = _external_id_from_url(canonical) if canonical else None
    if external_id is None:
        external_id = _external_id_from_url(page_url)
    if external_id is None:
        raise XvideosParseError(
            "no se encontró el patrón de vídeo /video<id>/ (ni canonical ni page_url); "
            "¿cambió la estructura HTML de xvideos?"
        )

    tags: list[str] = []
    for node in tree.css(_VIDEO_TAGS_SELECTOR):
        tag = node.text(strip=True)
        if tag:
            tags.append(tag)
    tags = list(dict.fromkeys(tags))

    duration_s = _int_field(data, "duration")
    duration_ms = duration_s * 1000 if duration_s is not None else None

    timestamp = _int_field(data, "timestamp")
    published_at: datetime | None = None
    if timestamp is not None:
        try:
            published_at = datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            published_at = None

    thumb_sprite = _str_field(data, "thumb_sprite")
    storyboard_urls = [thumb_sprite] if thumb_sprite is not None else []

    return VideoSource(
        source="xvideos",
        external_id=external_id,
        title=title,
        page_url=canonical if canonical is not None else f"{XV_BASE_URL}/video{external_id}/",
        duration_ms=duration_ms,
        thumbnail_url=_str_field(data, "thumb_url"),
        preview_url=_str_field(data, "preview_video"),
        storyboard_urls=storyboard_urls,
        tags=tags,
        published_at=published_at,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class XvideosAdapter:
    """Adapter real de xvideos.com (HTML, storyboard/sprite + thumbnails) — FR-004.

    Cumple el protocolo `SourceAdapter` (FR-001, ADR-0009). **Deshabilitado por
    diseño** (SEC-002): sin revisión legal no es habilitable. Sin red en tests:
    se inyecta un `httpx.MockTransport` y toda petición pasa por el cliente
    HTTP seguro con allowlist (SEC-001).
    """

    manifest = AdapterManifest(
        source="xvideos",
        access_method="html",  # FR-004: jerarquía api → sitemap → json → html → navegador
        assets_accessed=["storyboard", "thumbnail"],  # SC-006: nunca el vídeo completo
        robots_reviewed=False,
        terms_reviewed=False,
        rate_limit=RateLimitSpec(min_interval_ms=2_000, max_rps=0.5),  # conservador (D5)
        review_date=None,
    )

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Crea el adapter con su cliente HTTP seguro (allowlist de hosts).

        Args:
            transport: transporte inyectable (`httpx.MockTransport` en tests);
                `None` → red real (solo uso operativo tras habilitación, PR-033).
        """
        self._client = SafeHTTPClient(allowed_hosts=XV_VIDEO_HOSTS, transport=transport)

    # -- FR-004 · discover ----------------------------------------------------

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
        """Descubre IDs externos de una página de listado (FR-004).

        `cursor` es el path de la página siguiente (None → primera página).
        `limit` acota los IDs devueltos; si la página contiene más, `next_cursor`
        repite el cursor recibido para que el llamador pueda pedir el resto sin
        perder IDs (contrato documentado: repetir mientras se reciban
        exactamente `limit` IDs; avanzar con el cursor nuevo al recibir menos).
        """
        url = f"{XV_BASE_URL}/" if cursor is None else f"{XV_BASE_URL}{cursor}"
        response = await self._client.get(url)
        response.raise_for_status()
        page = parse_listing_page(response.text)
        truncated = len(page.external_ids) > limit
        return DiscoverPage(
            external_ids=page.external_ids[:limit],
            next_cursor=page.next_cursor if not truncated else cursor,
        )

    # -- FR-004 · get_video ---------------------------------------------------

    async def get_video(self, external_id: str) -> VideoSource | None:
        """Obtiene la metadata normalizada de un vídeo (FR-004).

        `None` solo para 404 (vídeo retirado, edge case de la spec); cualquier
        otro error HTTP o de estructura se propaga para que la capa de jobs lo
        reintente o lo marque `failed` con la causa.
        """
        url = XV_VIDEO_URL_TEMPLATE.format(external_id=external_id)
        response = await self._client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return parse_video_page(response.text, page_url=str(response.url))

    # -- FR-005 · get_visual_assets (SC-006) -----------------------------------

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        """Devuelve los assets visuales declarados en el manifest (FR-005/SC-006).

        Solo se ofrecen kinds incluidos en `manifest.assets_accessed`: el
        `preview_url` parseado queda como metadata del `VideoSource` pero no se
        expone como asset mientras el manifest no declare `preview` (SEC-002);
        basta actualizar el manifest cuando la revisión legal lo apruebe.
        """
        assets: list[VisualAsset] = []
        for url in video.storyboard_urls:
            assets.append(VisualAsset(kind="storyboard", url=url))
        if video.thumbnail_url is not None:
            assets.append(VisualAsset(kind="thumbnail", url=video.thumbnail_url))
        if video.preview_url is not None:
            assets.append(VisualAsset(kind="preview", url=video.preview_url))
        allowed = set(self.manifest.assets_accessed)
        return [asset for asset in assets if asset.kind in allowed]

    # -- FR-001 · check_availability ------------------------------------------

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        """Comprueba la disponibilidad del vídeo (FR-001).

        404 → `removed` (terminal, sin reintentos); página válida → `available`;
        cualquier otra cosa (error HTTP, estructura cambiada) → `unavailable`
        (no se puede confirmar ahora).
        """
        url = XV_VIDEO_URL_TEMPLATE.format(external_id=video.external_id)
        response = await self._client.get(url)
        if response.status_code == 404:
            return VideoAvailability.REMOVED
        try:
            response.raise_for_status()
            parse_video_page(response.text, page_url=str(response.url))
        except (httpx.HTTPError, XvideosParseError):
            return VideoAvailability.UNAVAILABLE
        return VideoAvailability.AVAILABLE

    # -- Ciclo de vida ---------------------------------------------------------

    async def aclose(self) -> None:
        """Cierra el cliente HTTP subyacente."""
        await self._client.aclose()
