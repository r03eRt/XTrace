"""Adapter de erome.com: parsing HTML con selectolax (FR-004 · SEC-001/002 · ADR-0009).

**Revisión de compliance (SEC-002, previa a este adapter)**:

- `robots.txt` (`https://www.erome.com/robots.txt`) solo bloquea `/cams/`;
  las páginas de búsqueda (`/search?q=...`) y de álbum (`/a/<id>`) no están
  bloqueadas para `User-agent: *`.
- Términos de servicio (`https://www.erome.com/s/terms`, comprobado
  manualmente): no contienen ninguna cláusula de prohibición de scraping,
  bots o extracción automatizada (a diferencia de otras fuentes candidatas
  descartadas por esa razón). Sí prohíben contenido con menores.

El manifest declara `robots_reviewed=True`/`terms_reviewed=True` con la fecha
de esta revisión; la habilitación EFECTIVA sigue exigiendo el gate del
registry (`sources.enabled=true` en BD, aprobación humana explícita) — igual
que xvideos.

Estructura HTML **observada** (captura real vía `curl`, fuera del repo;
fixtures sintéticos con dominio `.invalid` en `tests/fixtures/erome/`,
SEC-004):

- **Listado** (`/search?q=<término>` o `/search?q=<término>&page=N`): cada
  álbum es un `div.album` con `a.album-link[href="https://www.erome.com/a/<id>"]`;
  el ID externo es el último segmento del path (`/a/<id>`). El título viene
  del `alt` de `img.album-thumbnail` (incluye un sufijo `#<hash>` del nombre
  de fichero del thumbnail que se recorta). Solo se conservan álbumes con
  `span.album-videos` > 0 (los álbumes solo-fotos no son vídeos indexables).
  Paginación: `<a rel="next" href="...">` en `ul.pagination` — el cursor es
  el path+query del enlace; ausente en la última página. `<link rel="next">`
  en `<head>` es equivalente pero no se usa (evita depender del head).
- **Página de álbum** (`/a/<id>`): metadata en `meta[property="og:*"]` —
  `og:title`, `og:url`, `og:image` (miniatura del álbum). Cada clip de vídeo
  vive en un `div.media-group` con un `<video poster="...">` — el `poster` es
  la miniatura pública de ESE clip; el atributo `src` de `<source>` (el mp4)
  **nunca se usa ni se expone** (equivalente a SC-006 de xvideos: sin vídeo
  completo). No hay duración a nivel de álbum en el HTML — `duration_ms`
  queda `None`.
- Un álbum puede agrupar varios clips (colección); este adapter modela el
  álbum completo como **un único vídeo indexable** (limitación conocida,
  documentada aquí a falta de un modelo de "colección" en el core) y expone
  como assets visuales la miniatura del álbum + el `poster` de cada clip
  (deduplicados, tope defensivo `_MAX_ASSETS`).

Sin red en tests: toda petición pasa por `SafeHTTPClient` (allowlist
`erome.com`/`www.erome.com`, SEC-001) con un `httpx.MockTransport` inyectable.
"""

from __future__ import annotations

import re
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
from xtrace_crawler.crawling.http import Resolver, SafeHTTPClient

# Hosts permitidos para este adapter (SEC-001, anti-SSRF).
EROME_VIDEO_HOSTS = frozenset({"erome.com", "www.erome.com"})

#: Allowlist de hosts de assets (SEC-001): dominio de página + CDN de
#: miniaturas observado (`sNN.erome.com`, N variable por álbum/shard). El
#: patrón real usa subdominios numerados (`s1`…`s99` aprox.); se declara el
#: sufijo común y el pipeline valida el host completo contra esta lista, así
#: que se enumeran los prefijos observados en la validación manual. Un asset
#: fuera de esta lista degrada sin red (fail-closed), igual que xvideos.
EROME_ASSET_HOSTS: list[str] = [
    "erome.com",
    "www.erome.com",
    *(f"s{n}.erome.com" for n in range(1, 100)),
]

EROME_BASE_URL = "https://www.erome.com"
EROME_ALBUM_URL_TEMPLATE = "https://www.erome.com/a/{external_id}"

