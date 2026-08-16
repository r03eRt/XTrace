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
    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage: ...
    async def get_video(self, external_id: str) -> VideoSource | None: ...
    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]: ...
    async def check_availability(self, video: VideoSource) -> VideoAvailability: ...
```

- `DiscoverPage`: `external_ids: list[str]`, `next_cursor: str | None`.
- `VideoAvailability`: `available | unavailable | removed` (con razón opcional).
- Regla de oro: el **core nunca ve HTML/JSON de la web**; solo `VideoSource`/`VisualAsset`.
- `registry.py` no permite instanciar/habilitar un adapter real sin `robots_reviewed` y
  `terms_reviewed` en `true` + `enabled=true` en `sources` (SEC-002).
- **Unicidad de `RateLimitSpec`**: existe UNA sola definición canónica
  (`adapters/base.py`); `crawling/ratelimit.py` la **importa** y no redefine (alineación
  exigida a PR-030).

## 2. Entidad normalizada `VideoSource` — FR-002

> Nombre canónico del campo de duración: **`duration_ms`** (la spec/ADR lo citan como
> `duration`; la implementación y esta sección usan `duration_ms` en ms).

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

## 5. CLI (`xtrace-crawler`, Typer)

Salida **JSON** por stdout (tests/observabilidad); logs por stderr.

```
xtrace-crawler sources [--json]
xtrace-crawler backfill --source <name> [--limit N] [--incremental]
xtrace-crawler run-worker [--concurrency N] [--once]
xtrace-crawler stats [--json]
xtrace-crawler check-availability --source <name> [--limit N]
```

- `backfill` encola `DISCOVER`; sin `--incremental` es BACKFILL (FR-007).
- `stats` → jobs por estado/fuente, vídeos descubiertos/indexados/fallidos, errores
  recientes, rate-limit waits (FR-014).

## 6. Frontera con el spike (ADR-0011)

- El crawler **importa** `xtrace_spike` (editable) y reutiliza: `pHash`,
  `EmbeddingProvider` (SigLIP/fake), `VectorStore`/`PgVectorStore`, ranking y exclusión.
- El spike **no** se modifica; cualquier cambio necesario en él es un PR propio trazado a
  esta spec.

## 7. Invariantes

- **Nunca** se descarga un vídeo completo (solo storyboard/thumbnail/preview) — SC-006.
- Solo hosts permitidos por adapter (allowlist); sin redirects fuera de ella (SEC).
- Sin secretos en el repo; configuración por env (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
  compartidos con el spike).
- Toda media descargada es temporal, en directorio gitignored, con cleanup `try/finally`.
