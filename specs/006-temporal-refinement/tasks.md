# Tasks: Refinamiento temporal bajo demanda

**Input**: Design documents from `specs/006-temporal-refinement/`

**Prerequisites**: `spec.md` `APPROVED`, `plan.md`, `research.md`, `data-model.md`,
`contracts/`, `quickstart.md`.

**Tests**: Obligatorios. La lógica de negocio, la degradación, RLS, contrato REST y los
flujos críticos se prueban antes de cerrar la implementación correspondiente.

**Regla de edición**: cada tarea tiene `allowed_paths` explícitos en su contrato
`contracts/TASK-006-Txxx.md`. Dos tareas marcadas `[P]` no comparten archivos.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Preparar la dependencia del crawler, el esquema server-only y el esqueleto
del módulo sin tocar la política ni el índice base.

- [x] T001 [P] Añadir `xtrace-crawler` como dependencia editable de la API, actualizar `services/api/uv.lock` y adaptar `services/api/Dockerfile` para copiar el paquete crawler; comprobar instalación fake sin torch (FR-003, SEC-001; `allowed_paths`: `services/api/pyproject.toml`, `services/api/uv.lock`, `services/api/Dockerfile`)
- [x] T002 [P] Crear la migración de telemetría `search_refinements`/`search_refinement_evidence`, constraints, índices, cascade y RLS deny-by-default, más el test pgTAP negativo/positivo server-side (DATA-001..003, SEC-005; `allowed_paths`: `supabase/migrations/20260818000000_temporal_refinement.sql`, `supabase/tests/temporal_refinement_schema.test.sql`)
- [x] T003 [P] Crear el paquete `services/api/xtrace_api/refinement/` y sus modelos/puertos inyectables sin efectos de red ni escritura en `VectorStore` (FR-009, NFR-001; `allowed_paths`: `services/api/xtrace_api/refinement/__init__.py`, `services/api/xtrace_api/refinement/models.py`, `services/api/xtrace_api/refinement/ports.py`)

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Dejar configurables los límites, el contrato REST y las fronteras de
telemetría antes de implementar los tres escenarios.

**⚠️ CRITICAL**: Ninguna historia puede cerrarse hasta completar esta fase.

- [x] T004 [P] Escribir primero tests fallidos de `RefinementPolicy` para defaults 3/5/30, rangos, timeout global/candidato, flag de fuente y `policy_version` (FR-002, FR-004, FR-012, NFR-002; `allowed_paths`: `services/api/tests/unit/test_refinement_policy.py`)
- [x] T005 Implementar `RefinementPolicy`, settings `XTRACE_REFINEMENT_*` y overrides por fuente con validación fail-closed (T004; FR-002, FR-004, FR-012, NFR-002; `allowed_paths`: `services/api/xtrace_api/refinement/policy.py`, `services/api/xtrace_api/config.py`)
- [x] T006 [P] Escribir primero tests fallidos del contrato Pydantic/Zod para `RefinementSummary`, `TimestampProvenance` y compatibilidad de respuestas sin campos nuevos (DATA-001, UX-001, UX-003; `allowed_paths`: `services/api/tests/unit/test_schemas.py`, `tests/unit/api-contract.test.ts`)
- [x] T007 Implementar modelos REST Python y schema Zod compartido sin cambiar los campos existentes de búsqueda (T006; DATA-001, UX-001..003, SC-008; `allowed_paths`: `services/api/xtrace_api/schemas.py`, `src/lib/api/schemas.ts`)

**Checkpoint**: Dependencias instalables, migración verificada en esquema local, política
validada y contrato de respuesta listo. El índice base todavía no se escribe.

## Phase 3: User Story 1 — Timestamp más preciso (Priority: P1) 🎯 MVP

**Goal**: Refinar automáticamente los candidatos principales con assets permitidos y
devolver un timestamp respaldado sin cambiar el ranking base.

**Independent Test**: Un `MockAdapter` con thumbnails temporales produce una mejora
trazable en el candidato correcto; el resultado conserva `video_id`, score y orden del
primer pase.

### Tests for User Story 1 (write first)