_LISTING_ITEM_SELECTOR = "div.album a.album-link[href]"
_LISTING_NEXT_SELECTOR = "a[rel='next'][href]"
_ALBUM_VIDEOS_COUNT_SELECTOR = "span.album-videos"
_OG_TITLE_SELECTOR = "meta[property='og:title']"
_OG_URL_SELECTOR = "meta[property='og:url']"
_OG_IMAGE_SELECTOR = "meta[property='og:image']"
_MEDIA_VIDEO_SELECTOR = "div.media-group video[poster]"

# ID externo: último segmento no vacío del path `/a/<id>`.
_ALBUM_EXTERNAL_ID_RE = re.compile(r"^/a/(?P<album_id>[^/?#]+)")

#: Tope defensivo de assets visuales devueltos por álbum (miniatura + posters
#: de clips): evita álbumes anómalos con cientos de clips.
_MAX_ASSETS = 24


class EromeParseError(ValueError):
    """Error de parseo/contrato del adapter erome (estructura HTML inesperada)."""


def _resolve_album_href(href: str) -> str | None:
    """Resuelve un href de álbum al host canónico, o `None` si es ajeno (SEC-001).

    Acepta paths relativos (`/a/<id>` → `https://www.erome.com/a/<id>`) y URLs
    absolutas de `erome.com`/`www.erome.com`. Cualquier otro host (p. ej. un
    `og:url` con dominio distinto) devuelve `None`: el llamador cae al
    template `EROME_ALBUM_URL_TEMPLATE` — igual que xvideos (`_resolve_listing_href`),
    el `SafeHTTPClient` con allowlist es la segunda barrera.
    """
    if href.startswith("/"):
        return f"{EROME_BASE_URL}{href}"
    parsed = urlsplit(href)
    if parsed.scheme in ("http", "https") and parsed.netloc in EROME_VIDEO_HOSTS:
        return href
    return None


def _album_page_url(video: VideoSource) -> str:
    """URL canónica de la página de álbum para un `VideoSource` (SEC-001)."""
    url = EROME_ALBUM_URL_TEMPLATE.format(external_id=video.external_id)
    if video.page_url:
        resolved = _resolve_album_href(video.page_url)
        if resolved is not None:
            url = resolved
    return url


# ---------------------------------------------------------------------------
# Helpers de parseo (funciones puras: sin red, testables sobre fixtures)
# ---------------------------------------------------------------------------


def _external_id_from_url(url: str) -> str | None:
    match = _ALBUM_EXTERNAL_ID_RE.match(urlsplit(url).path)
    if match is None:
        return None
    return match.group("album_id")


def _find_album_container(node: Any) -> Any | None:
    """Sube por los ancestros hasta el `div` con clase `album` (o `None`)."""
    current = node
    while current is not None:
        classes = (current.attributes or {}).get("class") or ""
        if "album" in classes.split():
            return current
        current = current.parent
    return None


def parse_listing_page(html: str, *, current_path: str | None) -> DiscoverPage:
    """Parsea una página de listado (`/search?q=...`) en un `DiscoverPage`.

    Solo se conservan álbumes con al menos un vídeo (`span.album-videos`
    presente y > 0); los álbumes solo-fotos se descartan silenciosamente (no
    son vídeos indexables por este core). El cursor de paginación es el
    href de `a[rel='next']`, normalizado a path+query; `None` en la última
    página o si el candidato repite `current_path` (anti-bucle).
    """
    tree = HTMLParser(html)
    external_ids: list[str] = []
    page_urls: dict[str, str] = {}
    seen: set[str] = set()
    for anchor in tree.css(_LISTING_ITEM_SELECTOR):
        href = anchor.attributes.get("href")
        if not href:
            continue
        external_id = _external_id_from_url(href)
        if external_id is None or external_id in seen:
            continue
        container = _find_album_container(anchor)
        videos_node = container.css_first(_ALBUM_VIDEOS_COUNT_SELECTOR) if container else None
        video_count_text = videos_node.text(strip=True) if videos_node else None
        if not video_count_text:
            continue  # álbum solo-fotos: no es un vídeo indexable
        try:
            video_count = int(re.sub(r"[^\d]", "", video_count_text) or "0")
        except ValueError:
            video_count = 0
        if video_count <= 0:
            continue
        seen.add(external_id)
        external_ids.append(external_id)
        page_urls[external_id] = href

    next_node = tree.css_first(_LISTING_NEXT_SELECTOR)
    next_cursor: str | None = None
    if next_node is not None:
        href = next_node.attributes.get("href")
        if href:
            candidate = (
                href
                if href.startswith("/")
                else urlsplit(href).path
                + ("?" + urlsplit(href).query if urlsplit(href).query else "")
            )
            if current_path is None or candidate != current_path:
                next_cursor = candidate

    return DiscoverPage(external_ids=external_ids, next_cursor=next_cursor, page_urls=page_urls)


