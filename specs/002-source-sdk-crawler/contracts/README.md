# Contracts — Source SDK + Primer Crawler

Contratos estables que los implementadores deben respetar. Cambios a estos contratos
requieren actualizar la spec/plan primero.

## 1. `SourceAdapter` (ABC, async) — FR-001 / ADR-0009

> **Decisión de implementación (PR-020, revisado APPROVED)**: `AdapterManifest` se
> implementa como **modelo pydantic `frozen` + `strict`** (en vez de `TypedDict`): exige
> compliance en runtime (clave para SEC-002) e inmutabilidad. La firma funcional es la de
> abajo.

```python
class RateLimitSpec(BaseModel):          # CANÓNICO — definido en adapters/base.py
    min_interval_ms: int                 # default razonable del adapter
    max_rps: float = Field(gt=0)         # > 0 siempre (evita divisiones por cero)

class AdapterManifest(BaseModel):        # frozen + strict
    source: str                  # "xvideos"
    access_method: str           # jerarquía documentada: "html" | "json" | "sitemap" | "api"
    assets_accessed: list[str]   # ["storyboard", "thumbnail", "preview"] — nunca "video"
    robots_reviewed: bool        # False => adapter no habilitable
    terms_reviewed: bool         # False => adapter no habilitable
    rate_limit: RateLimitSpec    # defaults (D5)
    review_date: str | None      # ISO date de la revisión legal humana

class SourceAdapter(Protocol):
    manifest: AdapterManifest
    # PR-049 (enmienda): `section` OPCIONAL (kwarg, default None) — ruta de
    # sección del sitio (categoría/tag, p. ej. `/tags/xxx`, SIEMPRE con '/'
    # inicial): la fuente la usa como URL INICIAL del discover
    # (`https://www.xvideos.com<section>`) en vez de la home; con cursor, la
    # URL sale del cursor. None → home (retrocompatible: el MockAdapter
    # acepta el kwarg y lo ignora).
    async def discover(self, *, cursor: str | None, limit: int, section: str | None = None) -> DiscoverPage: ...
    # PR-045 (enmienda): `page_url` OPCIONAL (kwarg, default None) — href
    # COMPLETO del listado (p. ej. /video.<id>/<num>/<num>/<slug>) para fuentes
    # cuyo URL canónico exige el slug; None → la fuente reconstruye como antes.
    async def get_video(self, external_id: str, *, page_url: str | None = None) -> VideoSource | None: ...
    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]: ...
    async def check_availability(self, video: VideoSource) -> VideoAvailability: ...
    # OPCIONAL (PR-034, flujo offline FR-003/SC-001): si existe y devuelve bytes,
    # el pipeline los usa en lugar de descargar la URL; None => descarga HTTP normal.
    # No se declara en el cuerpo del Protocol por conformidad estructural (mypy strict);
    # se detecta con getattr(adapter, "fetch_asset_bytes", None).
    async def fetch_asset_bytes(self, url: str) -> bytes | None: ...
```

- `DiscoverPage`: `external_ids: list[str]`, `next_cursor: str | None`,
  `page_urls: dict[str, str]` (**PR-045**, OPCIONAL con default `{}` →
  retrocompatible: external_id → **href completo del listado**, p. ej.
  `/video.<id>/<num>/<num>/<slug-titulo>`; lo consume `get_video(page_url=...)`).
- `VideoAvailability`: `available | unavailable | removed` (con razón opcional).
- Regla de oro: el **core nunca ve HTML/JSON de la web**; solo `VideoSource`/`VisualAsset`.
- `registry.py` no permite instanciar/habilitar un adapter real sin `robots_reviewed` y
  `terms_reviewed` en `true` + `enabled=true` en `sources` (SEC-002).
- **Unicidad de `RateLimitSpec`**: existe UNA sola definición canónica
  (`adapters/base.py`); `crawling/ratelimit.py` la **importa** y no redefine (alineación
  exigida a PR-030).
- **Allowlist de hosts de assets por fuente (PR-036 · SEC-001)**: todo adapter real DEBE
  declarar el atributo de instancia `asset_hosts` (`list[str]`; `SafeHTTPClient`
  acepta también `set`/`frozenset` — declaración canónica en los adapters): hosts
  **revisados** de sus assets (dominio canónico + CDNs de imágenes/vídeo documentados).
  El pipeline **NO deriva la allowlist de las URLs parseadas**: un asset cuya URL apunte
  a un host fuera de `asset_hosts` se rechaza con `HostNotAllowedError` (degradación por
  asset, sin red); un adapter real sin `asset_hosts` declarado **NO descarga assets por
  HTTP** (`NoAssetHostsError`, fail-closed). El `MockAdapter` (FR-003) declara
  `asset_hosts = []` (lista vacía, fail-closed igualmente): sirve sus assets in-process
  (`fetch_asset_bytes`, PR-034) y el preview (sin representación) degrada — 0 superficie
  de red. El cliente de assets
  del pipeline además valida la **IP resuelta** de cada host (anti-DNS-rebinding, §7).
- **Decompression bomb (PR-036)**: toda imagen descargada se abre con un límite estricto
  de píxeles (`XTRACE_CRAWLER_MAX_IMAGE_PIXELS`, default 50 MP; verificado por header
  antes de decodificar) → `ImageTooManyPixelsError` tipado y degradación por asset.

> **Enmienda PR-045 (3a validación real, 2026-08-16)**: `DiscoverPage.page_urls`
> (opcional, default `{}`) + `get_video(page_url: str | None = None)` (kwarg
> opcional). Motivación: el discover real de xvideos parsea los IDs de la home,
> pero la página de vídeo reconstruida como `https://www.xvideos.com/video.<id>/`
> **sin slug devuelve 404 en todos**; el href real del listado es
> `/video.<id>/<num>/<num>/<slug-titulo>` y es la ÚNICA URL que la fuente acepta.
> Durante DISCOVER el pipeline reenvía `page.page_urls[external_id]` a
> `get_video`; `None` (p. ej. FETCH_METADATA, fuentes sin page_urls) → la fuente
> reconstruye su URL como antes. Retrocompatible: los llamadores/adapters
> existentes no cambian su comportamiento (el `MockAdapter` acepta el kwarg y lo
> ignora; `page_urls` vacío por defecto).

