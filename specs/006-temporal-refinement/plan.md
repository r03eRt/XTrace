# Implementation Plan: Refinamiento temporal bajo demanda

**Branch**: `feature/006-temporal-refinement` | **Date**: 2026-08-18 | **Spec**:
[spec.md](spec.md)

**Input**: Feature specification from `specs/006-temporal-refinement/spec.md`.

## Summary

Añadir un segundo pase automático y acotado al `POST /search`: los tres candidatos
principales (cinco máximo) podrán evaluarse contra hasta treinta thumbnails/storyboards
públicos del adapter habilitado. El timestamp solo se sustituye por evidencia temporal
real y visualmente no peor; si la fuente falla, no expone assets o se agota el
presupuesto, se conserva el resultado del índice base. La respuesta incluye procedencia
y estado, mientras Supabase guarda únicamente métricas y referencias públicas
sanitizadas, nunca imágenes ni vídeo.

## Technical Context

**Language/Version**: Python 3.11, TypeScript estricto.
**Primary Dependencies**: FastAPI, Pydantic Settings, `xtrace-spike` editable,
`xtrace-crawler` editable para adapters/allowlists/asset fetcher, Pillow, psycopg;
Next.js App Router, Zod, Vitest, Testing Library y WebdriverIO.
**Storage**: Supabase PostgreSQL + pgvector; nuevas tablas server-only para telemetría.
**Testing**: pytest, Ruff, mypy, Vitest, tests de esquema/RLS y WebdriverIO en Chrome
headless.
**Target Platform**: API FastAPI en Docker/local/Preview server-side y frontend Next.js
en Vercel; fuentes web solo mediante adapters aprobados.
**Project Type**: aplicación web con API Python, índice vectorial y crawler compartido.
**Performance Goals**: primer pase intacto; segundo pase ≤10 s por búsqueda y ≤3 s por
candidato, con límites de assets/bytes y cancelación controlada.
**Constraints**: no vídeo completo, no bypass de controles, no biometría, no caché
persistentemente de imágenes, no escritura en `frames` durante una búsqueda.
**Scale/Scope**: candidatos de un catálogo multi-proveedor potencialmente de millones de
vídeos; esta iteración habilita inicialmente adapters ya revisados (XVIDEOS).

## Constitution Check

_Gate evaluado antes de Phase 0 y nuevamente después del diseño de Phase 1._

| Principio | Estado | Evidencia |
| --- | --- | --- |
| Spec-first | PASS | Spec 006 marcada `APPROVED` tras `Especificación aprobada` del 2026-08-18. |
| Trazabilidad | PASS | Este plan, contratos, ADR, tareas y tests mapearán FR/NFR/SEC/DATA/UX. |
| Rama/PR independiente | PASS | Rama `feature/006-temporal-refinement`; sin push directo a `main`. |
| Test-first | PASS | Política, evaluator, fallback, migración y UI tendrán pruebas antes de cerrar tareas. |
| E2E crítico | PASS | WDIO cubrirá la distinción visual entre base/refinado y fallback. |
| Seguridad y Supabase | PASS | AdapterRegistry/allowlist; migración RLS deny-by-default y tests positivos/negativos. |
| Calidad | PASS | Formato, Ruff, mypy, pytest, DB, WDIO y build en el orden constitucional. |
| Dependencias | PASS | Se reutilizan contratos/HTTP/Pillow existentes; `xtrace-crawler` se añade como editable porque contiene la única frontera legal de assets. |

## Phase 0 — Research

Decisiones y alternativas: [research.md](research.md). ADR resultante:
[0014-on-demand-temporal-refinement.md](../../docs/adr/0014-on-demand-temporal-refinement.md).

No quedan preguntas funcionales abiertas. El benchmark denso/adaptativo de Spec 005 no
se reutiliza como prueba de adopción: la feature requiere verdad temporal independiente
y comparación pareada de primer/refinamiento (FR-014/SC-001..SC-003).

## Phase 1 — Design

- Modelo y retención: [data-model.md](data-model.md).
- Contratos REST/puerto/telemetría: [contracts/README.md](contracts/README.md).
- Validación reproducible: [quickstart.md](quickstart.md).
- Diagrama y fronteras: [temporal-refinement.md](../../docs/architecture/temporal-refinement.md).

### Arquitectura y componentes

1. **Política (`services/api/xtrace_api/refinement/policy.py`)**: settings tipados,
   defaults 3/5/30/10 s/3 s, overrides por entorno/fuente y `policy_version`. No toca
   la política de muestreo del índice.
2. **Catálogo (`.../catalog.py`)**: consulta server-side de metadatos web y timestamps
   ya indexados; construye `VideoSource` sin parsear HTML. `source_id=null`, excluidos,
   fuentes no habilitadas o adapter desconocido devuelven fallback.