def parse_album_page(html: str, *, page_url: str) -> VideoSource:
    """Parsea la página de un álbum (`/a/<id>`) en un `VideoSource`.

    `page_url` es la URL de la petición (tras redirects) — solo se usa como
    *fallback* para el ID externo, nunca como única señal de que la página es
    un álbum real. Una página sin `og:title` NI `og:url` (p. ej. un HTML
    cambiado o una página de protección anti-bot) levanta `EromeParseError`
    de inmediato, sin mirar `page_url` (que siempre "parece" válido porque lo
    construimos nosotros mismos a partir del `external_id` pedido).
    """
    tree = HTMLParser(html)
    title_node = tree.css_first(_OG_TITLE_SELECTOR)
    title = title_node.attributes.get("content") if title_node else None
    og_url_node = tree.css_first(_OG_URL_SELECTOR)
    url_from_og = og_url_node.attributes.get("content") if og_url_node else None

    if title is None and url_from_og is None:
        raise EromeParseError(
            "página sin señales de álbum (ni og:title ni og:url); "
            "¿cambió la estructura HTML de erome o hay una página de protección?"
        )

    external_id = _external_id_from_url(url_from_og) if url_from_og else None
    if external_id is None:
        external_id = _external_id_from_url(page_url)
    if external_id is None:
        raise EromeParseError(
            f"no se encontró el patrón /a/<id> (ni og:url ni page_url={page_url!r}); "
            "¿cambió la estructura HTML de erome?"
        )
    resolved_url = url_from_og or page_url
    image_node = tree.css_first(_OG_IMAGE_SELECTOR)
    thumbnail_url = image_node.attributes.get("content") if image_node else None

    return VideoSource(
        source="erome",
        external_id=external_id,
        title=title,
        page_url=resolved_url,
        duration_ms=None,  # no expuesto a nivel de álbum en el HTML observado
        thumbnail_url=thumbnail_url,
        preview_url=None,  # nunca el mp4 completo (equivalente a SC-006)
    )


def _album_visual_assets(html: str, *, thumbnail_url: str | None) -> list[VisualAsset]:
    """Miniatura del álbum + `poster` de cada clip (`div.media-group video[poster]`).

    Deduplicado por URL, con tope defensivo `_MAX_ASSETS`. El primer asset es
    siempre la miniatura del álbum (si existe); el resto son los posters de
    los clips en orden de aparición.
    """
    tree = HTMLParser(html)
    urls: list[str] = []
    if thumbnail_url:
        urls.append(thumbnail_url)
    for node in tree.css(_MEDIA_VIDEO_SELECTOR):
        poster = node.attributes.get("poster")
        if poster:
            urls.append(poster)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
        if len(deduped) >= _MAX_ASSETS:
            break

    return [
        VisualAsset(kind="thumbnail" if i == 0 else "storyboard", url=url, position=i)
        for i, url in enumerate(deduped)
    ]


