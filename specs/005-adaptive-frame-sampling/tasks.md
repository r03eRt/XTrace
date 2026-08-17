# Tasks: Muestreo adaptativo de frames

**Input**: Design documents from `specs/005-adaptive-frame-sampling/`

**Prerequisites**: `spec.md` APPROVED, `plan.md`, `research.md`, `data-model.md`,
`contracts/sampling.md`, `quickstart.md`

**Tests**: Obligatorios y escritos antes de la implementación correspondiente.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Puede ejecutarse en paralelo sin editar los mismos archivos.
- **[Story]**: Historia de usuario trazada desde la spec.

## Phase 1: Setup

**Purpose**: Dejar contratos ejecutables y fronteras de edición sin solapamientos.

- [x] T001 Crear contratos READY para TASK-005-001..004 en `specs/005-adaptive-frame-sampling/contracts/TASK-005-001.md`, `TASK-005-002.md`, `TASK-005-003.md` y `TASK-005-004.md`
- [x] T002 Confirmar el baseline de tests de `services/search-spike` y `services/crawler` y registrar resultados en `docs/handoffs/TASK-005-BASELINE.md`

---

## Phase 2: Foundational

**Purpose**: Política pura y reemplazo coherente compartidos por todas las historias.

**Critical**: Bloquea la integración local y web.

- [x] T003 [P] Añadir primero tests fallidos de conteo, puntos centrados, monotonicidad, máximo 8, assets escasos y timestamps desconocidos en `services/search-spike/tests/unit/test_sampling.py`
- [x] T004 Implementar `AdaptiveSamplingPolicy` y `select_representative_frames` en `services/search-spike/xtrace_spike/sampling.py` hasta pasar T003
- [x] T005 [P] Añadir primero tests fallidos de reemplazo exacto, estado/conteo y rollback tras cada punto de fallo en `services/search-spike/tests/unit/test_vectorstore_inmemory.py` y `services/search-spike/tests/integration/test_pgvector_store.py`
- [x] T006 Implementar `VideoIndexWriter.replace_video_index` para frames+estado+conteo en `services/search-spike/xtrace_spike/indexing/writer.py`, `services/search-spike/xtrace_spike/vectorstore/in_memory.py` y `services/search-spike/xtrace_spike/vectorstore/pgvector.py`

**Checkpoint**: Política y persistencia compartidas, sin cambiar todavía defaults.

---

## Phase 3: User Story 1 — Cobertura temporal proporcional (P1)

**Goal**: Extraer directamente entre 1 y 8 frames locales, centrados y proporcionales a
la duración.

**Independent Test**: Fixtures de varias duraciones producen conteos monotónicos y puntos
centrados; el modo histórico sigue produciendo 30 por defecto.

- [x] T007 [US1] Añadir primero tests fallidos del modo adaptativo y compatibilidad legacy en `services/search-spike/tests/unit/test_ingest.py`
- [x] T008 [US1] Adaptar extracción y configuración sin cambiar `DEFAULT_FRAMES_PER_VIDEO=30` en `services/search-spike/xtrace_spike/ingest/frames.py` y `services/search-spike/xtrace_spike/indexing/pipeline.py`
- [x] T009 [US1] Añadir primero tests fallidos del contrato CLI `--sampling adaptive` en `services/search-spike/tests/unit/test_cli_index.py`
- [x] T010 [US1] Implementar flags validados y salida estable del CLI local en `services/search-spike/xtrace_spike/cli.py`

**Checkpoint**: El dataset local puede reindexarse adaptativamente de forma explícita.

---

## Phase 4: User Story 2 — Assets realmente disponibles (P1)

**Goal**: Seleccionar hasta 8 frames web únicos y bien distribuidos sin inventar assets ni
timestamps.

**Independent Test**: Storyboards, thumbnails y previews con duplicados, posiciones
escasas o `None` producen un resultado estable de 1–8 frames.

- [x] T011 [US2] Añadir primero tests fallidos de pHash/posiciones duplicadas, timestamps inválidos/`None`, escasez y cierre de imágenes descartadas en `services/crawler/tests/integration/test_pipeline.py`
- [x] T012 [US2] Normalizar posiciones, deduplicar por posición+pHash e integrar la selección compartida en `services/crawler/xtrace_crawler/pipeline.py`
- [x] T013 [US2] Añadir aserciones de que no se acceden nuevos tipos de asset ni vídeos completos en `services/crawler/tests/integration/test_pipeline.py`

**Checkpoint**: El pipeline web limita evidencia permitida sin debilitar compliance.

---

## Phase 5: User Story 3 — Reindexación reproducible (P2)

**Goal**: Reemplazar el corpus seleccionado sin frames antiguos, duplicados ni estados
parciales.