- [x] T008 [P] [US1] Escribir tests fallidos del evaluador: embedding batch, similitud coseno, timestamp válido, guardia visual, dedupe y selección determinista (FR-005..007, FR-013, SC-001, SC-005; `allowed_paths`: `services/api/tests/unit/test_refinement_evaluator.py`)
- [x] T009 [P] [US1] Escribir tests fallidos del materializador con assets mock thumbnail/storyboard, dimensiones, cierre de imágenes y cero frames escritos (FR-003, FR-007, FR-009, FR-010, SEC-003; `allowed_paths`: `services/api/tests/unit/test_refinement_assets.py`)
- [x] T010 [P] [US1] Escribir tests fallidos del catálogo/bridge con vídeo web elegible, fuente habilitada y `XvideosAdapter` mockeado sin HTML real (FR-003, SEC-001, SEC-002; `allowed_paths`: `services/api/tests/unit/test_refinement_catalog.py`, `services/api/tests/unit/test_refinement_adapters.py`)

### Implementation for User Story 1

- [x] T011 [US1] Implementar modelos de outcome, evaluador y materializador en memoria; aceptar únicamente `thumbnail`/`storyboard` con timestamp respaldado y cerrar todos los recursos (T008, T009; FR-005..007, FR-009, FR-010, SEC-003; `allowed_paths`: `services/api/xtrace_api/refinement/models.py`, `services/api/xtrace_api/refinement/evaluator.py`, `services/api/xtrace_api/refinement/assets.py`)
- [x] T012 [US1] Implementar catálogo SQL y bridge de adapters reutilizando `AdapterRegistry`, `VideoSource`, `AssetFetcher` y allowlists, sin parsear HTML ni construir URLs en la API (T010, T011; FR-003, SEC-001, SEC-002, DATA-001; `allowed_paths`: `services/api/xtrace_api/refinement/catalog.py`, `services/api/xtrace_api/refinement/adapters.py`)
- [x] T013 [US1] Escribir primero la integración de `TemporalRefinementService` sobre una búsqueda in-memory/Postgres fake, verificando que solo se procesa el límite de candidatos y que el ranking/score base no cambia (FR-001, FR-002, FR-005, FR-009, NFR-001; `allowed_paths`: `services/api/tests/integration/test_temporal_refinement_search.py`, `services/api/tests/unit/test_search_service.py`)
- [x] T014 [US1] Orquestar el segundo pase en `POST /search`, generar `search_id` antes de refinar, mapear provenance/summary y conservar fallback si el primer pase no tiene candidatos (T005, T007, T011..T013; FR-001, FR-002, FR-005, FR-008, NFR-001, NFR-004, DATA-003; `allowed_paths`: `services/api/xtrace_api/refinement/service.py`, `services/api/xtrace_api/search_service.py`, `services/api/xtrace_api/routers/search.py`, `services/api/tests/integration/test_search.py`)

**Checkpoint MVP**: una búsqueda con adapter fake mejora el timestamp y otra búsqueda
local sin fuente sigue respondiendo con el primer pase; no hay escritura en `frames`.

## Phase 4: User Story 2 — Degradación segura y UX (Priority: P1)

**Goal**: Fallar cerrado ante fuentes sin assets, errores HTTP, corrupción, timeout o
restricciones, y explicar el fallback en la interfaz sin confundirlo con vídeo ausente.

**Independent Test**: 403/404/timeout/asset corrupto y `preview` rechazado conservan el
resultado base, limpian temporales y muestran estado comprensible.

### Tests for User Story 2 (write first)

- [x] T015 [P] [US2] Escribir tests fallidos de fallback por fuente/asset, timeout global/candidato, límite de bytes/píxeles, duplicados, timestamp ausente/fuera de rango, 403/404 y cancelación (FR-006..010, NFR-002, SEC-003, SEC-005; `allowed_paths`: `services/api/tests/unit/test_refinement_fallback.py`)
- [x] T016 [P] [US2] Escribir tests fallidos de seguridad que demuestren gate `sources.enabled`, manifest incompleto, host fuera de allowlist y rechazo de `preview`/vídeo (SEC-001..004, SEC-004, SC-006; `allowed_paths`: `services/api/tests/unit/test_refinement_security.py`, `services/crawler/tests/unit/test_refinement_asset_contract.py`)
- [x] T017 [P] [US2] Escribir tests Vitest/Testing Library para badge `refinado`, `índice base`, `disponibilidad limitada` y compatibilidad con payload legacy (UX-001..003, UX-002, SC-008; `allowed_paths`: `tests/unit/buscar-page.test.tsx`, `tests/unit/api-contract.test.ts`)

