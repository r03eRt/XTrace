# Implementation Plan: Source SDK + Primer Crawler (XTrace)

**Branch**: `feature/002-source-sdk-crawler` | **Date**: 2026-08-15 | **Spec**: [`spec.md`](./spec.md)

**Input**: Feature specification from `specs/002-source-sdk-crawler/spec.md` (`APPROVED`).

> Este plan define **cómo** se implementa la spec aprobada. No altera requisitos.
> Decisiones registradas como ADRs en `docs/adr/`.

## Summary

Construir un **servicio Python de crawling** (`services/crawler/`, paquete `xtrace_crawler`)
que define el contrato **`SourceAdapter`** + la entidad normalizada **`VideoSource`** como
única frontera entre fuentes web y el core de indexación/búsqueda del spike, con **mock
adapter + fixtures + harness** para ejecutar todo el flujo **sin red**. El primer adapter
real es **xvideos.com** (HTML, storyboards/sprite + thumbnails), habilitado **solo** tras la
revisión legal/ToS/robots del humano (manifest de compliance como puerta).

El crawler persiste sus trabajos en una tabla **`jobs`** en la **misma Supabase local
(Docker)** del spike y los despacha con **`FOR UPDATE SKIP LOCKED`** (sin Redis), con modos
**BACKFILL/INCREMENTAL**, retries con backoff exponencial + jitter, rate limits por adapter
(defaults en código + override por env) y aislamiento de fallos por fuente. Los visual assets
(storyboard/sprite → thumbnails → preview; **nunca** el vídeo completo) se convierten en
frames con timestamp e indexan **reutilizando el pipeline del spike** (pHash + SigLIP +
pgvector).

## Technical Context

**Language/Version**: Python 3.11 (mismo toolchain que el spike: `uv` + `ruff` + `mypy` +
`pytest` + `typer` + `pydantic-settings`). El skeleton Next.js **no se toca**.

**Primary Dependencies**:
- HTTP/parsing: `httpx` (async), `selectolax` (HTML del adapter xvideos). Sin navegador en
  esta fase (último recurso de la jerarquía de acceso).
- Reutilización del spike: `xtrace_spike` como **dependencia de camino editable**
  (pHash, `EmbeddingProvider`, `VectorStore`/`PgVectorStore`, ranking) — ADR-0011.
- Proceso de imagen: `Pillow` (crop de tiles de storyboard). `ffmpeg` solo para assets
  `preview` (vídeo corto), nunca para vídeos completos.
- Cola: **Postgres** (`psycopg`/`asyncpg` como en el spike) — ADR-0010.

**Storage**: Supabase PostgreSQL local: nuevas tablas `sources`, `jobs`; ampliación de
`videos` (source/external_id/metadatos web). Sin almacenamiento permanente de media.

**Testing**: `pytest` (unit + integration, sin red en CI) + `pgTAP` (`supabase test db`) para
esquema/RLS. WebdriverIO N/A en esta feature (sin UI).

**Target Platform**: Ejecución local (CLI) en Linux/macOS + Docker; Supabase local.

**Project Type**: Servicio/CLI Python en monorepo (segundo servicio, junto a
`services/search-spike/`).

**Performance Goals**: El crawler no tiene latencia crítica de usuario; objetivo operativo:
backfill acotado (≤ N vídeos de prueba) completado en local con coste ~0 € y **sin violar
los rate limits declarados** (SC-005).

**Constraints**: Coste dev **0–10 €/mes**. Sin Redis. Sin descargas de vídeo completo
(SC-006). Adapters aislados (SC-007/SC-008). Fixtures deterministas sin red (SC-001).

**Scale/Scope**: Una fuente (xvideos), backfill de validación acotado (límite decidido por
el operador, p. ej. ≤ 100 vídeos). Sin escalado horizontal de workers.

## Constitution Check

_GATE: debe pasar antes de implementar._

- **Spec-first** ✔ Spec `APPROVED` (2026-08-15) antes de este plan.
- **Aprobación humana** ✔ Registrada (frase exacta "Especificación aprobada").
- **Trazabilidad** ✔ Cada componente mapea FR/SEC/DATA/SC (ver *Requirements coverage*).
- **PRs aislados** ✔ Roadmap en `tasks.md`: PRs pequeños, sin `XL`.
- **Multiagente** ✔ Plan pensado para orquestador + implementador (subagentes
  `deepseek-v4-flash`) + revisor independiente: `allowed_paths` por tarea, handoffs.
