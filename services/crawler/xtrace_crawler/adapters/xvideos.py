"""Adapter de xvideos.com: parsing HTML con selectolax (PR-043 · FR-004 · SEC-001/002 · ADR-0009).

**HABILITADO POR MANIFEST tras revisión humana** (SEC-002, PR-042): el
manifest declara `robots_reviewed=True`, `terms_reviewed=True` y
`review_date="2026-08-16"` — aprobación explícita del operador (modo
prueba). La habilitación **efectiva** sigue exigiendo el gate del registry
(PR-028): manifest conforme Y `sources.enabled=true` en BD (aprobación
humana final en producción, SEC-002); sin `enabled` la fuente se rechaza
con `AdapterNotEnabledError`.

Método de acceso (FR-004): **html** — xvideos no ofrece API/feed oficial ni
sitemap estable para vídeos; el manifest documenta la elección dentro de la
jerarquía api → sitemap → json → html → navegador.

Estructura HTML **observada** (validación real 2026-08-16, PR-033; capturas
del operador en `/tmp/xvideos-probe/` — **nunca copiadas al repo**, SEC-004;
fixtures anonimizados en `tests/fixtures/xvideos/`):

- **Listado/discover**: ítems `a.thumb-link[href^="/video"]` — el ID externo
  es el **primer segmento del path** `/video.<encoded>/<slug>` (p. ej.
  `/video.abc12345/…` → `external_id="video.abc12345"`; el `<encoded>` NO es
  numérico); thumb lazy en `img[data-src]` del CDN
  `thumb-cdn77.xvideos-cdn.com` (ficheros `xv_<N>_t.jpg`); título en
  `div.thumb-under div.title a`. Paginación: `a.dir.next[href]` — el cursor
  es el **path** del enlace siguiente (p. ej. `/best/2026-07/1`), `None` en la
  última página. `/best/1` **redirige** a `/best/2026-07`: el cursor se toma
  de la **URL FINAL de la respuesta** (`response.url`).
- **Página de vídeo**: metadata en `meta[property="og:*"]` — `og:title`,
  `og:url` (page_url), `og:duration` (segundos) y `og:image` (thumbnail);
  fallback `h2.page-title` (sin el `span.duration` interno); fecha y tags en
  el bloque JSON-LD (`<script type="application/ld+json">`: `uploadDate` ISO
  tz-aware → `published_at`, `keywords` → tags, máx. 20). El reproductor
  carga una **galería de thumbnails** `xv_1_t.jpg … xv_N_t.jpg` (URLs del CDN
  `thumb-cdn77.xvideos-cdn.com`, `position=N`); existe mp4 completo
  (`html5player.setVideoUrlLow`) pero está **PROHIBIDO** exponerlo o
  descargarlo (SC-006) → `preview_url` queda siempre `None`. No se detectó
  sprite/storyboard real (solo `mozaique_*.jpg`, sin grid conocido) →
  `storyboard_urls` vacío y el manifest declara solo `["thumbnail"]`.

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

#: Allowlist de hosts de assets de xvideos (PR-040 · SEC-001 · contracts §1).
#: **PROVISIONAL** — actualizada a los hosts OBSERVADOS en la validación real
#: de 2026-08-16 (PR-033): el CDN de thumbnails/galería
#: (`thumb-cdn77.xvideos-cdn.com`), el CDN de assets del reproductor
#: (`assets-cdn77.xvideos-cdn.com`, visto en los `<script>` de la página) y
#: los dominios de página. Se **quitaron** los hosts inventados de la
#: estructura asumida (`thumbs2.xvideos.com`, `cdn77.io`, patrones
#: `th-01`/`vd-01`). `thumbs-gcore.xvideos-cdn.com` aparece solo en los thumbs
#: de vídeos **relacionados** (no se usan como assets del vídeo actual) y no
#: está en la lista: si un backfill real lo necesita, se amplía aquí y los
#: assets fuera de la lista degradan sin red (fail-closed).
XV_ASSET_HOSTS: list[str] = [
    "xvideos.com",
    "www.xvideos.com",
    "thumb-cdn77.xvideos-cdn.com",  # CDN de thumbnails/galería (observado, PR-033)
    "assets-cdn77.xvideos-cdn.com",  # CDN de assets del reproductor (observado, PR-033)
]

XV_BASE_URL = "https://www.xvideos.com"
# El ID externo incluye el prefijo `video.` (p. ej. `video.abc12345`).
XV_VIDEO_URL_TEMPLATE = "https://www.xvideos.com/{external_id}/"

# Selectores clave (regresión de estructura: si cambian, los tests de los
# fixtures fallan con mensaje claro).
_LISTING_ITEM_SELECTOR = "a.thumb-link[href^='/video']"
_LISTING_NEXT_SELECTOR = "a.dir.next[href]"
_VIDEO_TITLE_SELECTOR = "h2.page-title"
_OG_TITLE_SELECTOR = "meta[property='og:title']"
_OG_URL_SELECTOR = "meta[property='og:url']"
_OG_DURATION_SELECTOR = "meta[property='og:duration']"
_OG_IMAGE_SELECTOR = "meta[property='og:image']"

# Patrón del ID externo en hrefs/URLs: /video.<encoded>/... (o /video.<encoded>).
# El <encoded> NO es numérico (estructura real, PR-033): letras minúsculas + dígitos.
_VIDEO_EXTERNAL_ID_RE = re.compile(r"^/video\.(?P<encoded>[a-z0-9]+)(?:/|$)")

# Bloque JSON-LD de la página de vídeo (VideoObject: uploadDate/keywords).
# Acepta type con comillas dobles o simples.
_JSONLD_RE = re.compile(r'<script type=["\']application/ld\+json["\']>(.*?)</script>', re.DOTALL)

#: Máximo de tags expuestos desde `keywords` del JSON-LD (PR-043).
_MAX_TAGS: int = 20


class XvideosParseError(ValueError):
    """Error de parseo/contrato del adapter xvideos.

    Se lanza cuando no se puede identificar siquiera el patrón de vídeo
    (og:url ni page_url con `/video.<encoded>/`) — la señal de "el HTML
    cambió" para que el job quede `failed` con un error legible (edge case de
    la spec) — o cuando `discover()` recibe una página con más IDs que
    `limit` (truncación no soportada: el llamador debe ajustar su lote, nunca
    se pierden IDs en silencio). Los campos opcionales ausentes no lanzan
    error (degradación).
    """


# ---------------------------------------------------------------------------
# Helpers de parseo (funciones puras: sin red, testables sobre fixtures)
# ---------------------------------------------------------------------------


def _external_id_from_url(url: str) -> str | None:
    """Extrae el ID externo de una URL/path `/video.<encoded>/...` (o None).

    El ID externo es el primer segmento del path, **incluyendo** el prefijo
    `video.` (p. ej. `video.abc12345`) — estructura real observada (PR-033).
    """
    match = _VIDEO_EXTERNAL_ID_RE.match(urlsplit(url).path)
    if match is None:
        return None
    return f"video.{match.group('encoded')}"


def _cursor_from_href(href: str) -> str:
    """Normaliza el href del enlace siguiente a un cursor (path)."""
    if href.startswith("/"):
        return href
    return urlsplit(href).path or "/"


def _meta_content(tree: HTMLParser, selector: str) -> str | None:
    """Contenido de un `meta[property]` (og:*) o None si no está."""
    node = tree.css_first(selector)
    if node is None:
        return None
    content = node.attributes.get("content")
    return content if isinstance(content, str) and content else None


def _jsonld(html: str) -> dict[str, Any] | None:
    """Devuelve el dict JSON-LD de la página (VideoObject), o None si no es parseable.

    Acepta un objeto directo o un array cuyo primer elemento sea un dict
    (variantes de schema.org); JSON inválido → None (degradación).
    """
    match = _JSONLD_RE.search(html)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    return data if isinstance(data, dict) else None


def parse_listing_page(html: str, *, current_path: str | None = None) -> DiscoverPage:
    """Parsea una página de listado/discover: IDs externos (dedup) + cursor.

    `current_path` es el path de la **URL FINAL de la respuesta** (tras
    redirects, `response.url`): si el enlace `a.dir.next` apunta al mismo
    path, la paginación se considera terminada (`next_cursor=None`) — parte de
    la protección anti-bucle (PR-043).

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
            candidate = _cursor_from_href(href)
            # Anti-bucle: un `dir.next` que apunta a la página actual es fin.
            if current_path is None or candidate != current_path:
                next_cursor = candidate
    return DiscoverPage(external_ids=external_ids, next_cursor=next_cursor)


