# Implementation Plan: Adapter redgifs.com (fuente real vía API oficial)

**Branch**: `feature/008-redgifs-adapter` | **Date**: 2026-08-20 | **Spec**: [`spec.md`](./spec.md)

**Input**: Feature specification from `specs/008-redgifs-adapter/spec.md` (`APPROVED`,
2026-08-19, frase exacta "Especificación aprobada"; implementación reanudada
2026-08-20 por instrucción explícita del humano).

> Este plan define **cómo** se implementa la spec aprobada. No altera requisitos.
> Decisiones registradas como ADR en `docs/adr/`.

## Summary

Añadir el adapter real **`redgifs`** al SDK de la spec 002
(`services/crawler/xtrace_crawler/adapters/redgifs.py`): el **primer adapter con
`access_method="api"`** (jerarquía FR-004 nivel 1) — habla con `api.redgifs.com`
(JSON, token temporal `GET /v2/auth/temporary`), nunca con `www.redgifs.com` (SPA sin
SSR, `/watch/`+`/ifr/` disallowed por robots). `discover()` exige `section` con
prefijo `/niches/` (fail-fast sin ella, D2) y pagina por `page` (`GET
/v2/niches/<id>/gifs?order=new&count=100&page=N`, envelope `page`/`pages`/`total`,
anti-bucle). `get_video()` normaliza el objeto gif (`GET /v2/gifs/<id>`, wrapper
`{"gif": …}`) a `VideoSource`, con `page_url` `https://www.redgifs.com/watch/<id>`
como referencia **nunca fetcheada** (D5). `get_visual_assets()` devuelve **solo**
`thumbnail` + `poster` (ambos `kind="thumbnail"`, sin timestamp — no hay
storyboard, paridad FR-012); los mp4 (`urls.sd/hd/silent`) nunca se exponen
(SC-006 de la 002).

El adapter se desarrolla y prueba **sin red** con fixtures JSON sintéticos
anonimizados (dominios `.invalid`, SEC-004). El manifest queda **revisado en modo
prueba** (D4: `robots_reviewed=true`, `terms_reviewed=true`,
`review_date="2026-08-19"`), pero la fuente se registra en BD con `enabled=false`:
la habilitación efectiva (backfill real) sigue siendo una acción humana explícita
en BD. La validación real será un backfill acotado `--max-videos 50` sobre
`/niches/homemade` y `/niches/real-cellphone-clips` (D3).

**Sin cambios de esquema ni de contrato**: `sources`/`videos`/`jobs` ya cubren la
fuente. Único punto de composición: registro del adapter en el CLI (import
dinámico, paridad xvideos/xhamster/erome — SC-006/SC-007 de la 002).

## Technical Context

**Language/Version**: Python 3.11 (`uv` + `ruff` + `mypy` + `pytest` + `typer` +
`pydantic-settings`; `httpx` para JSON — sin parsing HTML, primer adapter sin
`selectolax`).

**Primary Dependencies**: `httpx` (cliente + `MockTransport` en tests),
`xtrace-spike` (editable, ADR-0011). Ninguna dependencia nueva: el token temporal
es una petición `GET` más sobre `SafeHTTPClient`.

**Storage**: Supabase local Postgres + pgvector (misma instancia del spike/002;
**sin migración nueva** — solo seed).

**Testing**: pytest (unit sin red con `httpx.MockTransport` + integración contra
Supabase local), mypy strict, ruff. E2E WebdriverIO no aplica (servicio CLI Python
sin UI).

**Target Platform**: servicio CLI local (`xtrace-crawler`), CPU, coste ~0 €.

**Project Type**: adapter de fuente dentro de un servicio CLI existente.

**Performance Goals**: ≤ 50 vídeos de validación por nicho; 1 GET de token +
1 GET de listado por página (`count=100`) + 1 GET de objeto gif por ítem
(reutilizable desde el listado si el envelope ya lo trae completo); rate limit
conservador por defecto (paridad con xhamster: 2000 ms / 0.5 rps, overridable).

**Constraints**: sin red en tests (NFR-003) · 0 descargas de mp4 (SC-004) · solo
hosts permitidos (SEC-001/003/006, `api.redgifs.com` + `media.redgifs.com`) ·
token nunca logueado/persistido (SEC-005) · determinismo en CI.