- **Testing test-first** ✔ Retry/backoff, rate limiter, jobs y adapters con tests primero.
- **Seguridad** ✔ RLS deny-by-default en tablas nuevas; `service_role` solo en servidor;
  validación de URLs de assets (anti-SSRF); **prohibido** saltar auth/paywalls/CAPTCHA/DRM
  (SEC-001); manifest de compliance como puerta de habilitación (SEC-002).

Sin violaciones que justificar → *Complexity Tracking* vacío.

## Project Structure

### Documentation (this feature)

```text
specs/002-source-sdk-crawler/
├── spec.md            # Qué/por qué (APPROVED)
├── plan.md            # Este archivo (cómo)
├── data-model.md      # Modelo de datos detallado (sources/jobs/videos-web)
├── contracts/         # Contratos SourceAdapter/VideoSource/jobs/CLI/rate limits
├── quickstart.md      # Cómo ejecutar backfill/worker/stats en local
└── tasks.md           # Roadmap de PRs (creado por task-planning)
```

### Source Code (repository root)

```text
services/
└── crawler/                          # Servicio Python de crawling (nuevo)
    ├── pyproject.toml                # xtrace_crawler; dep editable → ../search-spike
    ├── README.md
    ├── Dockerfile
    ├── xtrace_crawler/
    │   ├── __init__.py
    │   ├── cli.py                    # Typer: sources | backfill | run-worker | stats | check-availability
    │   ├── config.py                 # pydantic-settings: DB, rate limits (D5), retries
    │   ├── adapters/
    │   │   ├── base.py               # SourceAdapter (ABC) + AdapterManifest (FR-001, ADR-0009)
    │   │   ├── models.py             # VideoSource (FR-002)
    │   │   ├── mock.py               # MockAdapter determinista (FR-003)
    │   │   ├── xvideos.py            # XvideosAdapter — deshabilitado sin manifest revisado
    │   │   └── registry.py           # registro de adapters + gate de habilitación (SEC-002)
    │   ├── crawling/
    │   │   ├── discover.py           # orquesta discover por fuente (BACKFILL/INCREMENTAL) (FR-007)
    │   │   ├── ratelimit.py          # rate limiter por adapter (FR-009)
    │   │   └── http.py               # httpx client seguro: allowlist host, timeout, UA (SEC)
    │   ├── jobs/
    │   │   ├── repo.py               # encolar/leer con FOR UPDATE SKIP LOCKED (FR-006)
    │   │   ├── worker.py             # bucle worker + lease reset (FR-008, edge crash)
    │   │   ├── backoff.py            # backoff exponencial + jitter (FR-008)
    │   │   └── types.py              # JobType/JobStatus
    │   ├── assets/
    │   │   ├── fetch.py              # descarga de assets permitidos (FR-005)
    │   │   ├── storyboard.py         # crop de tiles + timestamp aproximado
    │   │   └── preview.py            # ffmpeg sobre previews cortos (nunca vídeo completo)
    │   ├── pipeline.py               # metadata→assets→frames→hash→embed→índice (FR-011)
    │   └── repo.py                   # sources/videos-web/stats (FR-012/013/014)
    └── tests/
        ├── unit/                     # adapters, backoff, ratelimit, storyboard, worker
        ├── integration/              # jobs + pipeline con MockAdapter contra Supabase local
        └── fixtures/                 # HTML/JSON sintéticos + MockAdapter (sin red)

supabase/
└── migrations/
    └── <ts>_source_sdk_crawler.sql   # sources + jobs + ampliación videos (no destructiva)

supabase/tests/
    └── source_sdk_crawler_schema.test.sql  # pgTAP: tablas, constraints, índices, RLS
```

**Structure Decision**: Monorepo. Segundo servicio Python en `services/crawler/` (Decisión
D3: SDK dentro del servicio, subpaquete `adapters/`). El spike se **reutiliza** como
dependencia editable (ADR-0011) sin modificar sus ficheros. La DB se comparte vía
`supabase/migrations`.

## Data model (resumen; detalle en `data-model.md`)

- **sources**: `id` (uuid pk), `name` (text unique), `adapter` (text), `manifest` (jsonb:
  access method, assets accessed, robots reviewed, terms reviewed, rate limit, review date),
  `enabled` (bool default false — gate SEC-002), `rate_limit` (jsonb: defaults),
  `created_at`, `updated_at`.