def parse_video_page(html: str, *, page_url: str) -> VideoSource:
    """Parsea una página de vídeo a `VideoSource` normalizado (FR-002 · PR-043).

    Estructura observada (PR-033): `og:title`/`og:url`/`og:duration`/
    `og:image`, fallback `h2.page-title`, y `uploadDate`/`keywords` del
    JSON-LD. El mp4 completo (`html5player.setVideoUrlLow`) NO se expone:
    `preview_url` queda siempre `None` (SC-006). Sin sprite real detectado →
    `storyboard_urls` vacío.

    El ID externo se toma del `og:url` (patrón `/video.<encoded>/`); si no
    hay og:url, se deduce de `page_url`. Sin patrón → `XvideosParseError` con
    mensaje claro (regresión de estructura).
    """
    tree = HTMLParser(html)

    title = _meta_content(tree, _OG_TITLE_SELECTOR)
    if title is None:
        title_node = tree.css_first(_VIDEO_TITLE_SELECTOR)
        if title_node is not None:
            title = title_node.text(strip=True) or None
            if title is not None:
                # El `h2.page-title` real incluye un `span.duration` ("14 min").
                duration_node = title_node.css_first("span.duration")
                if duration_node is not None:
                    title = title.replace(duration_node.text(strip=True) or "", "").strip() or None

    page_url_from_og = _meta_content(tree, _OG_URL_SELECTOR)

    # Guarda anti-regresión: una página sin ninguna señal de vídeo (og:title,
    # og:url ni h2.page-title) no es una página de vídeo (p. ej.
    # captcha/anti-bot o HTML cambiado): fallamos con error claro en vez de
    # devolver un vídeo vacío (SEC-001: no se intenta saltar protecciones;
    # edge case "HTML cambia").
    if page_url_from_og is None and title is None:
        raise XvideosParseError(
            "página sin señales de vídeo (og:title/og:url ni h2.page-title); "
            "¿cambió la estructura HTML de xvideos o hay una página de protección?"
        )

    external_id = _external_id_from_url(page_url_from_og) if page_url_from_og else None
    if external_id is None:
        external_id = _external_id_from_url(page_url)
    if external_id is None:
        raise XvideosParseError(
            "no se encontró el patrón de vídeo /video.<encoded>/ (ni og:url ni page_url); "
            "¿cambió la estructura HTML de xvideos?"
        )

    duration_ms: int | None = None
    duration_raw = _meta_content(tree, _OG_DURATION_SELECTOR)
    if duration_raw is not None:
        try:
            duration_s = int(duration_raw)
        except ValueError:
            duration_s = None
        if duration_s is not None and duration_s >= 0:
            duration_ms = duration_s * 1000

    data = _jsonld(html) or {}

    published_at: datetime | None = None
    upload_date = data.get("uploadDate")
    if isinstance(upload_date, str) and upload_date:
        try:
            parsed = datetime.fromisoformat(upload_date)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:  # ISO sin offset → asumimos UTC (tz-aware)
                parsed = parsed.replace(tzinfo=UTC)
            published_at = parsed

    tags: list[str] = []
    keywords = data.get("keywords")
    if isinstance(keywords, str) and keywords:
        tags = [tag.strip() for tag in keywords.split(",") if tag.strip()]
    elif isinstance(keywords, list):
        tags = [tag for tag in keywords if isinstance(tag, str) and tag]
    tags = list(dict.fromkeys(tags))[:_MAX_TAGS]

    return VideoSource(
        source="xvideos",
        external_id=external_id,
        title=title,
        page_url=(
            page_url_from_og if page_url_from_og is not None else f"{XV_BASE_URL}/{external_id}/"
        ),
        duration_ms=duration_ms,
        thumbnail_url=_meta_content(tree, _OG_IMAGE_SELECTOR),
        preview_url=None,  # SC-006: el mp4 completo (setVideoUrlLow) está PROHIBIDO
        storyboard_urls=[],  # sin sprite real detectado (PR-043); la galería son thumbnails
        tags=tags,
        published_at=published_at,
    )