### Implementation for User Story 2

- [x] T018 [US2] Completar degradación tipada, timeout/cancelación, cleanup `try/finally`, rate-limit efectivo y motivos de descarte sin reintentos fuera de política (T015, T016; FR-006..010, NFR-002, SEC-001..005, DATA-002; `allowed_paths`: `services/api/xtrace_api/refinement/assets.py`, `services/api/xtrace_api/refinement/evaluator.py`, `services/api/xtrace_api/refinement/adapters.py`, `services/api/xtrace_api/refinement/service.py`)
- [x] T019 [US2] Implementar el render del estado de timestamp/provenance y el mensaje de fallback en la página `/buscar`, manteniendo enlaces y loading existentes (T017, T018; UX-001..003, UX-002; `allowed_paths`: `src/features/search/buscar-page.tsx`, `src/lib/api/xtrace.ts`)
- [x] T020 [US2] Añadir integración Postgres de fallback y prueba de que una búsqueda no cambia conteo/filas de `frames` ni deja temporales (T018; FR-008..010, SC-004, SC-006; `allowed_paths`: `services/api/tests/integration/test_temporal_refinement_fallback.py`)

**Checkpoint**: cualquier fuente no disponible degrada a un resultado honesto; no se
intenta eludir CAPTCHA/paywall/DRM/auth/anti-bot y el usuario ve la diferencia.

## Phase 5: User Story 3 — Refinamiento acotado y observable (Priority: P2)

**Goal**: Persistir métricas agregables, permitir apagar/limitar la política y validar
la adopción con benchmark pareado antes de cambiar defaults.

**Independent Test**: Repetir una búsqueda con el mismo policy/fixture produce métricas
equivalentes; un benchmark con cobertura insuficiente falla cerrado.

### Tests for User Story 3 (write first)

- [x] T021 [P] [US3] Escribir tests fallidos de `record_refinement`, idempotencia, contadores, estados, cascade TTL y ausencia de media/query en tablas (FR-011, FR-013, DATA-002..003, DATA-003, NFR-004, SEC-005; `allowed_paths`: `services/api/tests/unit/test_refinement_analytics.py`, `services/api/tests/integration/test_temporal_refinement_schema.py`)
- [x] T022 [P] [US3] Escribir tests fallidos del benchmark con 30 positivos pareados, segmentos de duración, Top-1/Top-5, error temporal, coste/latencia y fail-closed de cobertura (FR-014, NFR-003, SC-001..003, SC-002, SC-003, SC-007; `allowed_paths`: `tests/unit/temporal-refinement-benchmark.test.ts`, `services/api/tests/unit/test_temporal_refinement_benchmark.py`)

### Implementation for User Story 3

- [x] T023 [US3] Implementar persistencia de resumen/evidencia, bytes agregados, número/tiempo de embeddings, métricas seguras y limpieza por cascade/TTL, sin almacenar bytes de consulta (T002, T021; FR-011, FR-013, NFR-003, DATA-001..003, SEC-005, SC-007; `allowed_paths`: `services/api/xtrace_api/refinement/analytics.py`, `services/api/xtrace_api/analytics.py`, `services/api/xtrace_api/refinement/service.py`, `services/api/xtrace_api/routers/search.py`)
- [x] T024 [US3] Implementar límites por fuente/entorno y exposición de `policy_version`, incluyendo métricas de presupuesto agotado y estado `limited` (T005, T021, T023; FR-012, NFR-002..003, SC-004, SC-007; `allowed_paths`: `services/api/xtrace_api/refinement/policy.py`, `services/api/xtrace_api/config.py`)
- [x] T025 [US3] Implementar `scripts/benchmark_temporal_refinement.py` con manifest, verdad temporal independiente, comparación base/refinamiento, segmentación y salida JSON fuera de Git (T022; FR-014, NFR-003, SC-001..003, SC-002, SC-003, SC-007; `allowed_paths`: `scripts/benchmark_temporal_refinement.py`, `services/api/xtrace_api/refinement/benchmark.py`)