- **videos** (ampliación no destructiva): + `source_id` (uuid null FK→sources),
  `external_id` (text null), `page_url`, `title`, `tags` (jsonb), `published_at`
  (timestamptz), `thumbnail_url`, `preview_url`, `storyboard_urls` (jsonb). Unicidad:
  `UNIQUE(source_id, external_id)` parcial (solo filas web); `local_ref` sigue siendo único
  para vídeos locales (DATA-001/DATA-003). Estado ampliado con `unavailable`/`removed`
  (FR-012).
- **jobs**: `id` (uuid pk), `job_type` (DISCOVER/FETCH_METADATA/INDEX_VIDEO/EXTRACT_FRAMES/
  GENERATE_EMBEDDINGS/CHECK_AVAILABILITY/REINDEX), `status` (pending/running/done/failed/
  unavailable), `source_id`, `video_id`, `payload` (jsonb), `attempts`, `max_attempts`,
  `not_before` (timestamptz, backoff), `locked_by`, `locked_at` (lease), `error`,
  `created_at`, `updated_at`. Índice de despacho: `(status, not_before)` (ADR-0010).

RLS: deny-by-default en `sources` y `jobs`; acceso con `service_role` desde el servicio
Python. pgTAP verifica constraints, índices y RLS.

## Contracts (detalle en `contracts/`)

**`SourceAdapter` (ABC, async)** — ADR-0009:
```python
class SourceAdapter(Protocol):
    manifest: AdapterManifest          # compliance + rate limit defaults
    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage
    async def get_video(self, external_id: str) -> VideoSource | None
    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]
    async def check_availability(self, video: VideoSource) -> VideoAvailability
```
`VisualAsset(kind: storyboard|thumbnail|preview, url, position|timestamp_ms?)`.

**CLI (Typer)**:
- `xtrace-crawler sources [--json]` — listar fuentes y manifiestos.
- `xtrace-crawler backfill --source <name> [--limit N] [--incremental]` — encola DISCOVER.
- `xtrace-crawler run-worker [--concurrency N] [--once]` — bucle de jobs (SKIP LOCKED).
- `xtrace-crawler stats [--json]` — jobs por estado/fuente, vídeos descubiertos/indexados.
- `xtrace-crawler check-availability --source <name> [--limit N]`.

**Rate limits** (D5): defaults en el manifest del adapter; overrides por env:
`XTRACE_CRAWLER_RATE_<SOURCE>_MIN_INTERVAL_MS`, `XTRACE_CRAWLER_RATE_<SOURCE>_MAX_RPS`.

## Security strategy

- `service_role` solo en el servicio Python (servidor); RLS deny-by-default en tablas nuevas.
- **Anti-SSRF**: el cliente HTTP solo permite `https` (y `http` en dev explícito) contra
  **hosts allowlist por adapter**; sin redirects fuera del allowlist; descargas a directorio
  temporal dedicado y cleanup en `try/finally` (FR-015).
- **Compliance gate**: `registry.py` no instancia adapters reales con `enabled=true` salvo
  manifest revisado (`robots_reviewed`/`terms_reviewed` + `review_date`) y aprobación humana
  (SEC-002). Prohibido saltar auth/paywalls/CAPTCHA/DRM/anti-bot (SEC-001).
- **Contenido**: los fixtures sintéticos se generan a partir de la **estructura** del HTML
  (títulos anonimizados); ninguna media real se commitea (SEC-004). Los temporales de
  descarga viven en un directorio gitignored.

## Testing strategy

- **Unit (pytest, sin red)**: contrato + `MockAdapter` (SC-001), `backoff.py` (matemática de
  jitter/límites, FR-008), `ratelimit.py` (SC-005), `storyboard.py` (crop + timestamps),
  `worker.py` con repo fake (transiciones de estado, lease), `registry.py` (gate SEC-002),
  `XvideosAdapter` contra fixtures HTML sintéticos (parseo y regresión de estructura).
- **Integration (pytest, Supabase local)**: `jobs` con `FOR UPDATE SKIP LOCKED` real
  (despacho único, lease reset, FR-006/FR-008); pipeline completo con `MockAdapter` →
  frames + embeddings en el índice del spike (SC-002/SC-003/SC-004).
- **DB (pgTAP)**: tablas `sources`/`jobs`, constraints únicos, estados, índices de despacho,
  RLS deny-by-default.
- **E2E de UI**: N/A (sin frontend). El skeleton (`pnpm verify`, `test:e2e:smoke`) intacto.
- Tests marcan el requisito que validan (trazabilidad, constitución §3/§6).

