# Implementation Plan: Adapter xhamster.com (segunda fuente real)

**Branch**: `feature/007-xhamster-adapter` | **Date**: 2026-08-19 | **Spec**: [`spec.md`](./spec.md)

**Input**: Feature specification from `specs/007-xhamster-adapter/spec.md` (`APPROVED`,
2026-08-19, frase exacta "Especificación aprobada").

> Este plan define **cómo** se implementa la spec aprobada. No altera requisitos.
> Decisiones registradas como ADRs en `docs/adr/`.

## Summary

Añadir el adapter real **`xhamster`** al SDK de la spec 002 (`services/crawler/`,
paquete `xtrace_crawler`): un `SourceAdapter` (HTML) que parsea el listado de
categoría (`/categories/amateur`, ítems `div.video-thumb[data-video-id]` con enlaces
`/videos/<slug>-<id>` y paginación numérica) y la página de vídeo (`og:*` +
`window.initials.videoModel`), y que expone como visual assets el **sprite/storyboard
webp** (`data-sprite`, CDN `thumb-*.xhcdn.com`) y el **thumbnail** (`og:image`). Los
previews mp4 observados no se exponen en v1 (Decisión D3).

El adapter se desarrolla y prueba **sin red** con fixtures sintéticos anonimizados
(SEC-004, mismo patrón que xvideos PR-031+). El manifest queda **revisado en modo
prueba** (Decisión D5: `robots_reviewed=true`, `terms_reviewed=true`,
`review_date="2026-08-19"`), pero la fuente se registra en BD con `enabled=false`:
la habilitación efectiva (backfill real) sigue siendo una acción humana explícita.
La validación real será un backfill acotado `--section /categories/amateur
--max-videos 50` (Decisiones D2/D4).

**Sin cambios de esquema ni de contrato**: `sources`/`videos`/`jobs` ya cubren la
fuente; el único punto de composición que se toca es el **registro** del adapter en el
CLI (paridad con xvideos: import dinámico, ningún módulo del core importa el adapter
estáticamente — SC-006/SC-007).

## Technical Context

**Language/Version**: Python 3.11 (mismo toolchain que 002: `uv` + `ruff` + `mypy` +
`pytest` + `typer` + `pydantic-settings`; `httpx` + `selectolax` para HTML).

**Primary Dependencies**: `httpx`, `selectolax`, `pillow` (WebP ya soportado por
Pillow — el sprite `*.s.webp` se abre con `open_image_limited` del pipeline),
`xtrace-spike` (editable, ADR-0011).

**Storage**: Supabase local Postgres + pgvector (misma instancia del spike/002; **sin
migración nueva** — solo seed).

**Testing**: pytest (unit sin red + integración contra Supabase local), mypy strict,
ruff. Los E2E WebdriverIO **no aplican** (servicio Python sin UI; el shell Next.js no
se toca).

**Target Platform**: servicio CLI local (`xtrace-crawler`), CPU, coste ~0 €.

**Project Type**: adapter de fuente dentro de un servicio CLI existente.

**Performance Goals**: ≤ 50 vídeos de validación; 1 GET de sprite por vídeo (grid
resolver, ver ADR-0015); rate limit conservador 1 req/2 s sostenido.

**Constraints**: sin red en tests (NFR-003) · 0 descargas de vídeo completo (SC-004) ·
solo hosts permitidos (SEC-001/003) · determinismo en CI.

**Scale/Scope**: 1 fuente nueva · ~3-4 PRs · archivos nuevos: adapter + fixtures +
tests + seed + docs.

## Constitution Check

| Puerta | Estado |
| --- | --- |
| Spec-first | ✅ spec 007 `APPROVED` (frase exacta humana, 2026-08-19) |
| Aprobación humana | ✅ hecha; puerta legal D5 OK en modo prueba; habilitación BD = humana |
| Trazabilidad | ✅ FR/SEC/DATA/NFR/SC de 007 → PRs → tests → handoffs |
| Pull requests | ✅ rama `feature/007-xhamster-adapter`, PRs aislados, sin push a main |
| Testing | ✅ test-first en parsers (fixtures); regresiones de estructura |
| Seguridad/Supabase | ✅ sin migración; RLS existente intacto; allowlist de hosts |
| Calidad | ✅ ruff + mypy + pytest en cada PR (ver sección de calidad) |
| Dependencias | ✅ sin dependencias nuevas (Pillow ya soporta WebP) |
| Gobernanza | ✅ el core no se modifica; única excepción posible: enmienda explícita |

