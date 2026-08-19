"""Adapter de xhamster.com: parsing HTML con selectolax (PR-062 · FR-001…FR-006 · ADR-0015).

**MANIFEST REVISADO EN MODO PRUEBA** (SEC-002 · Decisión D5 de la spec 007,
2026-08-19): el humano dio su OK de revisión legal/ToS/robots → el manifest
declara `robots_reviewed=True`, `terms_reviewed=True` y
`review_date="2026-08-19"`. La habilitación **efectiva** sigue exigiendo el gate
del registry (PR-028): manifest conforme Y `sources.enabled=true` en BD
(aprobación humana final; el seed registra la fuente con `enabled=false`).

Método de acceso (FR-004): **html** — xhamster no ofrece API/feed oficial ni
sitemap accesible (prospección 2026-08-19: `sitemap.xml` → 404, robots.txt sin
directivas `Sitemap:`); el manifest documenta la elección dentro de la
jerarquía api → sitemap → json → html → navegador.

Estructura HTML **observada** (prospección 2026-08-19, recursos públicos, sin
bypass; capturas del orquestador en `/tmp/xh-amateur.html` y
`/tmp/xh-video.html` — **nunca copiadas al repo**, SEC-004; fixtures
anonimizados en `tests/fixtures/xhamster/`):

- **Listado/discover** (`/categories/amateur`): ítems
  `div.video-thumb[data-video-id]` con enlaces
  `a.video-thumb__image-container[data-role="thumb-link"]` a
  `/videos/<slug>-<id>` — href **absoluto** en la captura real
  (`https://es.xhamster.com/videos/<slug>-<id>`); el selector NO fija
  `[href^="/videos/"]` precisamente porque el href real es absoluto: el filtro
  es el patrón del path. El `external_id` es el **último segmento tras el
  guion final** del path (`/videos/amateur-11-2533587` → `2533587`;
  `/videos/...-xhTRpbl` → `xhTRpbl`; charset `[A-Za-z0-9]+`); el
  `data-video-id` interno del listado NO se usa como id (no está en la URL
  canónica, ADR-0015 §2). `DiscoverPage.page_urls` guarda el href **completo**
  (paridad PR-045): la URL canónica de un vídeo exige el slug, así que
  `get_video(..., page_url=...)` lo consume (resuelto contra el host). Los
  ítems llevan además `data-previewvideo`/`data-previewvideo-fallback` (mp4 de
  preview: NO se exponen, Decisión D3) y `aria-label` (título; el parser del
  listado no lo usa — el título sale de la página de vídeo).
  **Paginación**: `a.page-button-link` dentro de `ol.page-list` (con
  `li.page-button`, separadores `...` y un `a.page-button-link` duplicado del
  enlace de la última página en `div.page-limit-button`): el cursor es el
  **path del enlace siguiente al activo** (`page-button-link--active`) en la
  misma lista; sin siguiente (activo al final) → `None`; un candidato que
  repite el path actual se descarta (anti-bucle). **La numeración salta** de
  páginas pequeñas a numeración alta (p. ej. `2..6` → `/16828` → `/33654`): el
  cursor avanza por el href siguiente y el anti-bucle (0 IDs nuevos / cursor
  repetido) termina la cadena.
- **Página de vídeo**: metadata en `meta[property="og:*"]` — `og:title`,
  `og:url` (URL canónica, puede servirse en `es.*` con IP española, corrección
  A1) y `og:image` (thumbnail, host `ic-vt-nss.xhcdn.com`); **sin JSON-LD ni
  `og:duration`** (prospección). El resto vive en `window.initials` (JSON en
  `<script id='initials-script'>`): `videoModel` con `id`, `duration`
  (segundos → `duration_ms`), `title` (fallback si falta `og:title`),
  `created` (epoch s → `published_at` tz-aware UTC), `tags` (array de
  `{name}`; fallback `keywords` string separada por comas; máx. 20). El
  **sprite del vídeo principal** sale del player config:
  `window.initials.spriteLoader.template` (p. ej.
  `https://thumb-v7.xhcdn.com/a/<token>/002/533/587/160x160.50.s.jpg` → fichero
  real 8000×131 → 50 tiles de 160×131, `spriteCount=50`) → `storyboard_urls=
  [template]`. **Los `data-sprite` del HTML de la página de vídeo pertenecen a
  vídeos RELACIONADOS y NO se usan** (hallazgo de prospección 2026-08-19; sus
  paths `/NNN/NNN/NNN/` difieren del vídeo principal). *Nota de robustez*: la
  captura real sirve el template anidado en
  `window.initials.xplayerPluginSettings.spriteLoader.template` (y reflejado en
  `videoModel.spriteURL`); `_sprite_template()` acepta ambas formas — siempre
  desde `window.initials`, nunca desde atributos del HTML. Sin template →
  `storyboard_urls=[]` (degradación a thumbnail, FR-005). `preview_url` queda
  **siempre `None`** (D3 · SC-004: los mp4 de preview/trailer observados no se
  exponen en v1).
- **Sprites xhamster**: tiras de UNA fila `…/<W>x<H>.<N>.s.<ext>` — observado
  `160x160.50.s.jpg` → 8000×131 → 50 tiles de 160×131 (spriteCount=50) y el
  hover sprite de listados `526x298.s.webp` → 5260×298 → 20 tiles de 263×298.
  `storyboard_grid()` (ADR-0015 §3, hook del pipeline PR-029): `(N, 1)` si la
  URL lleva `.<N>.s.`; `(20, 1)` para `*.s.webp` sin N; `None` en otro caso.

Sin red en tests: toda petición pasa por `SafeHTTPClient` (PR-024, allowlist
`xhamster.com`/`www.xhamster.com`/`es.xhamster.com`, SEC-001/003 — `es.*` como
objetivo de redirect/URL canónica, no como base, corrección A1) con un
`httpx.MockTransport` inyectable.
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
from xtrace_crawler.crawling.http import Resolver, SafeHTTPClient

# Hosts permitidos de PÁGINA para este adapter (SEC-001/003 · D1 + corrección A1):
# `es.xhamster.com` se acepta como objetivo de redirect/URL canónica (con IP
# española `og:url` puede servirse en `es.*`), no como base del discover.
XH_VIDEO_HOSTS = frozenset({"xhamster.com", "www.xhamster.com", "es.xhamster.com"})

#: Allowlist de hosts de assets de xhamster (PR-040 · SEC-001 · contracts §1).
#: **PROVISIONAL** — hosts OBSERVADOS en la prospección de 2026-08-19: el
#: sprite del player y los previews viven en `thumb-v*.xhcdn.com` (p. ej.
#: `thumb-v7.xhcdn.com`) y el thumbnail `og:image` en `ic-vt-nss.xhcdn.com`.
#: Fail-closed: un asset cuya URL apunte a un host fuera de la lista se
#: rechaza en el pipeline (PR-036) sin red. Validar/ampliar en la validación
#: real (PR-065).
XH_ASSET_HOSTS: list[str] = [f"thumb-v{index}.xhcdn.com" for index in range(10)] + [
    "ic-vt-nss.xhcdn.com",  # CDN del thumbnail og:image (observado, 2026-08-19)
]

# Host canónico de trabajo (D1): dominio neutro; los locales sirven el mismo
# catálogo y no se usan como base.
XH_BASE_URL = "https://xhamster.com"
# Plantilla de URL de vídeo (fallback cuando no hay `page_url` del listado):
# `https://xhamster.com/videos/x-<external_id>` — encaja en el patrón
# canónico `/videos/<slug>-<id>` con slug "x".
XH_VIDEO_URL_TEMPLATE = "https://xhamster.com/videos/x-{external_id}"

# Selectores clave (regresión de estructura: si cambian, los tests de los
# fixtures fallan con mensaje claro). El selector del ítem NO fija
# `[href^="/videos/"]` a propósito: el href real del listado es ABSOLUTO
# (`https://es.xhamster.com/videos/...`); el filtro de vídeo es el patrón del
# path en `_external_id_from_url`.
_LISTING_ITEM_SELECTOR = (
    "div.video-thumb a.video-thumb__image-container[data-role='thumb-link'][href]"
)
_LISTING_PAGE_SELECTOR = "a.page-button-link[href]"
_OG_TITLE_SELECTOR = "meta[property='og:title']"
_OG_URL_SELECTOR = "meta[property='og:url']"
_OG_IMAGE_SELECTOR = "meta[property='og:image']"

# Patrón del ID externo en la URL canónica: `/videos/<slug>-<id>` con el id
# como ÚLTIMO segmento tras el guion final (formas numérica `2533587` y
# alfanumérica `xhTRpbl`, charset `[A-Za-z0-9]+`). El `.+` greedy del slug hace
# que el id sea siempre el último segmento.
_VIDEO_EXTERNAL_ID_RE = re.compile(r"^/videos/.+-(?P<id>[A-Za-z0-9]+)$")

# Bloque `window.initials` de la página de vídeo (JSON en un `<script>` propio).
# Se toleran espacios/líneas entre la etiqueta y `window.initials=`, un `;`
# opcional y espacios/líneas entre el cierre del JSON y `</script>` (como
# aparece en la captura real: `};` + salto de línea).
_INITIALS_RE = re.compile(r"<script[^>]*>\s*window\.initials=(\{.*?\})\s*;?\s*</script>", re.DOTALL)

# Grid de los sprites de xhamster (ADR-0015 §3): tiras de UNA fila.
# - Con N explícito: `…/<W>x<H>.<N>.s.<ext>` (p. ej. `160x160.50.s.jpg` → 50
#   tiles; el patrón exige un `.` ANTES de los dígitos, de modo que
#   `526x298.s.webp` NO puede interpretarse como `<526x29>.<8>.s.webp`).
_SPRITE_GRID_RE = re.compile(r"\.(?P<count>\d+)\.s\.(?:jpe?g|webp|png)$")
# - Hover sprite de los listados SIN N: `…/<W>x<H>.s.webp` (`526x298.s.webp`
#   → fichero real 5260×298 → 20 tiles de 263×298).
_SPRITE_HOVER_RE = re.compile(r"\d+x\d+\.s\.webp$")

#: Máximo de tags expuestos desde `videoModel.tags`/`keywords` (PR-062).
_MAX_TAGS: int = 20


class XhamsterParseError(ValueError):
    """Error de parseo/contrato del adapter xhamster.

    Se lanza cuando no se puede identificar siquiera el patrón de vídeo
    (`og:url` ni `page_url` con `/videos/<slug>-<id>`) o no hay ninguna señal
    de página de vídeo — la señal de "el HTML cambió" para que el job quede
    `failed` con un error legible (edge case de la spec) — o cuando `discover()`
    recibe una página con más IDs que `limit` (truncación no soportada: el
    llamador debe ajustar su lote, nunca se pierden IDs en silencio). Los
    campos opcionales ausentes no lanzan error (degradación).
    """


# ---------------------------------------------------------------------------
# Helpers de parseo (funciones puras: sin red, testables sobre fixtures)
# ---------------------------------------------------------------------------


def _external_id_from_url(url: str) -> str | None:
    """Extrae el ID externo de una URL/path `/videos/<slug>-<id>` (o None).

    El ID externo es el **último segmento tras el guion final** del path
    (formas numérica `2533587` y alfanumérica `xhTRpbl`, charset
    `[A-Za-z0-9]+`) — estructura real observada (2026-08-19). Se tolera un
    `/` final.
    """
    match = _VIDEO_EXTERNAL_ID_RE.match(urlsplit(url).path.rstrip("/"))
    if match is None:
        return None
    return match.group("id")


def _cursor_from_href(href: str) -> str | None:
    """Normaliza el href de la paginación a un cursor (path) o None.

    Un href **sin path real** (`#`, vacío, solo query/fragmento) devuelve
    `None`: el llamador lo trata como fin de cadena — nunca se construye la
    HOME (`https://xhamster.com/`) como cursor (Decisión D2: en v1 no se
    explora la home). Hallazgo del revisor (F-1, revisión PR-062).
    """
    if href.startswith("/"):
        return href
    path = urlsplit(href).path
    return path if path else None


def _list_pagination_cursor(tree: HTMLParser, *, current_path: str | None) -> str | None:
    """Cursor de la paginación por `a.page-button-link` de xhamster.

    Estructura real (2026-08-19): `ol.page-list > li.page-button > a.
    page-button-link` (el activo lleva `page-button-link--active`), con
    separadores `...` y un `a.page-button-link` duplicado del enlace de la
    última página en `div.page-limit-button` (también `a.page-button-link`,
    así que entra en la misma lista — se descarta solo por el anti-bucle).

    El cursor es el **path del primer enlace siguiente al activo** en la misma
    lista: la numeración salta de páginas pequeñas a numeración alta (p. ej.
    activo=6 → `/16828`; activo=/16828 → `/33654`), y en la última página el
    activo es el último enlace (el duplicado del `page-limit-button` repite el
    path actual y se descarta) → `None`. Un candidato que repite el path actual
    de la respuesta devuelve `None` (anti-bucle, paridad PR-043). Sin activo →
    `None` (fin de cadena, fail-safe). Un candidato **sin path real** (`#`,
    vacío, solo query/fragmento) también es fin de cadena (`None`): nunca se
    construye la HOME como cursor (Decisión D2 · hallazgo del revisor F-1).
    Los hrefs se normalizan a path con `_cursor_from_href`.
    """
    links = tree.css(_LISTING_PAGE_SELECTOR)
    active_index = next(
        (
            i
            for i, node in enumerate(links)
            if "page-button-link--active" in (node.attributes.get("class") or "").split()
        ),
        None,
    )
    if active_index is None:
        return None
    for node in links[active_index + 1 :]:
        href = node.attributes.get("href")
        if href is None:
            continue
        candidate = _cursor_from_href(href)
        if candidate is None:
            # Href sin path real (`#`, vacío, solo query/fragmento): fin de
            # cadena — nunca se construye la HOME (D2).
            return None
        # Anti-bucle: un siguiente que apunta a la página actual es fin.
        if current_path is None or candidate != current_path:
            return candidate
    return None


def _resolve_listing_href(href: str) -> str | None:
    """Resuelve el href del listado a una URL absoluta del host canónico (PR-045).

    Acepta solo: (1) paths relativos (`/videos/...` → `https://xhamster.com/…`)
    y (2) URLs http(s) de `xhamster.com`/`www.xhamster.com`/`es.xhamster.com`
    (D1 + corrección A1: `es.*` como objetivo de redirect/URL canónica, no
    como base). Cualquier otro valor (host ajeno, esquema no http) → `None`:
    el llamador usa la plantilla fallback (el `SafeHTTPClient` con allowlist
    sería la segunda barrera, SEC-001).
    """
    if href.startswith("/"):
        return f"{XH_BASE_URL}{href}"
    parsed = urlsplit(href)
    host = (parsed.hostname or "").lower().removesuffix(".")
    if parsed.scheme in ("http", "https") and host in XH_VIDEO_HOSTS:
        return href
    return None


def _meta_content(tree: HTMLParser, selector: str) -> str | None:
    """Contenido de un `meta[property]` (og:*) o None si no está."""
    node = tree.css_first(selector)
    if node is None:
        return None
    content = node.attributes.get("content")
    return content if isinstance(content, str) and content else None


def _initials(html: str) -> dict[str, Any]:
    """Devuelve el dict de `window.initials` de la página de vídeo, o {}.

    El JSON viaja en `<script id='initials-script'>window.initials={...}
    </script>` (prospección 2026-08-19). JSON ausente o inválido → {} (los
    campos opcionales degradan a None; las señales de vídeo siguen pudiendo
    venir de og:*).
    """
    match = _INITIALS_RE.search(html)
    if match is None:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _sprite_template(initials: dict[str, Any]) -> str | None:
    """Template del sprite del vídeo principal desde `window.initials` (ADR-0015 §3).

    Forma canónica de la spec: `window.initials.spriteLoader.template`. La
    captura real (2026-08-19) lo sirve anidado en
    `window.initials.xplayerPluginSettings.spriteLoader.template` (y reflejado
    en `videoModel.spriteURL`): se aceptan ambas formas como fallback. NUNCA se
    leen los `data-sprite` del HTML — pertenecen a vídeos relacionados.
    """
    containers: list[Any] = [initials.get("spriteLoader")]
    plugin_settings = initials.get("xplayerPluginSettings")
    if isinstance(plugin_settings, dict):
        containers.append(plugin_settings.get("spriteLoader"))
    for container in containers:
        if isinstance(container, dict):
            template = container.get("template")
            if isinstance(template, str) and template:
                return template
    video_model = initials.get("videoModel")
    if isinstance(video_model, dict):
        sprite_url = video_model.get("spriteURL")
        if isinstance(sprite_url, str) and sprite_url:
            return sprite_url
    return None


def _tags_from_video_model(video_model: dict[str, Any]) -> list[str]:
    """Tags desde `videoModel.tags` (array de `{name}`) con fallback `keywords`.

    `keywords` es una string separada por comas (forma observada en la
    prospección). Dedup preservando orden y tope de `_MAX_TAGS` (paridad
    PR-043 de xvideos).
    """
    tags: list[str] = []
    raw_tags = video_model.get("tags")
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if not isinstance(tag, dict):
                continue
            name = tag.get("name")
            if isinstance(name, str) and name:
                tags.append(name)
    if not tags:
        keywords = video_model.get("keywords")
        if isinstance(keywords, str) and keywords:
            tags = [part.strip() for part in keywords.split(",") if part.strip()]
    return list(dict.fromkeys(tags))[:_MAX_TAGS]


def _published_at_from_created(video_model: dict[str, Any]) -> datetime | None:
    """`videoModel.created` (epoch segundos) → `published_at` tz-aware UTC."""
    created = video_model.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
        return None
    try:
        return datetime.fromtimestamp(created, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _duration_ms_from_video_model(video_model: dict[str, Any]) -> int | None:
    """`videoModel.duration` (segundos) → `duration_ms`, o None si no es fiable."""
    duration = video_model.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        return None
    return int(duration * 1000)


def parse_listing_page(html: str, *, current_path: str | None = None) -> DiscoverPage:
    """Parsea una página de listado/discover: IDs externos (dedup) + cursor + hrefs.

    Los ítems se detectan con `div.video-thumb a.video-thumb__image-container[
    data-role='thumb-link'][href]` (estructura observada 2026-08-19) y el ID
    externo sale del patrón `/videos/<slug>-<id>` del path (formas numérica y
    alfanumérica). El selector no fija `[href^="/videos/"]` porque el href real
    es absoluto (`https://es.xhamster.com/...`); los enlaces a otros
    contenidos (p. ej. `/photos/...`) no matchean el patrón y se ignoran. Los
    hrefs repetidos (p. ej. imagen + overlay del mismo vídeo) se deduplican por
    ID externo.

    **PR-045 (paridad)**: `DiscoverPage.page_urls` guarda por external_id el
    href **COMPLETO** del listado (el primer href visto por vídeo): la URL
    canónica de un vídeo exige el slug, así que `get_video(..., page_url=...)`
    lo consume (resuelto contra el host) y el pipeline lo reenvía durante
    DISCOVER.

    `current_path` es el path de la **URL FINAL de la respuesta** (tras
    redirects, `response.url`): el cursor de la paginación descarta un
    candidato que repite el path actual (anti-bucle, paridad PR-043).

    Paginación: `a.page-button-link` — el cursor es el path del enlace
    **siguiente al activo** (`page-button-link--active`) en la misma lista; sin
    siguiente (activo al final) → `next_cursor=None`. La numeración puede
    saltar de páginas pequeñas a numeración alta (`/16828`): el cursor avanza
    por el href siguiente igualmente.

    Si la estructura cambia y no hay ítems, devuelve una página vacía sin
    crashear (aislamiento); los tests de regresión sobre los fixtures señalan
    el cambio de selector.
    """
    tree = HTMLParser(html)
    page_urls: dict[str, str] = {}
    for node in tree.css(_LISTING_ITEM_SELECTOR):
        href = node.attributes.get("href")
        if not href:
            continue
        external_id = _external_id_from_url(href)
        if external_id is not None:
            page_urls.setdefault(external_id, href)
    external_ids = list(page_urls)
    next_cursor = _list_pagination_cursor(tree, current_path=current_path)
    return DiscoverPage(external_ids=external_ids, next_cursor=next_cursor, page_urls=page_urls)


def parse_video_page(html: str, *, page_url: str) -> VideoSource:
    """Parsea una página de vídeo a `VideoSource` normalizado (FR-004 · PR-062).

    Estructura observada (2026-08-19): `og:title`/`og:url`/`og:image` +
    `window.initials.videoModel` (`id`, `duration` s→ms, `title`, `created`
    epoch→`published_at`, `tags`/`keywords`); sin JSON-LD ni `og:duration`.
    El sprite del vídeo principal viene de
    `window.initials.spriteLoader.template` → `storyboard_urls=[template]`
    (los `data-sprite` del HTML son de vídeos RELACIONADOS y NO se usan; sin
    template → `storyboard_urls=[]`). Los mp4 de preview/trailer NO se
    exponen: `preview_url` queda siempre `None` (D3 · SC-004).

    El ID externo se toma del `og:url` (patrón `/videos/<slug>-<id>`); si no
    hay og:url, se deduce de `page_url`. Sin patrón → `XhamsterParseError` con
    mensaje claro (regresión de estructura).
    """
    tree = HTMLParser(html)
    title = _meta_content(tree, _OG_TITLE_SELECTOR)
    page_url_from_og = _meta_content(tree, _OG_URL_SELECTOR)

    initials = _initials(html)
    video_model = initials.get("videoModel")
    if not isinstance(video_model, dict):
        video_model = {}

    # Guarda anti-regresión: una página sin ninguna señal de vídeo (og:title,
    # og:url ni window.initials.videoModel) no es una página de vídeo (p. ej.
    # captcha/anti-bot o HTML cambiado): fallamos con error claro en vez de
    # devolver un vídeo vacío (SEC-001: no se intenta saltar protecciones;
    # edge case "HTML cambia").
    if page_url_from_og is None and title is None and not video_model:
        raise XhamsterParseError(
            "página sin señales de vídeo (og:title/og:url ni window.initials.videoModel); "
            "¿cambió la estructura HTML de xhamster o hay una página de protección?"
        )

    external_id = _external_id_from_url(page_url_from_og) if page_url_from_og else None
    if external_id is None:
        external_id = _external_id_from_url(page_url)
    if external_id is None:
        raise XhamsterParseError(
            "no se encontró el patrón de vídeo /videos/<slug>-<id> (ni og:url ni page_url); "
            "¿cambió la estructura HTML de xhamster?"
        )

    if title is None:
        model_title = video_model.get("title")
        if isinstance(model_title, str) and model_title:
            title = model_title

    storyboard_urls: list[str] = []
    sprite_template = _sprite_template(initials)
    if sprite_template is not None:
        storyboard_urls = [sprite_template]

    return VideoSource(
        source="xhamster",
        external_id=external_id,
        title=title,
        page_url=(
            page_url_from_og
            if page_url_from_og is not None
            else f"{XH_BASE_URL}/videos/x-{external_id}"
        ),
        duration_ms=_duration_ms_from_video_model(video_model),
        thumbnail_url=_meta_content(tree, _OG_IMAGE_SELECTOR),
        preview_url=None,  # D3 · SC-004: los mp4 de preview/trailer NO se exponen en v1
        storyboard_urls=storyboard_urls,
        tags=_tags_from_video_model(video_model),
        published_at=_published_at_from_created(video_model),
    )


def storyboard_grid(asset: VisualAsset) -> tuple[int, int] | None:
    """Grid (filas, columnas) del sprite storyboard de xhamster (ADR-0015 §3).

    Los sprites de xhamster son tiras de UNA fila:
    - `(N, 1)` si la URL lleva `.<N>.s.` (p. ej. `160x160.50.s.jpg` → 50 tiles
      de 160×131, `spriteCount=50`);
    - `(20, 1)` para `*.s.webp` sin N (hover sprite de los listados
      `526x298.s.webp` → fichero real 5260×298 → 20 tiles de 263×298);
    - `None` en cualquier otro caso (el pipeline degrada, paridad PR-053).

    Hook `storyboard_grid` del pipeline (PR-029): el CLI lo conecta con import
    dinámico (PR-063); el core no cambia.
    """
    path = urlsplit(asset.url).path
    if _SPRITE_HOVER_RE.search(path) is not None:
        return (20, 1)
    match = _SPRITE_GRID_RE.search(path)
    if match is not None:
        return (int(match.group("count")), 1)
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class XhamsterAdapter:
    """Adapter real de xhamster.com (HTML, sprite/storyboard + thumbnail) — FR-001…FR-006.

    Cumple el protocolo `SourceAdapter` (FR-001, ADR-0009). **Manifest
    revisado en modo prueba** (SEC-002 · Decisión D5, 2026-08-19):
    `robots_reviewed=True`, `terms_reviewed=True`, `review_date="2026-08-19"`
    (OK del operador); la habilitación efectiva sigue dependiendo de
    `sources.enabled=true` en BD (gate del registry: manifest + enabled). Sin
    red en tests: se inyecta un `httpx.MockTransport` y toda petición pasa por
    el cliente HTTP seguro con allowlist (SEC-001/003).

    **PR-062 · SEC-001**: declara `asset_hosts` (**PROVISIONAL**, ver
    `XH_ASSET_HOSTS`): allowlist de hosts de assets observados en la
    prospección de 2026-08-19 (sprite en `thumb-v*.xhcdn.com`, thumbnail en
    `ic-vt-nss.xhcdn.com`); el pipeline (PR-036) la usa para la descarga por
    HTTP — validar/ampliar en los backfills reales (PR-065).
    """

    manifest = AdapterManifest(
        source="xhamster",
        access_method="html",  # FR-004: jerarquía api → sitemap → json → html → navegador
        assets_accessed=["storyboard", "thumbnail"],  # FR-005: sprite + og:image; sin previews (D3)
        robots_reviewed=True,  # SEC-002: revisión robots del operador (2026-08-19, modo prueba, D5)
        terms_reviewed=True,  # SEC-002: revisión ToS del operador (2026-08-19, modo prueba, D5)
        rate_limit=RateLimitSpec(min_interval_ms=2_000, max_rps=0.5),  # conservador (D5)
        review_date="2026-08-19",  # SEC-002: aprobación humana (operador, modo prueba, D5)
    )

    # PR-062 · SEC-001 · contracts §1: allowlist de hosts de assets
    # (PROVISIONAL — hosts observados 2026-08-19, validar en PR-065); el
    # pipeline la usa como allowlist del cliente de assets, nunca derivada de
    # las URLs parseadas (fail-closed).
    asset_hosts: list[str] = XH_ASSET_HOSTS

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        """Crea el adapter con su cliente HTTP seguro (allowlist de hosts).

        Args:
            transport: transporte inyectable (`httpx.MockTransport` en tests);
                `None` → red real (solo uso operativo tras habilitación).
            resolver: resolución inyectable para la validación anti-DNS-rebinding;
                cuando se usa un `httpx.MockTransport` sin resolver explícito se
                emplea una IP pública fija para mantener los tests sin DNS real.
        """
        if isinstance(transport, httpx.MockTransport) and resolver is None:
            # `MockTransport` no resuelve DNS. Mantener esta ruta determinista
            # permite conservar `validate_resolved_ip=True` sin red real en
            # tests y deja la resolución real para el transporte operativo.
            def mock_resolver(_host: str) -> list[str]:
                return ["93.184.216.34"]

            resolver = mock_resolver
        self._client = SafeHTTPClient(
            allowed_hosts=XH_VIDEO_HOSTS,
            transport=transport,
            validate_resolved_ip=True,
            resolver=resolver,
        )
        # Protección anti-bucle de discover (paridad PR-043): IDs ya vistos en
        # esta instancia (una instancia por proceso de worker vía el registry).
        # Se reinicia al arrancar una cadena nueva (cursor=None).
        self._seen_external_ids: set[str] = set()

    # -- FR-003 · discover ----------------------------------------------------

    async def discover(
        self, *, cursor: str | None, limit: int, section: str | None = None
    ) -> DiscoverPage:
        """Descubre IDs externos de una página de listado (FR-003 · PR-062).

        **D2 — `section` OBLIGATORIO** (Decisión D2 de la spec 007): en v1 no
        se explora la home; sin `section` (o sin '/' inicial) el discover se
        rechaza con `ValueError` claro (fail-fast). Con `section`, la primera
        página se pide a `https://xhamster.com<section>`; con cursor, la URL
        sale del cursor (la sección solo fija el arranque).

        `cursor` es el path de la página siguiente (None → primera página).
        `limit` debe ser >= tamaño de página: **la truncación no está
        soportada** — si la página trae más IDs que `limit`, se lanza
        `XhamsterParseError` indicando los tamaños reales (nunca se descartan
        IDs en silencio ni se repite el cursor recibido).

        **Protección anti-bucle** (paridad PR-043):
        - el cursor se toma de la página FINAL (`response.url` tras redirects);
        - si la paginación repite el path actual → `next_cursor=None`;
        - si la página devuelve 0 IDs **nuevos** (no vistos en esta instancia)
          → `next_cursor=None` (fin de la cadena).

        **PR-045 (paridad)**: la página devuelta incluye `page_urls`
        (external_id → href COMPLETO del listado con slug): el pipeline lo
        reenvía a `get_video(..., page_url=...)` porque la URL canónica
        reconstruida sin slug puede no servir.
        """
        if section is None:
            raise ValueError(
                "section es OBLIGATORIO para xhamster (D2): el discover solo se explora "
                "por sección (p. ej. /categories/amateur); en v1 no se explora la home"
            )
        if not section.startswith("/"):
            raise ValueError(
                f"section debe empezar por '/'; recibido {section!r} "
                "(ruta de sección del sitio, p. ej. /categories/amateur)"
            )
        if cursor is None:
            # Nueva cadena de paginación: se reinicia el conjunto de vistos.
            self._seen_external_ids.clear()
            url = f"{XH_BASE_URL}{section}"
        else:
            url = f"{XH_BASE_URL}{cursor}"
        response = await self._client.get(url)
        response.raise_for_status()
        current_path = urlsplit(str(response.url)).path
        page = parse_listing_page(response.text, current_path=current_path)
        if len(page.external_ids) > limit:
            raise XhamsterParseError(
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
            return DiscoverPage(
                external_ids=page.external_ids,
                next_cursor=None,
                page_urls=page.page_urls,  # PR-045: los hrefs del listado se conservan
            )
        return page

    # -- FR-004 · get_video ---------------------------------------------------

    async def get_video(
        self, external_id: str, *, page_url: str | None = None
    ) -> VideoSource | None:
        """Obtiene la metadata normalizada de un vídeo (FR-004).

        **PR-045 (paridad)**: `page_url` (opcional) es el **href completo del
        listado** (`DiscoverPage.page_urls[external_id]`, p. ej.
        `/videos/<slug>-<id>`): se resuelve contra el host canónico y se usa tal
        cual. `None` → se reconstruye la plantilla `https://xhamster.com/videos/
        x-<external_id>` (retrocompatible; FETCH_METADATA no dispone del href).
        Un `page_url` ajeno al host canónico (o de host fuera de la allowlist
        D1/A1) no se usa (fallback; SEC-001).

        `None` solo para 404 (vídeo retirado, edge case de la spec); cualquier
        otro error HTTP o de estructura se propaga para que la capa de jobs lo
        reintente o lo marque `failed` con la causa.
        """
        url = XH_VIDEO_URL_TEMPLATE.format(external_id=external_id)
        if page_url is not None:
            resolved = _resolve_listing_href(page_url)
            if resolved is not None:
                url = resolved
        response = await self._client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return parse_video_page(response.text, page_url=str(response.url))

    # -- FR-005 · get_visual_assets (SC-004) ----------------------------------

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        """Devuelve los assets visuales declarados en el manifest (FR-005/SC-004 · PR-062).

        UN `VisualAsset(kind="storyboard", url=video.storyboard_urls[0],
        position=None, timestamp_ms=None)` si hay sprite (el template del
        player ya viaja en el `VideoSource` — sin re-fetch) + UN
        `VisualAsset(kind="thumbnail", url=video.thumbnail_url)` si hay
        thumbnail. Sin sprite → solo thumbnail (degradación, FR-005); sin
        ambos → `[]`. **Nunca un mp4** (SC-004): los previews observados no se
        exponen en v1 (D3) y `preview_url` es siempre `None`.
        """
        assets: list[VisualAsset] = []
        if video.storyboard_urls:
            assets.append(
                VisualAsset(
                    kind="storyboard",
                    url=video.storyboard_urls[0],
                    position=None,
                    timestamp_ms=None,
                )
            )
        if video.thumbnail_url is not None:
            assets.append(VisualAsset(kind="thumbnail", url=video.thumbnail_url))
        return assets

    # -- FR-001 · check_availability ------------------------------------------

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        """Comprueba la disponibilidad del vídeo (FR-001).

        404 → `removed` (terminal, sin reintentos); página válida → `available`;
        cualquier otra cosa (error HTTP, estructura cambiada) → `unavailable`
        (no se puede confirmar ahora). Igual que `get_visual_assets`, la
        petición usa `video.page_url` (URL canónica con slug) cuando está
        disponible; vacío o ajeno al host canónico (SEC-001, A1) → fallback a
        la plantilla.
        """
        url = XH_VIDEO_URL_TEMPLATE.format(external_id=video.external_id)
        if video.page_url:
            resolved = _resolve_listing_href(video.page_url)
            if resolved is not None:
                url = resolved
        response = await self._client.get(url)
        if response.status_code == 404:
            return VideoAvailability.REMOVED
        try:
            response.raise_for_status()
            parse_video_page(response.text, page_url=str(response.url))
        except (httpx.HTTPError, XhamsterParseError):
            return VideoAvailability.UNAVAILABLE
        return VideoAvailability.AVAILABLE

    # -- Ciclo de vida ---------------------------------------------------------

    async def aclose(self) -> None:
        """Cierra el cliente HTTP subyacente."""
        await self._client.aclose()