3. **Registry/bridge (`.../adapters.py`)**: registro de adapters aprobados (inicialmente
   `XvideosAdapter`), aplica `AdapterRegistry.get_enabled` y cierra clientes al final de
   la invocación. La DB solo aporta `enabled`; el manifest de compliance procede del
   código revisado.
4. **Materializador (`.../assets.py`)**: usa el hook opcional in-process o
   `AssetFetcher` + `SafeHTTPClient` con allowlist; admite `thumbnail`/`storyboard`,
   rechaza `preview`, limita bytes/píxeles, deduplica URL/posición y cierra temporales.
5. **Evaluador (`.../evaluator.py`)**: ejecuta timeout global/candidato, embebe en
   memoria, calcula similitud coseno, valida timestamp y aplica la guardia de evidencia.
   Devuelve un `RefinementOutcome` ordenado igual que el ranking base; no conoce el
   `VectorStore` para escribir.
6. **Servicio/analytics (`.../service.py`, `analytics.py`)**: orquesta candidatos,
   mapea estados/provenance, inserta `search_refinements`/evidence después de `searches`
   y expone contadores seguros en logs.
7. **API (`search_service.py`, `routers/search.py`, `schemas.py`)**: genera
   `search_id` antes del segundo pase, conserva campos actuales, añade summary y
   provenance, y trata cualquier fallo de refinamiento como fallback controlado (un
   fallo del primer pase sigue siendo 503/500 según el contrato existente).
8. **Frontend (`src/lib/api/schemas.ts`, `src/features/search/buscar-page.tsx`)**:
   valida campos nuevos y muestra badges/mensajes `refinado`, `índice base` o
   `disponibilidad limitada` sin prometer exactitud. El cliente mantiene su timeout y
   cancelación.
9. **Benchmark (`scripts/benchmark_temporal_refinement.py`)**: consume un manifest de
   consultas pareadas, ejecuta ambos pases con el mismo provider y escribe el informe
   fuera de Git, segmentado por fuente y duración.

### Contrato de evaluación

- El número de candidatos se corta antes de cualquier red.
- `get_visual_assets` se limita a los primeros assets únicos permitidos hasta el
  presupuesto efectivo; no se generan URLs nuevas en el core.
- `timestamp_ms` ausente, negativo, fuera de duración, duplicado o sin origen fiable se
  descarta.
- Un asset solo puede reemplazar al timestamp base si su similitud es al menos la del
  frame base del candidato y el asset aporta una posición distinta; empates mantienen
  el resultado base para garantizar idempotencia.
- Si no hay mejora, se mantiene la lista/ranking base y se diferencian `unchanged`,
  `unavailable`, `limited` y `disabled`.

### API y datos

Se conserva `SearchResultItem.match_timestamp_ms` y se añaden los modelos Pydantic
`TimestampProvenance` y `RefinementSummary`. La migración crea las tablas descritas en
`data-model.md`, checks de estado/contadores, índices de consulta y RLS sin grants. La
limpieza de `searches` existente aprovecha `ON DELETE CASCADE`; no se introduce una
segunda retención de media.

### Seguridad y compliance

- El adapter registry comprueba manifest, review date y `sources.enabled` antes de
  obtener la primera URL.
- `SafeHTTPClient` valida esquema/host/IP; la URL pública se sanitiza antes de
  responder/persistir. No se registran query strings ni credenciales.
- `preview` queda fuera de la lista de tipos aceptados; no hay FFmpeg ni descarga de
  vídeo en el segundo pase.
- límites de bytes/píxeles, timeout, cero reintentos por defecto y cleanup en `finally`.
- `service_role`/DSN solo en servidor; el frontend nunca habla con Supabase ni recibe
  secretos.

### Estrategia de tests

- **Unitarios Python**: política (rangos, overrides), dedupe/validación, similitud y
  guardia visual, estados, idempotencia, límites y cleanup con `MockAdapter`.
- **API/integración Python**: `/search` con in-memory y adapter fake, fallback de 403/404/
  timeout/corrupción, respuesta/provenance, no escritura en `frames`; Postgres verifica
  métricas, TTL/cascade y ausencia de lectura RLS para anon/authenticated.
- **Crawler**: contrato del bridge con `XvideosAdapter` existente, allowlist y rechazo
  de `preview`; no se modifica el comportamiento legacy de indexación.
- **Frontend/Vitest**: Zod acepta estados nuevos, renderiza badge/mensaje y mantiene
  respuesta legacy mínima si los campos nuevos faltan.
- **E2E WebdriverIO**: stub HTTP real devuelve un resultado refinado y otro en fallback;
  Chrome headless comprueba timestamp, procedencia visible, enlace y que el loading se
  resuelve dentro del presupuesto. No Playwright/Cypress.
