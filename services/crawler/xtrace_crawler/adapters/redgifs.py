"""Adapter de redgifs.com: API oficial con token temporal (PR-066 · FR-001…FR-006 · ADR-0016).

**MANIFEST REVISADO EN MODO PRUEBA** (SEC-002 · Decisión D4 de la spec 008,
2026-08-19; reanudación de la implementación instruida por el humano el
2026-08-20): el manifest declara `robots_reviewed=True`, `terms_reviewed=True`
y `review_date="2026-08-19"`. La habilitación **efectiva** sigue exigiendo el
gate del registry (PR-028): manifest conforme Y `sources.enabled=true` en BD
(aprobación humana final; el seed registra la fuente con `enabled=false`).

Método de acceso (FR-002): **api** — primer adapter del SDK con este nivel de
la jerarquía (api → sitemap → json → html → navegador, FR-004). `www.redgifs
.com` es una SPA sin SSR (HTML inservible para crawling) y robots.txt disallow
`/watch/` y `/ifr/`: este adapter **nunca** habla con `www.redgifs.com`, solo
con `api.redgifs.com` (metadata) y `media.redgifs.com` (assets, vía el
pipeline con `asset_hosts`).

Estructura de la API **observada** (prospección 2026-08-19, recursos públicos,
sin bypass; JSON real capturado fuera del repo, SEC-004; fixtures anonimizados
en `tests/fixtures/redgifs/`):

- **Token temporal**: `GET /v2/auth/temporary` → `{"token": "<jwt>", ...}`
  (`scope=read`, sin clave, validez ≈24h, `rate:-1`). Se obtiene bajo demanda
  (primer uso) y se cachea **solo en memoria** de esta instancia (SEC-005:
  nunca se loguea, persiste en BD ni aparece en fixtures/errores); ante `401`
  se renueva automáticamente (una vez por petición) antes de propagar el
  error.
- **Listado de nicho** (`GET /v2/niches/<id>/gifs?order=new&count=100&
  page=N`): envelope `{gifs, page, pages, total, ...}`; paginación por
  **`page`** (1-based; el campo `cursor` del envelope NO pagina este
  endpoint); `count=100` es el máximo aceptado (`count=200` → respuesta
  vacía). Los ítems del listado ya traen el objeto gif completo: se cachean
  en memoria para que `get_video` no repita la petición (optimización, sin
  cambiar el contrato).
- **Objeto gif** (`GET /v2/gifs/<id>`): envuelto en `{"gif": {...}}` (con
  `user`/`niches` extra, ignorados); `404` (`{"error":{"code":
  "GifNotFound"}}`) → ítem retirado. Campos usados: `id` (→ `external_id`
  lowercase; la API exige lowercase), `description` (→ `title`, nullable),
  `createDate` (epoch s → `published_at`), `duration` (s, **nullable** — hay
  posts de imagen, `type=2`/`hasAudio=false`/`hls=false` — → `duration_ms`
  redondeado o `None`), `tags`, `urls.thumbnail` (→ `thumbnail_url`),
  `urls.poster` (sin campo propio en `VideoSource`: se reutiliza
  `storyboard_urls=[poster_url]` como contenedor genérico — redgifs no tiene
  storyboard/sprite, ADR-0016 §4). `urls.sd`/`urls.hd`/`urls.silent` **nunca**
  se leen: son el contenido completo del ítem (SC-006 de la spec 002).
- **`page_url`**: fijo `https://www.redgifs.com/watch/<external_id>` —
  referencia canónica que este adapter **nunca fetchea** (Decisión D5;
  robots.txt disallow `/watch/`).

Sin red en tests: toda petición pasa por `SafeHTTPClient` (PR-024, allowlist
`api.redgifs.com`, SEC-001/003) con un `httpx.MockTransport` inyectable.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

import httpx

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.crawling.http import Resolver, SafeHTTPClient

#: Host de la API permitido para este adapter (SEC-001/003 · FR-002/FR-006):
#: `www.redgifs.com` NUNCA se fetchea (SPA sin SSR + robots disallow en
#: `/watch/`+`/ifr/`).
RG_API_HOSTS = frozenset({"api.redgifs.com"})

#: Allowlist de hosts de assets de redgifs (PR-040 · SEC-001 · contracts §1):
#: los thumbnails/posters viven en `media.redgifs.com` (fail-closed, paridad
#: con el resto de adapters reales).
RG_ASSET_HOSTS: list[str] = ["media.redgifs.com"]

RG_API_BASE = "https://api.redgifs.com"
RG_AUTH_TEMPORARY_URL = f"{RG_API_BASE}/v2/auth/temporary"
RG_WATCH_URL_TEMPLATE = "https://www.redgifs.com/watch/{external_id}"

#: Prefijo de sección obligatorio (Decisión D2): en v1 solo se exploran
#: nichos (`/niches/<id>`), nunca tags ni búsqueda de texto.
_NICHES_PREFIX = "/niches/"

#: Tamaño de página máximo verificado en la prospección (`count=200` →
#: respuesta vacía).
_PAGE_COUNT = 100

#: Tope de `_gif_cache` (hallazgo de revisión independiente, 2026-08-20):
#: cada gif se lee de la cache dos veces por diseño del pipeline (dentro de
#: `discover()` al upsertear, y de nuevo en el job `FETCH_METADATA`
#: posterior, `pipeline.py::_fetch_metadata`) — la cache NO puede consumirse
#: de un solo uso. Para no crecer sin límite en un worker de larga duración
#: (una fuente con 66 000+ ítems), se acota con evicción FIFO: 10 páginas
#: (`_PAGE_COUNT`) de margen es más que suficiente para el desfase real entre
#: DISCOVER y FETCH_METADATA.
_GIF_CACHE_MAX_ENTRIES = _PAGE_COUNT * 10


class RedgifsParseError(ValueError):
    """Error de parseo/contrato del adapter redgifs (estructura de API inesperada).

    Se lanza cuando un envelope de listado no tiene ni `gifs` ni señales de
    paginación (`pages`/`total` ausentes con `gifs` vacío — respuesta de
    `count=0` inválida), cuando un objeto gif no tiene un `id` parseable, o
    cuando `discover()` recibe una página con más IDs que `limit` (truncación
    no soportada). Los campos opcionales ausentes no lanzan error
    (degradación, paridad spec 002).
    """


class RedgifsAuthError(ValueError):
    """Fallo al obtener/renovar el token temporal de la API (SEC-005).

    El mensaje **nunca** incluye el valor de un token (ni el fallido ni uno
    previo): solo describe la causa (HTTP, JSON inválido, campo ausente).
    """


# ---------------------------------------------------------------------------
# Helpers de parseo (funciones puras: sin red, testables sobre fixtures)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    """`value` como `int`, o `None` si no es un entero fiable (bool excluido)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _clean_http_url(value: Any) -> str | None:
    """`value` como URL http(s) no vacía, o `None` (degradación silenciosa)."""
    if not isinstance(value, str) or not value:
        return None
    if not value.startswith(("http://", "https://")):
        return None
    return value