**Scale/Scope**: 1 fuente nueva · 4 PRs (paridad con la 007) · archivos nuevos:
adapter + fixtures + tests + seed + docs; ningún fichero del core se modifica
salvo los 2 puntos de registro documentados abajo.

## Constitution Check

| Puerta | Estado |
| --- | --- |
| Spec-first | ✅ spec 008 `APPROVED` (frase exacta humana, 2026-08-19) |
| Aprobación humana | ✅ hecha; puerta legal D4 OK en modo prueba; habilitación BD = acción humana pendiente; reanudación de la implementación instruida 2026-08-20 |
| Trazabilidad | ✅ FR/SEC/DATA/NFR/SC de 008 → PRs → tests → handoffs |
| Pull requests | ✅ rama `feature/008-redgifs-adapter`, PRs aislados, sin push a main |
| Testing | ✅ test-first en el adapter (fixtures JSON + `MockTransport`) |
| Seguridad/Supabase | ✅ sin migración; RLS existente intacto; allowlist de hosts nueva (`api.redgifs.com`, `media.redgifs.com`) |
| Calidad | ✅ ruff + mypy + pytest en cada PR |
| Dependencias | ✅ sin dependencias nuevas |
| Gobernanza | ✅ el core no se modifica; única excepción posible: enmienda explícita |

## Arquitectura / decisiones clave (ADR-0016)

1. **Método de acceso `api`** (FR-002, primer adapter de este nivel): sin parsing
   HTML; `SafeHTTPClient` con allowlist de host `{"api.redgifs.com"}` para
   metadata y `{"media.redgifs.com"}` para assets (FR-006).
2. **Token temporal** (SEC-005): `GET /v2/auth/temporary` al primer uso, cacheado
   en memoria del adapter (nunca en BD/logs); renovación automática ante `401`
   con el mismo backoff/jitter del rate limiter existente; sin reintento infinito
   (si la renovación falla persistentemente, error tipado contenido en la
   fuente — paridad edge case de la spec).
3. **`external_id`** = `id` del objeto gif normalizado a **lowercase** (FR-004;
   la API exige lowercase en `GET /v2/gifs/<id>`).
4. **`discover()` solo por sección `/niches/<id>`** (D2/FR-003): `section=None` o
   sin prefijo `/niches/` → `ValueError` tipado, fail-fast (paridad D2 de la 007).
   Paginación por **`page`** (1-based) contra
   `GET /v2/niches/<id>/gifs?order=new&count=100&page=N`; anti-bucle: página
   repetida, 0 IDs nuevos, o `page >= pages` → fin (paridad PR-043).
5. **`get_video()`**: si el listado ya trae el objeto gif completo, se normaliza
   directamente (sin segundo GET); si no, `GET /v2/gifs/<id>` (wrapper
   `{"gif": {...}}`, ignorando `user`/`niches` extra). `404` (`GifNotFound`) →
   `None` (paridad contrato: ítem no encontrado). `page_url` fijo
   `https://www.redgifs.com/watch/<external_id>` — **nunca fetcheado** (D5).
6. **`get_visual_assets()`** (FR-005): dos `VisualAsset(kind="thumbnail")` desde
   `urls.thumbnail` y `urls.poster` (si están presentes; alguno puede faltar en
   posts de imagen — degradación sin fallar), sin `position`/`timestamp_ms`
   (no hay storyboard). `urls.sd/hd/silent` **nunca** se leen ni exponen
   (SC-006/SC-004).
7. **`check_availability()`**: `GET /v2/gifs/<id>` → `200` = `AVAILABLE`; `404`
   (`GifNotFound`) = `REMOVED`; otro error de API = `UNAVAILABLE` (terminal, sin
   reintentos infinitos).
8. **Posts de imagen** (`type=2`, `duration=null`, `hasAudio=false`): se procesan
   como vídeo normal con `duration_ms=None` (edge case de la spec, sin rama
   especial en el modelo).
9. **Manifest** revisado en modo prueba (D4); seed con `enabled=false`; registry
   gate sin cambios.
10. **Composición**: registro dinámico en `cli._default_registry()` (import
    dinámico, mismo mecanismo anti-acoplamiento que xvideos/xhamster/erome — test
    AST extendido a redgifs).

## Project Structure

### Documentation (this feature)