## 2. Entidad normalizada `VideoSource` — FR-002

> Nombre canónico del campo de duración: **`duration_ms`** (ms; alineado en la spec con
> PR-039 — FR-002 y Key Entities; solo el ADR-0009 lo cita como `duration`; la
> implementación y esta sección usan `duration_ms`).

```python
class VideoSource(BaseModel):          # pydantic, validación estricta
    source: str                        # nombre canónico ("xvideos")
    external_id: str
    title: str | None
    page_url: str                      # http(s)
    duration_ms: int | None
    thumbnail_url: str | None
    preview_url: str | None
    storyboard_urls: list[str]         # vacía si no hay
    tags: list[str]
    published_at: datetime | None
```

`VisualAsset`:
```python
class VisualAsset(BaseModel):
    kind: Literal["storyboard", "thumbnail", "preview"]
    url: str
    position: int | None          # índice de tile en storyboard / orden
    timestamp_ms: int | None      # cuando la fuente lo expone
```

## 3. Jobs — FR-006 / ADR-0010

- Tipos: `DISCOVER | FETCH_METADATA | INDEX_VIDEO | EXTRACT_FRAMES |
  GENERATE_EMBEDDINGS | CHECK_AVAILABILITY | REINDEX`.
- Estados: `pending | running | done | failed | unavailable`.
- Despacho: `SELECT … WHERE status='pending' AND not_before<=now() ORDER BY created_at
  FOR UPDATE SKIP LOCKED LIMIT 1` + marca `running`/`locked_by`/`locked_at` en la misma
  transacción.
- Retries: backoff exponencial base 1 s, factor 2, cap 1 h, **jitter completo**; terminales
  (404/removed, bloqueo robots/ToS) van a `unavailable`/`failed` definitivo sin reintentos
  (FR-008).
- Lease reset de `running` vencidos → `pending` (crash del worker).

## 4. Rate limits — FR-009 / Decisión D5

- Defaults en el manifest de cada adapter.
- Overrides por entorno (sin tocar código):
  - `XTRACE_CRAWLER_RATE_<SOURCE>_MIN_INTERVAL_MS` (intervalo mínimo entre requests)
  - `XTRACE_CRAWLER_RATE_<SOURCE>_MAX_RPS` (límite sostenido)
- Implementación con jitter; esperas medibles en logs (SC-005).

> **Nota (PR-039 · limitación conocida, security-review P2)**: el worker toma un
> **snapshot de `sources.enabled` al arrancar** (`cli.py` → `_enabled_snapshot`); para
> **deshabilitar una fuente hay que reiniciar el worker** — los cambios de `enabled` no se
> aplican a jobs ya en vuelo.

## 5. CLI (`xtrace-crawler`, Typer)

Salida **JSON** por stdout (tests/observabilidad); logs por stderr.

```
xtrace-crawler sources [--json]
xtrace-crawler backfill --source <name> [--limit N] [--max-videos N] [--incremental] [--section <path>]
xtrace-crawler run-worker [--concurrency N] [--once]
xtrace-crawler stats [--json]
xtrace-crawler check-availability --source <name> [--limit N]
```