def _duration_ms_from_seconds(value: Any) -> int | None:
    """`duration` (segundos, nullable — posts de imagen) → `duration_ms` redondeado."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(round(value * 1000))


def _published_at_from_create_date(value: Any) -> datetime | None:
    """`createDate` (epoch segundos) → `published_at` tz-aware UTC, o `None`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _tags_from_gif(value: Any) -> list[str]:
    """`tags` (array de strings) con dedup preservando orden, o `[]`."""
    if not isinstance(value, list):
        return []
    tags = [tag for tag in value if isinstance(tag, str) and tag]
    return list(dict.fromkeys(tags))


def parse_gif_object(data: Mapping[str, Any]) -> VideoSource:
    """Normaliza un objeto gif crudo de la API a `VideoSource` (FR-004 · PR-066).

    Acepta el objeto **desenvuelto** (sin el wrapper `{"gif": ...}`) — tanto
    desde `GET /v2/gifs/<id>` (tras `parse_gif_response`) como desde cada
    ítem del envelope de listado (que ya trae el objeto completo). `id` se
    normaliza a lowercase (la API lo exige en `GET /v2/gifs/<id>`).
    `urls.poster` se guarda en `storyboard_urls` — contenedor genérico
    reutilizado porque redgifs no tiene storyboard/sprite (ADR-0016 §4), no
    porque el poster sea un storyboard. `urls.sd`/`hd`/`silent` nunca se leen
    (SC-006 de la spec 002).

    Raises:
        RedgifsParseError: sin un `id` string no vacío (regresión de estructura).
    """
    raw_id = data.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise RedgifsParseError(
            "objeto gif sin 'id' string válido; ¿cambió la estructura de la API de redgifs?"
        )
    external_id = raw_id.lower()

    description = data.get("description")
    title = description if isinstance(description, str) and description else None

    urls = data.get("urls")
    urls = urls if isinstance(urls, dict) else {}
    thumbnail_url = _clean_http_url(urls.get("thumbnail"))
    poster_url = _clean_http_url(urls.get("poster"))

    return VideoSource(
        source="redgifs",
        external_id=external_id,
        title=title,
        page_url=RG_WATCH_URL_TEMPLATE.format(external_id=external_id),
        duration_ms=_duration_ms_from_seconds(data.get("duration")),
        thumbnail_url=thumbnail_url,
        preview_url=None,  # SC-006: los mp4 sd/hd/silent nunca se exponen
        storyboard_urls=[poster_url] if poster_url is not None else [],
        tags=_tags_from_gif(data.get("tags")),
        published_at=_published_at_from_create_date(data.get("createDate")),
    )