## Deployment / CI strategy

- **Local-first**: CLI + Supabase local (Docker). No despliegue a Vercel en esta feature.
- Nuevo job de CI para `services/crawler/` (mismo patrón que el del spike): `ruff`, `mypy`,
  `pytest` (unit + integration con servicio Postgres/pgvector) y `pgTAP` de la nueva
  migración. La pipeline JS y el job CI del spike permanecen verdes.
- Gate por PR: `ruff && mypy && pytest && supabase test db` para tareas del crawler.
- El backfill contra xvideos **real** se ejecuta manualmente en local por el operador, con
  límite acotado; **nunca** en CI.

## Observability

- Logs estructurados: job id, tipo, fuente, vídeo, intentos, backoff, duración por etapa.
- `stats` expone: jobs por estado/fuente, vídeos descubiertos/indexados/fallidos, errores
  recientes con causa, rate-limit waits acumulados (FR-014).
- Métricas de respeto a límites por fuente (SC-005) y aislamiento (SC-008) verificables.

## Requirements coverage (trazabilidad)

| Requisito | Cubierto por |
| --- | --- |
| FR-001 | `adapters/base.py` (`SourceAdapter` + `AdapterManifest`) |
| FR-002 | `adapters/models.py` (`VideoSource`) |
| FR-003 | `adapters/mock.py` + `tests/fixtures/` + harness |
| FR-004 | manifest (`access_method` con jerarquía documentada) + `adapters/xvideos.py` |
| FR-005 | `assets/fetch.py`, `assets/storyboard.py`, `assets/preview.py` (nunca vídeo completo) |
| FR-006 | `jobs/repo.py` (`FOR UPDATE SKIP LOCKED`) + migración `jobs` |
| FR-007 | `crawling/discover.py` (BACKFILL/INCREMENTAL) |
| FR-008 | `jobs/backoff.py`, `jobs/worker.py` (intentos máx, estados terminales) |
| FR-009 | `crawling/ratelimit.py` + `config.py` (D5) |
| FR-010 | aislamiento por fuente en `worker.py`/`pipeline.py` |
| FR-011 | `pipeline.py` (reutiliza `xtrace_spike`) |
| FR-012 | `repo.py` + migración (estados ampliados, unicidad `(source_id, external_id)`) |
| FR-013 | `repo.py` `exclude` (reutiliza columna del spike) |
| FR-014 | `repo.py` `stats` + CLI `stats` |
| FR-015 | cleanup `try/finally` en `assets/` y `pipeline.py` |
| SEC-001/002/003/004 | `crawling/http.py`, `adapters/registry.py`, RLS pgTAP, política de fixtures |
| DATA-001/002/003 | `data-model.md` + migración no destructiva |
| NFR-001..004 | CI local, aislamiento, mock sin red, rate limits |
| SC-001..008 | tests unit/integration + pgTAP + ejecución manual acotada del operador |

## Risks (plan)

- **Estructura HTML de xvideos cambia** → fixtures sintéticos versionados + adapter aislado;
  el fallo no toca el core (SC-007).
- **Bloqueo/anti-bot legítimo** → rate limits estrictos, backoff con jitter, **sin** intentar
  saltar protecciones; una fuente bloqueada no detiene las demás (SC-008).
- **Revisión legal pendiente** → el adapter xvideos permanece deshabilitado; el desarrollo
  completa SDK, jobs, pipeline y mock sin depender de la fuente.
- **Acoplamiento al mock** → harness y tests contra el contrato; `XvideosAdapter` cubierto
  con fixtures sintéticos de la misma estructura.
- **Reutilización del spike** → dependencia editable de solo lectura; si aparecen conflictos
  de empaquetado se documenta en ADR-0011 (alternativa: extraer paquete compartido).

## ADRs (creados en `docs/adr/`)

- `0009` Contrato `SourceAdapter` + `VideoSource` normalizado + manifest de compliance
  (extiende ADR-0007).
- `0010` Cola de jobs en Postgres con `FOR UPDATE SKIP LOCKED` (sin Redis) + retries.
- `0011` Reutilización del pipeline del spike como dependencia editable (sin extraer
  paquete compartido en esta fase).

## Bloqueos

Ninguno funcional. La **revisión legal/ToS/robots de xvideos** es responsabilidad del humano
y solo condiciona la habilitación del adapter real (no el desarrollo). Próximo paso:
`task-planning` → `tasks.md`.