## Arquitectura / decisiones clave (ADR-0015)

1. **Método de acceso `html`** (FR-004): sin API/feed oficial ni sitemap (404) →
   parsing con `selectolax` sobre `SafeHTTPClient` (allowlist `xhamster.com` +
   `www.xhamster.com`).
2. **`external_id` = sufijo de la URL canónica** `/videos/<slug>-<id>` (formas
   numérica y alfanumérica), derivable de `og:url`/href del listado; el
   `data-video-id` interno no se usa como id (no está en la URL canónica).
3. **Sprite storyboard**: el sprite del vídeo principal sale del **player config**
   (`window.initials.spriteLoader.template` — path `/NNN/NNN/NNN/` coincidente con
   `og:image`); los `data-sprite` de la página de vídeo son de vídeos relacionados y
   NO se usan. El adapter emite **UN** `VisualAsset(kind="storyboard")` por vídeo
   (sin `position`/`timestamp_ms`, URL desde `video.storyboard_urls[0]`) y exporta
   `storyboard_grid(asset)` que el CLI conecta al pipeline (hook `storyboard_grid` de
   PR-029, ya existente): 1 descarga de sprite → N tiles con timestamp aproximado
   `round(pos/N*duration_ms)`. Formato: tira de una fila `…/<W>x<H>.<N>.s.<ext>` —
   observado `160x160.50.s.jpg` → 8000×131 → 50 tiles de 160×131 (spriteCount=50) y
   `526x298.s.webp` → 5260×298 → 20 tiles de 263×298. Grid: `(N, 1)` si la URL lleva
   `.<N>.s.`; `(20, 1)` para `.s.webp` sin N; `None` en otro caso. Re-validado con
   fixtures y capturas reales (paridad PR-053 de xvideos).
4. **Degradación**: sin sprite → thumbnail único (`og:image`); sin duración →
   timestamps `None` (paridad FR-012 del spike).
5. **Discover solo por sección** (D2): `section` obligatorio para xhamster;
   `None` → `ValueError` tipado. Paginación por cursor `/categories/amateur/N` con
   anti-bucle (cursor repetido / 0 IDs nuevos → fin; patrón PR-043).
6. **Manifest** revisado en modo prueba (D5); seed con `enabled=false`; registry
   gate sin cambios.
7. **Composición**: registro dinámico en `cli._default_registry()` + wire del grid
   resolver con import dinámico — mismo mecanismo anti-acoplamiento que xvideos
   (test AST `test_core_no_importa_el_adapter_xvideos` extendido a xhamster).

## Project Structure

### Documentation (this feature)

```text
specs/007-xhamster-adapter/
├── spec.md              # APPROVED (ya existe)
├── plan.md              # este archivo
├── quickstart.md        # ejecución: fixtures → backfill acotado real
└── tasks.md             # siguiente fase ($speckit-tasks / task-planning)
```

### Source Code (solo ficheros nuevos + 3 puntos de registro)

```text
services/crawler/
├── xtrace_crawler/adapters/xhamster.py        # NUEVO: parsers puros + XhamsterAdapter + storyboard_grid
├── xtrace_crawler/cli.py                      # EDITAR solo: _default_registry() + wire storyboard_grid (import dinámico)
├── tests/fixtures/xhamster/                   # NUEVO: HTML sintético anonimizado + README (SEC-004)
│   ├── README.md
│   ├── category_page_1.html
│   ├── category_page_2.html
│   ├── video_page_full.html
│   ├── video_page_minimal.html
│   └── video_page_sin_sprite.html
├── tests/unit/test_xhamster_adapter.py        # NUEVO: parsers + adapter con MockTransport
└── tests/unit/test_registry.py                # EDITAR solo: caso xhamster (gate + registro)

supabase/
└── seed.sql                                   # EDITAR solo: fila fuente xhamster (manifest D5, enabled=false)
```