**Independent Test**: Dos ejecuciones iguales producen el mismo conjunto; cambiar de 30 a
adaptativo elimina frames sobrantes y un fallo conserva el conjunto completo anterior.

- [x] T014 [P] [US3] Añadir primero tests fallidos de reemplazo en el pipeline local en `services/search-spike/tests/integration/test_indexing_pipeline.py`
- [x] T015 [US3] Sustituir upsert por reemplazo completo tras embeddings en `services/search-spike/xtrace_spike/indexing/pipeline.py`
- [x] T016 [P] [US3] Añadir primero tests fallidos de elegibilidad, estado de fallo, dedupe concurrente, `reindex/reindex-status` y resumen por vídeo en `services/crawler/tests/integration/test_pipeline.py` y `services/crawler/tests/unit/test_cli.py`
- [x] T017 [US3] Registrar `REINDEX`, revalidar fuente/estado/exclusión, validar perfil y preservar el índice anterior en `services/crawler/xtrace_crawler/pipeline.py`
- [x] T018 [US3] Implementar `reindex` con `run_id`/dedupe key y `reindex-status` agregado en `services/crawler/xtrace_crawler/cli.py`
- [x] T019 [US3] Filtrar elegibilidad y consultar resultados por `run_id` en `services/crawler/xtrace_crawler/repo.py` y `services/crawler/tests/integration/test_pipeline.py`

**Checkpoint**: Corpus local y web reindexables de forma idempotente y coherente.

---

## Phase 6: User Story 4 — Comparar calidad y coste (P2)

**Goal**: Medir recall, error temporal, frames y segmentos con una puerta automática de
adopción.

**Independent Test**: Un fixture conocido genera métricas deterministas y rechaza/acepta
políticas al cruzar SC-004..SC-008.

- [x] T020 [P] [US4] Añadir primero tests fallidos de sidecar local/web, comparación pareada, error normalizado, cobertura mínima y adopción en `services/search-spike/tests/unit/test_sampling_benchmark.py` y `services/search-spike/tests/unit/test_benchmark_dataset.py`
- [x] T021 [US4] Extender casos con fuente+duración+timestamp e implementar informe en `services/search-spike/xtrace_spike/benchmark/dataset.py`, `runner.py` y `sampling.py`
- [x] T022 [US4] Exponer ejecución reproducible y JSON estable en `services/search-spike/xtrace_spike/cli.py`

**Checkpoint**: La política solo puede declararse adoptable con evidencia cuantificada.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T023 [P] Actualizar uso y límites en `services/search-spike/README.md`, `services/crawler/README.md` y `docs/STATUS.md`
- [x] T024 Ejecutar `quickstart.md`, Ruff, mypy, pytest, tests pgvector, `pnpm verify` aplicable y registrar evidencias en `docs/handoffs/TASK-005-001.md`..`TASK-005-004.md`
- [x] T025 [P] Ejecutar revisión estática de ausencia de reconocimiento facial y nuevos tipos de asset y registrarla en `docs/handoffs/TASK-005-SECURITY.md`
- [x] T026 Como orquestador, ejecutar `speckit-analyze`, `speckit-converge` y revisión independiente; resolver hallazgos y actualizar `specs/005-adaptive-frame-sampling/tasks.md`

---

## Dependencies & Execution Order

```text
Setup → Foundation (policy + replace)
                ├─→ US1 local ───────┐
                └─→ US2 web ──→ US3 ├─→ US4 benchmark → Polish
                         US1 ──→ US3 ┘
```

- T003/T004 y T005/T006 pueden desarrollarse en paralelo porque no comparten archivos.
- US1 y US2 pueden comenzar en paralelo tras T004/T006.
- US3 depende de US1 y US2 porque integra ambos pipelines.
- US4 depende de una reindexación reproducible completa.

## Parallel Execution Examples

- **Foundation**: agente A T003–T004; agente B T005–T006.
- **P1 stories**: agente A T007–T010; agente B T011–T013.
- **US3 tests**: T014 y T016 pueden escribirse en paralelo; implementación posterior por
  propietario de cada servicio.
- **Polish**: documentación T023 puede ejecutarse mientras termina la validación técnica.

## Implementation Strategy

1. Entregar primero política pura + local adaptativo como MVP verificable.
2. Integrar selección web sin cambiar el acceso a fuentes.
3. Añadir reemplazo y operación de reindexación completa.
4. Medir antes de cambiar defaults; si falla la puerta, conservar adaptativo como opt-in.

## Format Validation

Las 26 tareas usan checkbox, ID secuencial, etiqueta `[P]` solo cuando procede, etiqueta
`[USn]` en fases de historias y rutas concretas.