def parse_gif_response(payload: Mapping[str, Any]) -> VideoSource:
    """Normaliza la respuesta de `GET /v2/gifs/<id>` (wrapper `{"gif": {...}}`).

    Raises:
        RedgifsParseError: sin el campo `gif` (objeto) — regresión de estructura.
    """
    gif = payload.get("gif")
    if not isinstance(gif, dict):
        raise RedgifsParseError(
            "respuesta de /v2/gifs/<id> sin el campo 'gif' esperado; "
            "¿cambió la estructura de la API de redgifs?"
        )
    return parse_gif_object(gif)


class NicheGifsPage(NamedTuple):
    """Página parseada del listado de un nicho (interno, no forma parte del contrato)."""

    external_ids: list[str]
    gifs_by_id: dict[str, VideoSource]
    page: int | None
    pages: int | None


def parse_niche_gifs_envelope(payload: Mapping[str, Any]) -> NicheGifsPage:
    """Parsea el envelope `{gifs, page, pages, total}` del listado de nicho (FR-003).

    Ítems sin `id` parseable se ignoran (no rompen la página completa); IDs
    repetidos dentro de la misma página se deduplican preservando el primer
    orden visto.

    **Edge cases de la spec**:
    - `gifs` ausente o vacío **con** `pages`/`total` presentes → página vacía
      sin error (fin de discover, paridad con el anti-bucle de xvideos).
    - `gifs` ausente o vacío **y** `pages`/`total` ausentes (respuesta de
      `count=0` inválida) → `RedgifsParseError` (regresión de estructura).
    """
    raw_gifs = payload.get("gifs")
    page = _as_int(payload.get("page"))
    pages = _as_int(payload.get("pages"))
    total = payload.get("total")

    if not isinstance(raw_gifs, list) or not raw_gifs:
        if pages is None and total is None:
            raise RedgifsParseError(
                "envelope de listado sin 'gifs' y sin 'pages'/'total'; "
                "¿cambió la estructura de la API de redgifs o es una respuesta de count=0 inválida?"
            )
        return NicheGifsPage(external_ids=[], gifs_by_id={}, page=page, pages=pages)

    external_ids: list[str] = []
    gifs_by_id: dict[str, VideoSource] = {}
    for raw in raw_gifs:
        if not isinstance(raw, dict):
            continue
        try:
            video = parse_gif_object(raw)
        except RedgifsParseError:
            continue  # ítem sin id parseable: se ignora (no rompe la página)
        if video.external_id not in gifs_by_id:
            external_ids.append(video.external_id)
            gifs_by_id[video.external_id] = video

    return NicheGifsPage(external_ids=external_ids, gifs_by_id=gifs_by_id, page=page, pages=pages)