**Estructura heredada sin cambios**: `adapters/base.py`, `adapters/models.py`,
`adapters/registry.py`, `crawling/*`, `jobs/*`, `assets/*`, `pipeline.py`, `repo.py`,
migraciones, tests de BD. El core NO cambia (SC-006).

## Contratos (heredados de la spec 002, sin cambios)

- `SourceAdapter` + `AdapterManifest` + `VideoSource`/`VisualAsset`/
  `DiscoverPage`/`VideoAvailability` — tal cual `contracts/` de 002 (incluidas las
  enmiendas `page_url` PR-045 y `section` PR-049).
- `storyboard_grid` (hook del pipeline, PR-029) — ya en el core; el adapter solo
  provee la función y el CLI la conecta.
- Gate SEC-002 del registry — sin cambios; `enabled_in_db` sigue viniendo de
  `sources.enabled`.

## Modelo de datos

**Sin cambios de esquema.** Solo datos:
- `sources`: nueva fila `xhamster` (manifest D5, `enabled=false`).
- `videos`: filas web con `source_id`→xhamster, `external_id` = sufijo de la URL
  canónica, `storyboard_urls` = [sprite], `thumbnail_url` = og:image, resto de campos
  desde `videoModel`.
- `frames`: `source_kind=storyboard`/`thumbnail` (ya contemplado); timestamps del
  sprite con clamp `[0, duration_ms)` (paridad PR-053).

## Estrategia de seguridad

- `SafeHTTPClient` con allowlist de página `{"xhamster.com", "www.xhamster.com",
  "es.xhamster.com"}` + anti-DNS-rebinding (heredado). `es.*` se acepta como
  objetivo de redirect/URL canónica (corrección A1: con IP española `og:url` puede
  servirse en `es.*`), no como base; cualquier otro redirect aborta (fail-closed).
- `asset_hosts = ["thumb-v0..9.xhcdn.com", "ic-vt-nss.xhcdn.com"]` — **PROVISIONAL**
  (el sprite del player vive en `thumb-*.xhcdn.com`, el thumbnail `og:image` en
  `ic-vt-nss.xhcdn.com`),
  confirmada en la validación real (fail-closed: host fuera de lista → degradación).
- Sin acceso a rutas disallowed por robots (`/premium/`, filtros best/…); el adapter
  solo construye URLs de sección/paginación/vídeo.
- Media real **nunca** en el repo (SEC-004); capturas de validación en `/tmp`.
- BD: credenciales service_role solo en el crawler (sin cambios); RLS existente intacto.

## Estrategia de tests

| Capa | Ficheros | Qué cubre |
| --- | --- | --- |
| Unit (parsers puros) | `test_xhamster_adapter.py` | listado: IDs dedup, page_urls, paginación/cursor, anti-bucle, truncación; página de vídeo: og:* + videoModel, IDs numérico/alfanumérico, campos opcionales; sprite: assets storyboard+thumbnail, degradación; grid resolver |
| Unit (adapter) | `test_xhamster_adapter.py` | `httpx.MockTransport` con fixtures: discover (section obligatoria, fail-fast sin section), get_video 404→None, check_availability, allowlist de hosts |
| Registry | `test_registry.py` | registro dinámico, gate SEC-002 con manifest D5 (revisado) pero `enabled=false` → `AdapterNotEnabledError`; AST: el core no importa estáticamente `xhamster.py` |
| Integración (Supabase local) | `test_pipeline.py` (+caso xhamster) | flujo completo con fixtures+MockTransport: discover→metadata→assets→frames (tiles del sprite)→embeddings fake; INCREMENTAL sin duplicados |
| BD | sin cambios | esquema ya cubierto por `source_sdk_crawler_schema.test.sql` |
| E2E | no aplica | servicio CLI sin UI; el shell Next.js no se toca |

Regresiones de estructura: si el HTML real cambia, los tests de fixtures fallan con
mensaje claro (paridad PR-043…PR-053).

## Estrategia de despliegue / operación