class EromeAdapter:
    """Adapter real de erome.com (HTML, álbumes con clips de vídeo) — FR-004.

    Cumple el protocolo `SourceAdapter` (FR-001, ADR-0009). Manifest revisado
    (SEC-002): `robots_reviewed=True`, `terms_reviewed=True` — ver docstring
    del módulo para el detalle de la revisión. La habilitación efectiva sigue
    dependiendo de `sources.enabled=true` en BD (gate del registry).
    """

    manifest = AdapterManifest(
        source="erome",
        access_method="html",
        assets_accessed=["thumbnail", "storyboard"],
        robots_reviewed=True,
        terms_reviewed=True,
        rate_limit=RateLimitSpec(min_interval_ms=2_000, max_rps=0.5),  # conservador (D5)
        review_date=None,  # placeholder: fecha real de la revisión del operador
    )

    asset_hosts: list[str] = EROME_ASSET_HOSTS

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        if isinstance(transport, httpx.MockTransport) and resolver is None:

            def mock_resolver(_host: str) -> list[str]:
                return ["93.184.216.34"]

            resolver = mock_resolver
        self._client = SafeHTTPClient(
            allowed_hosts=EROME_VIDEO_HOSTS,
            transport=transport,
            validate_resolved_ip=True,
            resolver=resolver,
        )
        self._seen_external_ids: set[str] = set()

    # -- FR-004 · discover ----------------------------------------------------

    async def discover(
        self, *, cursor: str | None, limit: int, section: str | None = None
    ) -> DiscoverPage:
        """Descubre álbumes-con-vídeo de una búsqueda (FR-004).

        `section` es la **query de búsqueda**, SIEMPRE con forma
        `?q=<término>` (erome no tiene categorías separadas del buscador). En
        la primera página (`cursor=None`) la URL es
        `https://www.erome.com/search<section>`; con cursor, la URL sale del
        cursor (la sección solo fija el arranque). Igual que xvideos: si la
        página trae más IDs que `limit` → `EromeParseError` (sin truncación
        silenciosa); 0 IDs nuevos → fin (anti-bucle).
        """
        if section is not None and not section.startswith("?"):
            raise ValueError(
                f"section debe empezar por '?'; recibido {section!r} "
                "(query de búsqueda de erome, p. ej. ?q=amateur)"
            )
        if cursor is None:
            self._seen_external_ids.clear()
            url = f"{EROME_BASE_URL}/search{section or '?q='}"
        else:
            url = f"{EROME_BASE_URL}{cursor}"
        response = await self._client.get(url)
        response.raise_for_status()
        current_path = urlsplit(str(response.url)).path
        current_query = urlsplit(str(response.url)).query
        current = current_path + (f"?{current_query}" if current_query else "")
        page = parse_listing_page(response.text, current_path=current)
        if len(page.external_ids) > limit:
            raise EromeParseError(
                f"la página trae {len(page.external_ids)} IDs con limit={limit}; "
                "limit debe ser >= tamaño de página (truncación no soportada)"
            )
        new_ids = [eid for eid in page.external_ids if eid not in self._seen_external_ids]
        self._seen_external_ids.update(page.external_ids)
        if not new_ids:
            return DiscoverPage(
                external_ids=page.external_ids, next_cursor=None, page_urls=page.page_urls
            )
        return page

    # -- FR-004 · get_video ---------------------------------------------------

    async def get_video(
        self, external_id: str, *, page_url: str | None = None
    ) -> VideoSource | None:
        """Obtiene la metadata normalizada de un álbum (FR-004).

        `None` solo para 404 (álbum retirado); cualquier otro error se
        propaga para que la capa de jobs lo reintente o lo marque `failed`.
        `page_url` ajeno al host canónico se ignora (SEC-001).
        """
        url = EROME_ALBUM_URL_TEMPLATE.format(external_id=external_id)
        if page_url is not None:
            resolved = _resolve_album_href(page_url)
            if resolved is not None:
                url = resolved
        response = await self._client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return parse_album_page(response.text, page_url=str(response.url))

    # -- FR-005 · get_visual_assets (equivalente SC-006: sin vídeo completo) --

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        """Miniatura del álbum + posters de sus clips (nunca el mp4)."""
        response = await self._client.get(_album_page_url(video))
        response.raise_for_status()
        assets = _album_visual_assets(response.text, thumbnail_url=video.thumbnail_url)
        if not assets and video.thumbnail_url is not None:
            assets = [VisualAsset(kind="thumbnail", url=video.thumbnail_url)]
        return assets

    # -- FR-001 · check_availability ------------------------------------------

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        """404 → `removed`; página válida → `available`; resto → `unavailable`."""
        response = await self._client.get(_album_page_url(video))
        if response.status_code == 404:
            return VideoAvailability.REMOVED
        try:
            response.raise_for_status()
            parse_album_page(response.text, page_url=str(response.url))
        except (httpx.HTTPError, EromeParseError):
            return VideoAvailability.UNAVAILABLE
        return VideoAvailability.AVAILABLE

    # -- Ciclo de vida ---------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()