```text
specs/008-redgifs-adapter/
├── spec.md               # APPROVED (ya existe)
├── plan.md               # este archivo
├── quickstart.md         # ejecución: fixtures → backfill acotado real
└── tasks.md              # siguiente fase (task-planning)
```

### Source Code (solo ficheros nuevos + 2 puntos de registro)

```text
services/crawler/
├── xtrace_crawler/adapters/redgifs.py         # NUEVO: cliente token + parsers JSON + RedgifsAdapter
├── xtrace_crawler/cli.py                      # EDITAR solo: _default_registry() (import dinámico)
├── tests/fixtures/redgifs/                    # NUEVO: JSON sintético anonimizado + README (SEC-004)
│   ├── README.md
│   ├── auth_temporary.json
│   ├── niche_gifs_page_1.json
│   ├── niche_gifs_page_2.json
│   ├── niche_gifs_empty.json
│   ├── gif_object.json
│   ├── gif_object_image_post.json
│   └── gif_not_found_404.json
├── tests/unit/test_redgifs_adapter.py         # NUEVO: parsers + adapter con MockTransport
└── tests/unit/test_registry.py                # EDITAR solo: caso redgifs (gate + registro)

supabase/
└── seed.sql                                   # EDITAR solo: fila fuente redgifs (manifest D4, enabled=false)
```

**Estructura heredada sin cambios**: `adapters/base.py`, `adapters/models.py`,
`adapters/registry.py`, `crawling/*`, `jobs/*`, `assets/*`, `pipeline.py`,
`repo.py`, migraciones, tests de BD. El core NO cambia (SC-006).

## Contratos (heredados de la spec 002, sin cambios)

- `SourceAdapter` + `AdapterManifest` + `VideoSource`/`VisualAsset`/
  `DiscoverPage`/`VideoAvailability` (incluidas las enmiendas `page_url` y
  `section` ya existentes).
- Gate SEC-002 del registry — sin cambios; `enabled_in_db` viene de
  `sources.enabled`.
- Sin uso de `storyboard_grid` (no hay storyboard en redgifs — a diferencia de
  xhamster/xvideos, este adapter no exporta hook de grid).

## Modelo de datos

**Sin cambios de esquema.** Solo datos:
- `sources`: nueva fila `redgifs` (manifest D4, `enabled=false`).
- `videos`: filas de redgifs con `source_id`→redgifs, `external_id` = id del gif
  (lowercase), `duration_ms` nullable (posts de imagen), `thumbnail_url` =
  `urls.thumbnail`, `tags`, `published_at` desde `createDate`.
- `frames`: `source_kind=thumbnail` (ya contemplado) — 1–2 frames por ítem
  (thumbnail + poster), **sin timestamp** (`timestamp_ms=None`, paridad FR-012).

## Estrategia de seguridad

- `SafeHTTPClient` con allowlist de host de API `{"api.redgifs.com"}` y de
  assets `{"media.redgifs.com"}` (FR-006/SEC-003), anti-DNS-rebinding heredado.
- Sin acceso a `www.redgifs.com`, `/watch/`, `/ifr/` en ningún caso (SEC-001).
- Token temporal: solo en memoria del proceso adapter, nunca en BD, logs ni
  fixtures/errores (SEC-005); los fixtures de test usan un valor de token
  claramente sintético (`"fixture-token-not-a-secret"`).
- Media real **nunca** en el repo (SEC-004); JSON real de prospección vive fuera
  del repo (`/tmp`).
- BD: credenciales service_role solo en el crawler (sin cambios); RLS existente
  intacto.

## Estrategia de tests

| Capa | Ficheros | Qué cubre |
| --- | --- | --- |
| Unit (parsers puros) | `test_redgifs_adapter.py` | envelope de listado (page/pages/total, anti-bucle, `count>100` rechazado), objeto gif (wrapper, campos opcionales, `duration` nullable→`duration_ms`), 404→`None`/`REMOVED` |
| Unit (adapter) | `test_redgifs_adapter.py` | `httpx.MockTransport` con fixtures: token temporal + renovación ante 401, discover sin `section`/con sección inválida → `ValueError`, `get_visual_assets` sin mp4, allowlist de hosts |
| Registry | `test_registry.py` | registro dinámico, gate SEC-002 con manifest D4 (revisado) pero `enabled=false` → `AdapterNotEnabledError`; AST: el core no importa estáticamente `redgifs.py` |
| Integración (Supabase local) | `test_pipeline.py` (+caso redgifs) | flujo completo con fixtures+MockTransport: discover→metadata→assets (thumbnail+poster, sin timestamp)→embeddings fake; INCREMENTAL sin duplicados |
| BD | sin cambios | esquema ya cubierto por `source_sdk_crawler_schema.test.sql` |
| E2E | no aplica | servicio CLI sin UI |