- **No hay cambios en el shell Next.js ni en Vercel**: esta feature no altera la web.
- Entregable operativo: `specs/007-xhamster-adapter/quickstart.md` con:
  `uv sync --locked` → `uv run ruff check && uv run mypy . && uv run pytest` →
  `supabase db reset` (seed registra xhamster deshabilitado) → habilitación explícita
  en BD (SQL del operador) → `xtrace-crawler backfill --source xhamster
  --section /categories/amateur --limit 64 --max-videos 50` → `--incremental` →
  `xtrace-crawler stats`.
- Overrides de rate limit por entorno: `XTRACE_CRAWLER_RATE_XHAMSTER_MIN_INTERVAL_MS`
  / `XTRACE_CRAWLER_RATE_XHAMSTER_MAX_RPS` (defaults en código: 2000 ms / 0.5 rps).
- Embeddings de la validación real: `XTRACE_CRAWLER_EMBEDDINGS=fake` por defecto;
  `siglip` solo si el operador lo pide (extra opcional, paridad PR-051).

## Observabilidad

Reutilizada de la spec 002 sin cambios: jobs por estado/fuente, vídeos
descubiertos/indexados/fallidos, errores recientes y `rate_limits` por fuente
(`stats`). El manifest/seed de xhamster aparece en `sources`.

## Calidad por PR (gates obligatorios)

```bash
cd services/crawler
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pytest -q
```

(opcional con Supabase local: `uv run pytest tests/integration -q` y
`supabase test db`). El `pnpm verify` global no aplica al servicio Python salvo que se
toque el shell Next.js (no es el caso).

## Riesgos y mitigaciones

- **Grid del sprite distinto en la práctica** → `storyboard_grid` con constantes
  revisables + fixtures; si un sprite real no es divisible por el grid, el pipeline
  degrada (StoryboardError contenido) y la validación real del operador lo corrige
  (paridad PR-053).
- **Paginación con saltos** (`/16828`, `/33654`) → el cursor avanza por href
  siguiente y el anti-bucle (0 IDs nuevos / cursor repetido) termina la cadena; la
  cota `--max-videos 50` garantiza acotación aunque la numeración salte.
- **HTML cambia** → fixtures versionados + errores tipados (`XhamsterParseError`).
- **Bloqueo/429** → backoff con jitter heredado; nunca se intenta saltar la
  protección.
- **Riesgo legal** → gate SEC-002 intacto: manifest revisado (D5) pero
  `enabled=false` en BD hasta acción humana.
- **Acoplamiento accidental al core** → test AST + revisión por agente distinto.

## Desglose de PRs propuesto (numeración continúa tras PR-061)

| PR | Contenido | Trazabilidad |
| --- | --- | --- |
| PR-062 | `adapters/xhamster.py` (parsers + adapter + manifest D5 + `storyboard_grid` + `asset_hosts`), fixtures sintéticos `tests/fixtures/xhamster/`, tests unitarios (parsers + MockTransport) | FR-001…FR-008, SEC-001/003/004, NFR-003, SC-001 |
| PR-063 | Registro en `cli._default_registry()` + wire de `storyboard_grid` (import dinámico), test AST anti-acoplamiento, caso registry (gate SEC-002 con `enabled=false`), seed `supabase/seed.sql` (fila xhamster) | FR-007, SEC-002, DATA-001, SC-006 |
| PR-064 | Integración end-to-end con fixtures (pipeline + Supabase local: frames del sprite con timestamp, embeddings fake, INCREMENTAL sin duplicados), `quickstart.md`, actualización README del servicio | FR-009, NFR-002, SC-002 (parcial offline), SC-003, SC-007 |
| PR-065 | **Validación real del operador** (fuera de CI): habilitar en BD, backfill real `--section /categories/amateur --max-videos 50`, incremental, stats, ajustes de estructura real si aparecen (paridad PR-042…PR-053) + handoff de validación | SC-002…SC-005, NFR-004 |

## Notas de planificación

- tasks.md se genera después con `task-planning` (tareas READY por PR, allowed_paths
  explícitos, un implementador a la vez, revisión por agente distinto).
- Cualquier hallazgo que exija tocar el core (contrato/modelo) **bloquea** el PR y se
  convierte en enmienda explícita de la spec 002 (constitución §1), nunca un cambio
  silencioso.