- **Benchmark**: 30 positivos pareados con verdad independiente, local + web permitida,
  tramos válidos `<5m`, `5-15m`, `>15m`; informe falla cerrado si falta cobertura.

### Despliegue y Preview

- `services/api/pyproject.toml` y `uv.lock` añaden `xtrace-crawler`; el Dockerfile de la
  API copia el paquete crawler al contexto de build. No se añade torch al grupo CI; el
  provider fake cubre tests.
- La migración se aplica en Supabase local/CI/Preview con la comprobación de esquema
  existente. Variables `XTRACE_REFINEMENT_*` son server-only salvo el flag de UI (que no
  se expone al cliente).
- Vercel Preview ejecuta el frontend contra un API/stub de contrato sin credenciales de
  producción; las pruebas WDIO no contactan fuentes reales. El PR debe tener Preview
  revisada antes de fusionarse.
- Producción/operación puede desactivar la feature con `XTRACE_REFINEMENT_ENABLED=false`
  sin cambiar el índice ni reindexar.

### Observabilidad

Cada búsqueda registra `search_id`, estado, candidates/assets evaluados, descartes,
errores, latencia, bytes agregados, número/tiempo de embeddings, mejora y
`policy_version`. Las métricas SQL permiten agrupar por fuente, duración, rango de
candidato y política; no incluyen la media ni el contenido de bytes. Logs estructurados
no contienen URL completa ni excepción remota. El benchmark genera una tabla de
comparación base/refinamiento con Top-1/Top-5, error temporal y coste.

## Traceability matrix

| Requisitos | Cobertura en diseño |
| --- | --- |
| FR-001..008 | Servicio/evaluator, fallback y `TimestampProvenance`; tests unit/API. |
| FR-009..010 | Sin `VectorStore` write, temporales `finally`, migración/TTL. |
| FR-011..014 | Summary/SQL metrics, settings, idempotencia y benchmark pareado. |
| NFR-001..004 | Feature flag, timeouts, métricas y provenance sin media. |
| SEC-001..005 | Registry/allowlist, tipos sin preview, no bypass/biometría, cleanup/RLS. |
| DATA-001..003 | Provenance REST + evidence SQL + dimensiones de agregación. |
| UX-001..003 | Schema Zod, badges/mensajes y E2E de base/refinado/fallback. |
| SC-001..008 | Script benchmark, tests de fallback/trazabilidad, DB/RLS y flag off. |

## Project Structure

### Documentation (this feature)

```text
specs/006-temporal-refinement/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/README.md
```

### Source Code (repository root)

```text
services/api/
├── xtrace_api/refinement/
│   ├── policy.py
│   ├── ports.py
│   ├── catalog.py
│   ├── adapters.py
│   ├── assets.py
│   ├── evaluator.py
│   ├── service.py
│   ├── analytics.py
│   ├── benchmark.py
│   └── models.py
├── xtrace_api/{analytics.py,config.py,schemas.py,search_service.py}
└── tests/{unit,integration}/

services/crawler/
└── tests/{unit,integration}/   # solo contratos/bridge, sin duplicar parser

supabase/
├── migrations/20260818000000_temporal_refinement.sql
└── tests/temporal_refinement_schema.test.sql

src/
├── lib/api/{schemas.ts,xtrace.ts}
└── features/search/buscar-page.tsx

tests/e2e/
├── stub-api.mjs
└── specs/search.smoke.e2e.ts

scripts/benchmark_temporal_refinement.py
```

**Structure Decision**: la frontera legal de fuentes permanece en crawler; la
orquestación de la petición vive en API; la telemetría es una migración server-only; el
frontend solo consume y visualiza el contrato. No se crea un servicio nuevo ni se
modifica el índice base.

## Phase 2 handoff

Este plan no crea `tasks.md`. La siguiente fase debe usar `task-planning`/`speckit-tasks`
para generar tareas `READY` pequeñas, con `allowed_paths`, dependencias y tests; cada
tarea de implementación deberá cargar `task-execution`. Después se ejecutará
`speckit-analyze`, convergencia, revisión independiente y `pr-finalization`.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
| --- | --- | --- |
| Añadir `xtrace-crawler` como dependencia de API | Reutiliza la única frontera de adapters, compliance y allowlists sin duplicar parser ni red. | Copiar/reescribir el parser en API sería más código, más riesgo legal y violaría ADR-0009. |
| Dos tablas server-only de telemetría | DATA-001/002/003 requieren métricas y trazabilidad por asset sin modificar `frames`. | Meter JSON en `searches` perdería constraints, agregaciones, cascade y tests RLS. |