**Checkpoint**: la operación puede medir coste/calidad, apagar el segundo pase y
repetirlo de forma idempotente sin reindexar ni ampliar el catálogo base.

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T026 [P] Actualizar el stub HTTP y E2E WebdriverIO para respuestas refinadas y fallback, incluyendo captura/log de fallo; no usar Playwright/Cypress (UX-001..003, SC-004, SC-008; `allowed_paths`: `tests/e2e/stub-api.mjs`, `tests/e2e/specs/search.smoke.e2e.ts`, `tests/e2e/fixtures/`)
- [x] T027 [P] Actualizar README/quickstart/runbook y documentar configuración, límites, cumplimiento, benchmark y la decisión de no tocar el índice base (NFR-002, SEC-001..005, SC-006..007; `allowed_paths`: `specs/006-temporal-refinement/quickstart.md`, `docs/STATUS.md`, `docs/runbooks/`)
- [x] T028 Ejecutar formato, Ruff, mypy, pytest, tests Supabase/RLS, Vitest, E2E y build en el orden constitucional; registrar resultados y tamaños de temporales en `docs/handoffs/TASK-006-VALIDATION.md` (Definition of Done; `allowed_paths`: `docs/handoffs/TASK-006-VALIDATION.md`)
- [x] T029 Ejecutar revisión de seguridad y cumplimiento (allowlist, ausencia de vídeo completo/biometría/bypass, secretos y logs) y registrar hallazgos sin modificar código de implementación (T031; SEC-001..005; `allowed_paths`: `docs/handoffs/TASK-006-SECURITY.md`)
- [x] T030 Ejecutar `speckit-analyze`, `speckit-converge`, revisión independiente y `pr-finalization`; resolver comentarios, actualizar trazabilidad y dejar el PR listo sin aprobarlo el implementador (Constitution §4/§5/§8; `allowed_paths`: `specs/006-temporal-refinement/tasks.md`, `docs/handoffs/`, PR metadata)
- [x] T031 [P] Corregir el cliente HTTP del adapter XVIDEOS para activar validación anti-DNS-rebinding también en descubrimiento y lectura de assets, con regresión sobre el constructor seguro (SEC-001..003; `allowed_paths`: `services/crawler/xtrace_crawler/adapters/xvideos.py`, `services/crawler/tests/unit/test_xvideos_adapter.py`)

## Dependencies & Execution Order

```text
T001/T002/T003 → T004..T007 (Foundation)
Foundation → US1 (T008..T014) → US2 (T015..T020) → US3 (T021..T025)
US1/US2/US3 → Polish (T026..T031)
```

- T001, T002 y T003 son paralelizables: packaging, migración y esqueleto no comparten
  archivos.
- T004 y T006 son paralelizables; T005 depende de T004 y T007 de T006.
- T008, T009 y T010 son paralelizables y deben fallar antes de T011/T012.
- T015, T016 y T017 son paralelizables; T018 espera a los tres y T019/T020 después.
- T021 y T022 son paralelizables; T023/T024/T025 esperan sus tests y T023 comparte el
  contrato de migración completado en T002.
- T026 y T027 son paralelizables con la ejecución final de tests, pero T028 necesita
  todos los cambios integrados.

## Parallel Execution Examples

```text
Foundation: T001 (Luna) | T002 (Luna) | T003 (Luna)
US1 tests:   T008 evaluator | T009 assets | T010 catalog
US2 tests:   T015 fallback | T016 security | T017 frontend
US3 tests:   T021 analytics | T022 benchmark
Polish:      T026 E2E | T027 docs | T029 security review
```

Cada línea paralela tiene archivos distintos; cualquier integración que comparta
`service.py`, `config.py`, `schemas.py` o `buscar-page.tsx` espera a la tarea anterior.

## Implementation Strategy

1. Completar Setup/Foundational y validar la migración sin tocar el índice.
2. Entregar MVP US1 con adapter fake y un timestamp refinado trazable.
3. Cerrar US2 con fallback seguro y UX antes de habilitar tráfico real.
4. Añadir observabilidad y benchmark pareado; mantener el índice base y sus defaults.
5. Ejecutar todos los gates constitucionales y revisión independiente antes del PR.

## Task Contract Index

Los contratos detallan `allowed_paths`, precondiciones, tests, criterios de finalización,
riesgo y handoff para cada tarea: `contracts/TASK-006-T001.md` …
`contracts/TASK-006-T031.md`.

## Format Validation

Las 31 tareas usan checkbox, ID secuencial, `[P]` solo cuando procede, etiqueta `[USn]`
en fases de historias y rutas concretas en cada descripción.