def _thumb_gallery(
    html: str, *, thumbnail_url: str | None, duration_ms: int | None
) -> list[VisualAsset]:
    """Galería de thumbnails `xv_<N>_t.jpg` del reproductor (PR-043 · FR-005/SC-006).

    Busca en el HTML/JSON de la página las URLs `xv_<N>_t.jpg` del **mismo
    path CDN** que `thumbnail_url` (og:image): así los thumbs de los vídeos
    **relacionados** (otros UUIDs) quedan fuera. Soporta URLs JSON-escapadas
    (backslash-barra) como aparecen en los scripts del reproductor.

    `kind="thumbnail"`, `position=N` y `timestamp_ms` aproximado
    `round(N / (total + 1) * duration_ms)` (la fuente no expone una referencia
    temporal fiable por thumb). `thumbnail_url` sin el patrón `xv_<N>_t.jpg` o
    sin galería → lista vacía (el llamador degrada a la miniatura única).
    """
    if thumbnail_url is None:
        return []
    marker = thumbnail_url.rfind("xv_")
    if marker < 0:
        return []
    prefix = thumbnail_url[: marker + len("xv_")]

    # Las URLs de los scripts del reproductor llegan JSON-escapadas (`\/`).
    unescaped = html.replace("\\/", "/")
    by_position: dict[int, str] = {}
    for match in re.finditer(re.escape(prefix) + r"(\d+)_t\.jpg", unescaped):
        by_position.setdefault(int(match.group(1)), match.group(0))
    if not by_position:
        return []

    total = len(by_position)
    assets: list[VisualAsset] = []
    for position in sorted(by_position):
        timestamp_ms: int | None = None
        if duration_ms is not None:
            timestamp_ms = round(position / (total + 1) * duration_ms)
        assets.append(
            VisualAsset(
                kind="thumbnail",
                url=by_position[position],
                position=position,
                timestamp_ms=timestamp_ms,
            )
        )
    return assets


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class XvideosAdapter:
    """Adapter real de xvideos.com (HTML, galería de thumbnails) — FR-004.

    Cumple el protocolo `SourceAdapter` (FR-001, ADR-0009). **Manifest
    revisado** (SEC-002, PR-042): `robots_reviewed=True`,
    `terms_reviewed=True`, `review_date="2026-08-16"` (aprobación del
    operador, modo prueba); la habilitación efectiva sigue dependiendo de
    `sources.enabled=true` en BD (gate del registry: manifest + enabled).
    Sin red en tests: se inyecta un `httpx.MockTransport` y toda petición
    pasa por el cliente HTTP seguro con allowlist (SEC-001).

    **PR-040/043 · SEC-001**: declara `asset_hosts` (**PROVISIONAL**, ver
    `XV_ASSET_HOSTS`): allowlist de hosts de sus assets actualizada a los
    hosts **observados** en la validación real de 2026-08-16 (PR-033); el
    pipeline (PR-036) la usa para la descarga por HTTP — validar de nuevo en
    los backfills reales.
    """

    manifest = AdapterManifest(
        source="xvideos",
        access_method="html",  # FR-004: jerarquía api → sitemap → json → html → navegador
        assets_accessed=["thumbnail"],  # PR-043: galería xv_N_t.jpg; sin sprite real (SC-006)
        robots_reviewed=True,  # SEC-002: revisión robots del operador (2026-08-16, modo prueba)
        terms_reviewed=True,  # SEC-002: revisión ToS del operador (2026-08-16, modo prueba)
        rate_limit=RateLimitSpec(min_interval_ms=2_000, max_rps=0.5),  # conservador (D5)
        review_date="2026-08-16",  # SEC-002: aprobación humana explícita (operador, modo prueba)
    )

    # PR-040/043 · SEC-001 · contracts §1: allowlist de hosts de assets
    # (PROVISIONAL — hosts observados 2026-08-16, validar en backfills reales);
    # el pipeline la usa como allowlist del cliente de assets, nunca derivada
    # de las URLs parseadas (fail-closed).
    asset_hosts: list[str] = XV_ASSET_HOSTS

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Crea el adapter con su cliente HTTP seguro (allowlist de hosts).

        Args:
            transport: transporte inyectable (`httpx.MockTransport` en tests);
                `None` → red real (solo uso operativo tras habilitación).
        """
        self._client = SafeHTTPClient(allowed_hosts=XV_VIDEO_HOSTS, transport=transport)
        # Protección anti-bucle de discover (PR-043): IDs ya vistos en esta
        # instancia (una instancia por proceso de worker vía el registry). Se
        # reinicia al arrancar una cadena nueva (cursor=None).
        self._seen_external_ids: set[str] = set()

    # -- FR-004 · discover ----------------------------------------------------

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
        """Descubre IDs externos de una página de listado (FR-004 · PR-043).

        `cursor` es el path de la página siguiente (None → primera página).
        `limit` debe ser >= tamaño de página: **la truncación no está
        soportada** — si la página trae más IDs que `limit`, se lanza
        `XvideosParseError` indicando los tamaños reales (nunca se descartan
        IDs en silencio ni se repite el cursor recibido).

        **Protección anti-bucle (hallazgo de la validación real, PR-033)**:
        - el cursor se toma de la URL **FINAL** de la respuesta
          (`response.url` tras redirects: `/best/1` → `/best/2026-07`);
        - si el `a.dir.next` repite el path actual → `next_cursor=None`;
        - si la página devuelve 0 IDs **nuevos** (no vistos en esta
          instancia) → `next_cursor=None` (fin de la cadena).
        """
        if cursor is None:
            # Nueva cadena de paginación: se reinicia el conjunto de vistos.
            self._seen_external_ids.clear()
        url = f"{XV_BASE_URL}/" if cursor is None else f"{XV_BASE_URL}{cursor}"
        response = await self._client.get(url)
        response.raise_for_status()
        current_path = urlsplit(str(response.url)).path
        page = parse_listing_page(response.text, current_path=current_path)
        if len(page.external_ids) > limit:
            raise XvideosParseError(
                f"la página trae {len(page.external_ids)} IDs con limit={limit}; "
                "limit debe ser >= tamaño de página (truncación no soportada)"
            )
        new_ids = [
            external_id
            for external_id in page.external_ids
            if external_id not in self._seen_external_ids
        ]
        self._seen_external_ids.update(page.external_ids)
        if not new_ids:
            # Anti-bucle: 0 IDs nuevos → fin (no se encola la siguiente página).
            return DiscoverPage(external_ids=page.external_ids, next_cursor=None)
        return page

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
        """Devuelve los assets visuales declarados en el manifest (FR-005/SC-006 · PR-043).

        La **galería de thumbnails** `xv_<N>_t.jpg` del reproductor se parsea
        del HTML/JSON de la página de vídeo (los datos del reproductor no
        viajan en el `VideoSource` del contrato, así que se re-fetcha la
        página — 1 request extra por vídeo, ver handoff). Sin galería, degrada
        a la miniatura única `thumbnail_url` (jerarquía de assets, FR-005);
        nunca un mp4 completo (SC-006).
        """
        response = await self._client.get(
            XV_VIDEO_URL_TEMPLATE.format(external_id=video.external_id)
        )
        response.raise_for_status()
        assets = _thumb_gallery(
            response.text, thumbnail_url=video.thumbnail_url, duration_ms=video.duration_ms
        )
        if not assets and video.thumbnail_url is not None:
            assets = [VisualAsset(kind="thumbnail", url=video.thumbnail_url)]
        return assets

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