def _bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RedgifsAdapter:
    """Adapter real de redgifs.com (API oficial, token temporal) — FR-001…FR-006.

    Cumple el protocolo `SourceAdapter` (FR-001, ADR-0009). **Manifest
    revisado en modo prueba** (SEC-002 · Decisión D4, 2026-08-19):
    `robots_reviewed=True`, `terms_reviewed=True`, `review_date="2026-08-19"`
    (OK del operador); la habilitación efectiva sigue dependiendo de
    `sources.enabled=true` en BD (gate del registry). Sin red en tests: se
    inyecta un `httpx.MockTransport` y toda petición pasa por el cliente HTTP
    seguro con allowlist (SEC-001/003).

    **Primer adapter `access_method="api"`** del SDK (ADR-0016): sin parsing
    HTML, sin sprite/storyboard que recortar — no exporta ningún grid
    resolver, a diferencia de xhamster/xvideos.
    """

    manifest = AdapterManifest(
        source="redgifs",
        access_method="api",  # FR-002/FR-004: primer adapter de este nivel (jerarquía)
        assets_accessed=["thumbnail"],  # FR-005: thumbnail + poster, ambos kind="thumbnail"
        robots_reviewed=True,  # SEC-002: revisión robots del operador (2026-08-19, modo prueba, D4)
        terms_reviewed=True,  # SEC-002: revisión ToS del operador (2026-08-19, modo prueba, D4)
        rate_limit=RateLimitSpec(min_interval_ms=2_000, max_rps=0.5),  # conservador (D4)
        review_date="2026-08-19",  # SEC-002: aprobación humana (operador, modo prueba, D4)
    )

    #: PR-066 · SEC-001 · contracts §1: allowlist de hosts de assets
    #: (thumbnail/poster viven en `media.redgifs.com`); fail-closed.
    asset_hosts: list[str] = RG_ASSET_HOSTS

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        """Crea el adapter con su cliente HTTP seguro (allowlist de `api.redgifs.com`).

        Args:
            transport: transporte inyectable (`httpx.MockTransport` en tests);
                `None` → red real (solo uso operativo tras habilitación).
            resolver: resolución inyectable para la validación anti-DNS-rebinding;
                con un `httpx.MockTransport` sin resolver explícito se usa una
                IP pública fija para mantener los tests sin DNS real.
        """
        if isinstance(transport, httpx.MockTransport) and resolver is None:

            def mock_resolver(_host: str) -> list[str]:
                return ["93.184.216.34"]

            resolver = mock_resolver
        self._client = SafeHTTPClient(
            allowed_hosts=RG_API_HOSTS,
            transport=transport,
            validate_resolved_ip=True,
            resolver=resolver,
        )
        # Token temporal (SEC-005): solo en memoria de esta instancia, nunca
        # logueado ni persistido. `None` → se obtiene bajo demanda.
        self._token: str | None = None
        # Evita peticiones de token duplicadas bajo concurrencia (hallazgo de
        # revisión independiente, 2026-08-20): sin lock, dos corrutinas que ven
        # `self._token is None` (o un 401) a la vez dispararían dos GET
        # /v2/auth/temporary; con `asyncio.Lock` + doble comprobación, solo una
        # lo hace y la otra reutiliza el resultado.
        self._token_lock = asyncio.Lock()
        # Anti-bucle de discover (paridad PR-043): IDs ya vistos, **por
        # nicho** (hallazgo de revisión independiente, 2026-08-20: esta
        # instancia es un singleton compartido por todos los jobs de la
        # fuente — sin esta clave, dos cadenas de discover concurrentes sobre
        # nichos distintos se contaminarían entre sí, disparando el anti-bucle
        # de una cadena con los IDs de otra). Se reinicia por nicho al
        # arrancar una cadena nueva (cursor=None).
        self._seen_external_ids_by_niche: dict[str, set[str]] = {}
        # Cache en memoria de objetos gif ya vistos en un listado (ADR-0016
        # §5): evita un segundo GET en `get_video` cuando el ítem viene del
        # discover más reciente. Puramente una optimización; no forma parte
        # del contrato. `get_video` la lee sin consumirla (`get`, no `pop`):
        # el pipeline llama a `get_video` DOS veces por vídeo por diseño
        # (dentro de `discover()` y de nuevo en el job `FETCH_METADATA`
        # posterior) — consumirla de un solo uso rompería la segunda
        # llamada. Para no crecer sin límite en un worker de larga duración
        # se acota con evicción FIFO (`_GIF_CACHE_MAX_ENTRIES`, hallazgo de
        # revisión independiente, 2026-08-20).
        self._gif_cache: OrderedDict[str, VideoSource] = OrderedDict()

    # -- Token temporal (SEC-005) ----------------------------------------------

    async def _fetch_token(self) -> str:
        """`GET /v2/auth/temporary` → el JWT del campo `token`.

        Raises:
            RedgifsAuthError: HTTP no-2xx, JSON inválido o sin campo `token`
                string no vacío. El mensaje nunca incluye el valor del token.
        """
        response = await self._client.get(RG_AUTH_TEMPORARY_URL)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise RedgifsAuthError("respuesta de /v2/auth/temporary no es JSON válido") from exc
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise RedgifsAuthError(
                "respuesta de /v2/auth/temporary sin campo 'token' string no vacío"
            )
        return token

    async def _ensure_token(self) -> str:
        """Token cacheado en memoria; lo obtiene la primera vez que se necesita.

        Doble comprobación bajo `_token_lock` (hallazgo de revisión
        independiente, 2026-08-20): sin ella, dos corrutinas concurrentes que
        ven `self._token is None` a la vez dispararían dos GET
        `/v2/auth/temporary`; con el lock, la segunda espera y reutiliza el
        token que ya obtuvo la primera.
        """
        if self._token is None:
            async with self._token_lock:
                if self._token is None:
                    self._token = await self._fetch_token()
        return self._token

    async def _authorized_get(self, url: str) -> httpx.Response:
        """GET con `Authorization: Bearer <token>`, con una renovación ante 401.

        Ante `401` renueva el token (una vez) y repite la petición; si la
        segunda respuesta también falla, se propaga tal cual (fallo contenido
        en la fuente, sin reintento infinito — el worker de jobs aplica su
        propio backoff por encima). La renovación va bajo `_token_lock` con
        doble comprobación (`self._token == token`): si otra corrutina ya
        renovó mientras esta esperaba, se reutiliza ese token nuevo en vez de
        pedir uno más (mismo hallazgo que `_ensure_token`).
        """
        token = await self._ensure_token()
        response = await self._client.get(url, headers=_bearer_header(token))
        if response.status_code == 401:
            async with self._token_lock:
                if self._token == token:
                    self._token = await self._fetch_token()
                refreshed = self._token
            assert refreshed is not None  # noqa: S101 — recién asignado por _fetch_token
            response = await self._client.get(url, headers=_bearer_header(refreshed))
        return response

    # -- FR-003 · discover ------------------------------------------------------

    async def discover(
        self, *, cursor: str | None, limit: int, section: str | None = None
    ) -> DiscoverPage:
        """Descubre IDs externos de un nicho (FR-003 · Decisión D2 · PR-066).

        **`section` OBLIGATORIO con prefijo `/niches/`** (Decisión D2 de la
        spec 008): en v1 no se exploran tags ni búsqueda de texto; sin
        `section` (o con un prefijo distinto) el discover se rechaza con
        `ValueError` claro (fail-fast).

        `cursor` es el número de página como string (`None` → página 1).
        Paginación por `page`, con `count=min(limit, 100)` (100 es el máximo
        aceptado por la API, verificado en prospección): `limit` es quien
        fija el tamaño de página pedido, nunca un valor fijo — pedir más IDs
        de los que `limit` admite provocaría la misma trampa de truncación
        que en xhamster. Aun así, si la API devolviera más IDs de los
        pedidos, la truncación sigue sin soportarse (error tipado, nunca se
        descartan IDs en silencio). `limit` MUST ser >= 1 (fail-fast: hallazgo
        de revisión independiente, 2026-08-20 — un `limit<=0` ya no cae al
        tamaño de página fijo de 100, que reproduciría la misma trampa de
        truncación con `limit=0`).

        **Protección anti-bucle** (paridad PR-043): 0 IDs nuevos (no vistos
        **en este nicho**, hallazgo de revisión independiente 2026-08-20 — el
        anti-bucle está aislado por `niche_id`, porque esta instancia es un
        singleton compartido por todos los jobs de la fuente y dos cadenas de
        discover concurrentes sobre nichos distintos no deben contaminarse) o
        `page >= pages` → `next_cursor=None` (fin de la cadena).
        """
        if limit < 1:
            raise ValueError(f"limit debe ser >= 1; recibido {limit}")
        if section is None or not section.startswith(_NICHES_PREFIX):
            raise ValueError(
                f"section debe empezar por '/niches/' (D2); recibido {section!r} — "
                "en v1 redgifs solo explora nichos, no tags ni búsqueda de texto"
            )
        # Exactamente un segmento de nicho tras el prefijo (se tolera un '/'
        # final): "/niches/homemade/videos" NO es un nicho válido — fail-fast
        # en vez de resolver silenciosamente al primer segmento (hallazgo de
        # revisión independiente, 2026-08-20).
        niche_segments = [part for part in section[len(_NICHES_PREFIX) :].split("/") if part]
        if len(niche_segments) != 1:
            raise ValueError(
                f"section debe ser exactamente '/niches/<id>' (D2); recibido {section!r}"
            )
        niche_id = niche_segments[0]

        seen_for_niche = self._seen_external_ids_by_niche.setdefault(niche_id, set())
        if cursor is None:
            seen_for_niche.clear()
            page = 1
        else:
            try:
                page = int(cursor)
            except ValueError as exc:
                raise RedgifsParseError(
                    f"cursor inválido para redgifs (se esperaba un nº de página): {cursor!r}"
                ) from exc
            if page < 1:
                raise RedgifsParseError(f"cursor de página inválido: {page}")

        count = min(limit, _PAGE_COUNT)
        url = f"{RG_API_BASE}/v2/niches/{niche_id}/gifs?order=new&count={count}&page={page}"
        response = await self._authorized_get(url)
        response.raise_for_status()
        envelope = parse_niche_gifs_envelope(response.json())

        if len(envelope.external_ids) > limit:
            raise RedgifsParseError(
                f"la página trae {len(envelope.external_ids)} IDs con limit={limit}; "
                "limit debe ser >= tamaño de página (truncación no soportada)"
            )

        for gif_id, video in envelope.gifs_by_id.items():
            self._cache_gif(gif_id, video)
        new_ids = [
            external_id
            for external_id in envelope.external_ids
            if external_id not in seen_for_niche
        ]
        seen_for_niche.update(envelope.external_ids)

        if not new_ids:
            # Anti-bucle: 0 IDs nuevos (en este nicho) → fin.
            return DiscoverPage(external_ids=envelope.external_ids, next_cursor=None)
        if envelope.pages is not None and page >= envelope.pages:
            return DiscoverPage(external_ids=envelope.external_ids, next_cursor=None)
        return DiscoverPage(external_ids=envelope.external_ids, next_cursor=str(page + 1))

    # -- FR-004 · get_video -----------------------------------------------------

    async def get_video(
        self, external_id: str, *, page_url: str | None = None
    ) -> VideoSource | None:
        """Obtiene la metadata normalizada de un gif (FR-004).

        `page_url` se ignora siempre (Decisión D5): la referencia canónica es
        fija (`RG_WATCH_URL_TEMPLATE`) y nunca se fetchea. Si el ítem viene de
        un `discover()` reciente de esta misma instancia, se usa la cache en
        memoria (sin segundo GET); si no, se pide `GET /v2/gifs/<id>`. `None`
        solo para `404` (`GifNotFound`, ítem retirado); cualquier otro error
        HTTP o de estructura se propaga.

        La cache **no** se consume de un solo uso (`get`, no `pop`): el
        pipeline llama a `get_video` dos veces por vídeo por diseño (dentro
        de `discover()` y de nuevo en el job `FETCH_METADATA` posterior); se
        acota con evicción FIFO (`_cache_gif`) en vez de con consumo único.
        """
        normalized_id = external_id.lower()
        cached = self._gif_cache.get(normalized_id)
        if cached is not None:
            return cached
        response = await self._authorized_get(f"{RG_API_BASE}/v2/gifs/{normalized_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        video = parse_gif_response(response.json())
        self._cache_gif(normalized_id, video)
        return video

    def _cache_gif(self, external_id: str, video: VideoSource) -> None:
        """Inserta en `_gif_cache` con evicción FIFO (tope `_GIF_CACHE_MAX_ENTRIES`)."""
        self._gif_cache[external_id] = video
        self._gif_cache.move_to_end(external_id)
        while len(self._gif_cache) > _GIF_CACHE_MAX_ENTRIES:
            self._gif_cache.popitem(last=False)

    # -- FR-005 · get_visual_assets (SC-004/SC-006) -----------------------------

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        """Devuelve thumbnail + poster, ambos `kind="thumbnail"`, sin timestamp (FR-005).

        Redgifs no tiene storyboard/sprite: como mucho dos `VisualAsset` (uno
        por `video.thumbnail_url`, otro por `video.storyboard_urls[0]` — el
        poster, reutilizando ese campo como contenedor genérico, ADR-0016
        §4), ninguno con `position`/`timestamp_ms`. Ausencia de alguno de los
        dos → degradación sin fallar (paridad FR-012 del spike). **Nunca**
        expone `urls.sd`/`hd`/`silent` (SC-004/SC-006): este método solo lee
        los dos campos ya normalizados en `VideoSource`.
        """
        assets: list[VisualAsset] = []
        if video.thumbnail_url is not None:
            assets.append(VisualAsset(kind="thumbnail", url=video.thumbnail_url))
        if video.storyboard_urls:
            assets.append(VisualAsset(kind="thumbnail", url=video.storyboard_urls[0]))
        return assets

    # -- FR-001 · check_availability ---------------------------------------------

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        """Comprueba la disponibilidad vía `GET /v2/gifs/<id>` (FR-001).

        `404` (`GifNotFound`) → `removed` (terminal, sin reintentos); objeto
        gif válido → `available`; cualquier otro error (HTTP o de estructura)
        → `unavailable` (no se puede confirmar ahora).
        """
        response = await self._authorized_get(f"{RG_API_BASE}/v2/gifs/{video.external_id}")
        if response.status_code == 404:
            return VideoAvailability.REMOVED
        try:
            response.raise_for_status()
            parse_gif_response(response.json())
        except (httpx.HTTPError, RedgifsParseError):
            return VideoAvailability.UNAVAILABLE
        return VideoAvailability.AVAILABLE

    # -- Ciclo de vida -----------------------------------------------------------

    async def aclose(self) -> None:
        """Cierra el cliente HTTP subyacente."""
        await self._client.aclose()