## Estrategia de despliegue / operación

- **No hay cambios en el shell Next.js ni en Vercel**: esta feature no altera la web.
- Entregable operativo: `specs/008-redgifs-adapter/quickstart.md` con:
  `uv sync --locked` → `uv run ruff check && uv run mypy . && uv run pytest` →
  `supabase db reset` (seed registra redgifs deshabilitado) → habilitación
  explícita en BD (SQL del operador) →
  `xtrace-crawler backfill --source redgifs --section /niches/homemade
  --max-videos 50` → repetir con `/niches/real-cellphone-clips` →
  `--incremental` → `xtrace-crawler stats`.
- Overrides de rate limit por entorno:
  `XTRACE_CRAWLER_RATE_REDGIFS_MIN_INTERVAL_MS` /
  `XTRACE_CRAWLER_RATE_REDGIFS_MAX_RPS` (defaults en código, conservadores).
- Embeddings de la validación real: `XTRACE_CRAWLER_EMBEDDINGS=fake` por defecto.

## Observabilidad

Reutilizada de la spec 002 sin cambios: jobs por estado/fuente, vídeos
descubiertos/indexados/fallidos, errores recientes y `rate_limits` por fuente
(`stats`). El manifest/seed de redgifs aparece en `sources`.

## Calidad por PR (gates obligatorios)

```bash
cd services/crawler
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pytest -q
```

## Riesgos y mitigaciones

- **Cambio de la API de redgifs** (JSON, endpoints, requisitos de auth) →
  adapter aislado + fixtures versionados que detectan la regresión.
- **Rate limits / 429 / Cloudflare** → backoff con jitter heredado; nunca se
  intenta saltar la protección.
- **Token temporal efímero** (caducidad ≈ 24 h, posible revocación) →
  renovación automática ante 401; nunca se persiste ni se loguea.
- **Riesgo legal** → gate SEC-002 intacto: manifest revisado (D4) pero
  `enabled=false` en BD hasta acción humana explícita.
- **Densidad visual baja** (1–2 frames sin timestamp por ítem) → limitación
  documentada (spec, Assumptions/Risks); medible con stats existentes.
- **Acoplamiento accidental al core** → test AST + revisión por agente distinto.

## Desglose de PRs propuesto (numeración continúa tras PR-065)

| PR | Contenido | Trazabilidad |
| --- | --- | --- |
| PR-066 | `adapters/redgifs.py` (cliente de token + parsers JSON + adapter + manifest D4 + `asset_hosts`), fixtures sintéticos `tests/fixtures/redgifs/`, tests unitarios (parsers + MockTransport) | FR-001…FR-006, FR-008, SEC-001/003/004/005, NFR-003, SC-001 |
| PR-067 | Registro en `cli._default_registry()` (import dinámico), test AST anti-acoplamiento, caso registry (gate SEC-002 con `enabled=false`), seed `supabase/seed.sql` (fila redgifs) | FR-007, SEC-002, DATA-001, SC-006 |
| PR-068 | Integración end-to-end con fixtures (pipeline + Supabase local: frames sin timestamp, embeddings fake, INCREMENTAL sin duplicados), `quickstart.md`, actualización README del servicio | FR-009, NFR-002, SC-001 (offline), SC-003, SC-007 |
| PR-069 | **Validación real del operador** (fuera de CI): habilitar en BD, backfill real `--section /niches/homemade --max-videos 50` y `--section /niches/real-cellphone-clips --max-videos 50`, incremental, stats, ajustes de estructura real si aparecen + handoff de validación | SC-002…SC-005, NFR-004 |

## Notas de planificación

- `tasks.md` se genera después con `task-planning` (tareas READY por PR,
  `allowed_paths` explícitos, un implementador a la vez, revisión por agente
  distinto).
- Cualquier hallazgo que exija tocar el core (contrato/modelo) **bloquea** el PR
  y se convierte en enmienda explícita de la spec 002 (constitución §1), nunca
  un cambio silencioso.