- `backfill` encola `DISCOVER`; sin `--incremental` es BACKFILL (FR-007).
- **`--section <path>` (PR-049 · FR-007 · pruebas del operador)**: acota el
  discover a una **sección** del sitio (categoría/tag, p. ej. `/tags/xxx`).
  El path SIEMPRE empieza por '/' (validado: sin la barra inicial → error de
  uso, exit 2). El payload del DISCOVER incluye `section` (**null si no se
  da**) y el adapter la usa como **URL INICIAL** del discover
  (`https://www.xvideos.com<section>`) en vez de la home; el cursor, la
  paginación (`a.dir.next` de la página de la sección) y el anti-bucle son
  idénticos. El pipeline la propaga al siguiente DISCOVER (payload y
  `dedupe_key` de la cadena) y el JSON de salida del backfill incluye
  `section` cuando se da. Retrocompatible: sin `--section` el comportamiento
  es exactamente el previo (el MockAdapter acepta el kwarg y lo ignora).
- **`--limit` de `backfill` es el TAMAÑO DE PÁGINA de DISCOVER** (vídeos devueltos por
  página del catálogo), **no** una cota global del backfill: el handler de PR-030 encola el
  siguiente `DISCOVER` con el cursor hasta agotar el catálogo.
- **Cota global `--max-videos` (PR-036 · analyze hallazgo 2 · SC-002)**: cota GLOBAL de
  vídeos del backfill; el payload del DISCOVER la incluye (`max_videos`) y el pipeline
  corta la cadena de paginación al alcanzarla, **acumulando vídeos ya conocidos y nuevos**
  (`videos_counted` fluye por payload entre páginas) con log claro. Default:
  `XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS` (100). Es **OBLIGATORIA** para el backfill real de
  xvideos (SC-002: backfill acotado por el operador).
- `stats` → jobs por estado/fuente, vídeos descubiertos/indexados/fallidos, errores
  recientes (FR-014) y sección **`rate_limits` por fuente** (PR-035 · SC-005/NFR-004):
  `requests`, `rate_limit_waits` y `total_wait_ms` acumulados del `RateLimiter` del
  proceso que ejecutó el pipeline (contabilidad en memoria; el `run-worker` deja el
  resumen en logs).

## 6. Frontera con el spike (ADR-0011)

- El crawler **importa** `xtrace_spike` (editable) y reutiliza: `pHash`,
  `EmbeddingProvider` (SigLIP/fake), `VectorStore`/`PgVectorStore`, ranking y exclusión.
- El spike **no** se modifica; cualquier cambio necesario en él es un PR propio trazado a
  esta spec.
- **Proveedor de embeddings por env (PR-050 · FR-011)**: env `XTRACE_CRAWLER_EMBEDDINGS`
  (`fake` | `siglip`, default **`fake`** — igual que hoy):
  - `fake` (default) → `CliContext.embeddings=None`: el pipeline (PR-030) usa el
    `FakeEmbeddingProvider` determinista de `xtrace_spike` (dimensión del esquema;
    tests/CI sin torch).
  - `siglip` → el CLI construye el pipeline con el **`SiglipLocalProvider` REAL** de
    `xtrace_spike.embeddings.siglip_local` (ADR-0011): instanciado **sin argumentos**
    (paridad con el CLI del spike, PR-005: `ViT-B-16-SigLIP`/`webli`, D=768, embeddings
    L2-normalizados) para las **validaciones reales del operador** (FR-011). El
    constructor no carga torch (carga lazy en el primer uso, PR-005).
  - El switch solo se evalúa en el **contexto por defecto real** del CLI
    (`_default_context`); `CliContext.embeddings` sigue siendo **inyectable en tests**
    (sin torch en CI, NFR-003). Un valor distinto de `fake`/`siglip` falla al construir
    `Settings` (fail-fast).

## 7. Invariantes

- **Nunca** se descarga un vídeo completo (solo storyboard/thumbnail/preview) — SC-006.
- Solo hosts permitidos por adapter (allowlist declarada por fuente, PR-036); sin redirects
  fuera de ella (SEC). **Anti-DNS-rebinding (PR-036)**: el cliente de assets del pipeline
  valida la **IP resuelta** de cada host de asset en cada petición (incluidos redirects) y
  rechaza rangos privados/link-local/loopback/metadata (RFC1918, 169.254.0.0/16 —
  incluida 169.254.169.254 —, 127.0.0.0/8, ::1, fc00::/7, fe80::/10) con `PrivateIPError`.
- **Decompression bomb (PR-036)**: límite estricto de píxeles al abrir imágenes
  (`XTRACE_CRAWLER_MAX_IMAGE_PIXELS`, default 50 MP), verificado por header antes de
  decodificar; excederlo es `ImageTooManyPixelsError` (degradación por asset).
- Sin secretos en el repo; configuración por env (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
  compartidos con el spike).
- Toda media descargada es temporal, en directorio gitignored, con cleanup `try/finally`.
