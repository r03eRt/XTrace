# STATUS — XTrace

> Documento vivo para continuar el proyecto con **cualquier** agente. Lo actualiza el
> orquestador tras cada PR. Fuente de verdad de requisitos: `docs/PRODUCT_IDEA.md` y
> `specs/`. Contrato: `AGENTS.md` + `.specify/memory/constitution.md`.

**Última actualización**: 2026-08-14 · por DeepSeek V4 Pro (orquestador).

## Setup de agentes (esta ejecución)

- **Orquestador**: DeepSeek V4 Pro (`deepseek-v4-pro`) → asigna tareas, resuelve dependencias, actualiza este
  archivo y `tasks.md`. **No** implementa. Único que edita `tasks.md`.
- **Implementador**: DeepSeek V4 Flash (`deepseek-v4-flash`) → ejecuta **una** tarea a la vez respetando su
  `allowed_paths`. Carga skill `task-execution` antes de tocar archivos.
- **Revisor**: un agente **distinto** al implementador (idealmente otro proveedor). Carga
  `task-review`. No implementa. Con PR-016 (puerta de decisión) revisa un modelo distinto.
- Handoff obligatorio por PR en `docs/handoffs/PR-0NN.md` (plantilla en `docs/handoffs/`).

### Política de coste/tokens (modelos)

- **`deepseek-v4-pro`** (orquestador): SOLO para orquestación y puertas de decisión (p. ej. PR-016). Coste ≈ $0.435 / $0.87 por 1M tokens (in/out).
- **`deepseek-v4-flash`** (implementador y revisor DeepSeek): modelo por defecto de ejecución, para ahorrar tokens. Coste ≈ $0.14 / $0.28 por 1M tokens.
- **Enforcement**: el orquestador fija el modelo del implementador vía `workflow.agent({ provider: "deepseek-official", model: "deepseek-v4-flash" })`, porque la tool `subagent` no expone selector de modelo (hereda el default `deepseek-v4-pro`). El revisor, si es DeepSeek, usa también flash; idealmente otro proveedor (constitución §5).

## Fase actual

**Fase 1 — Visual Search Spike** (`specs/001-visual-search-spike`).
Estado spec: **APPROVED** (2026-08-14). Documentación de diseño **completa**:
`plan.md`, `data-model.md`, `contracts/`, `quickstart.md`, `tasks.md`, ADR-0003..0008,
`docs/architecture/visual-search-spike.md`.

**Implementación en curso.** PR-001…PR-014 (14 PRs) + FIX-phash implementados, revisados (APPROVED) y mergeados a `feature/001-visual-search-spike`. **Milestone 1 mergeado a `main`** (PR #1, CI verde). **US1 (indexar) y US2 (buscar por imagen) FUNCIONALES end-to-end** (CLI: index/stats/search/exclude). Siguiente: PR-015/016 (benchmark, requiere dataset real del operador).

## Roadmap de la fase

18 PRs (PR-001 … PR-018), ninguno XL. Ver `specs/001-visual-search-spike/tasks.md` para
objetivos, dependencias, `allowed_paths`, tests y criterios. Grafo de dependencias y plan
de paralelización incluidos allí.

- **PRs completados**: PR-001…PR-014 (14 PRs) + FIX-phash (implementados + revisados APPROVED + mergeados a `feature/001-visual-search-spike`; milestone 1 mergeado a `main`).
- **PRs abiertos**: — (14 PRs + 1 fix en la rama de integración).
- **Siguiente tarea**: PR-015 (generador dataset benchmark ~210 casos) y PR-016 (runner + CLI `benchmark`) — requieren **dataset local real** del operador para la puerta SC-001.

## Primer PR recomendado y por qué

**PR-001**. Habilita el toolchain Python (ruff/mypy/pytest) y la CI sin romper la pipeline
JS del skeleton. Desbloquea la Ola A (PR-002/003/004/008 en paralelo). Es pequeño, de bajo
riesgo y necesario antes de cualquier lógica de dominio.

## Puerta de decisión del spike

El spike se valida si el benchmark cumple **SC-001: Top-5 ≥ 80%** (positivos) y **SC-002**
(negativas), con latencia reportada (SC-003). Se evalúa en **PR-016** y se decide
30 vs 60 frames/vídeo en **PR-017**. Si no se cumple → no escalar crawling; revisar modelo/
frames/pipeline (spec §101: *VALIDATE SEARCH FIRST, SCALE CRAWLING SECOND*).

## Blockers conocidos

- Ninguno técnico. Requiere **dataset local** aportado por el operador para PR-008+ y
  benchmark (fuera del control del agente).

## Decisiones pendientes

- ~~Dimensión `D`~~ → **FIJADA: D = 768** (SigLIP v1 ViT-B-16-SigLIP, torch 2.2.2 CPU Intel-Mac, PR-005; anexado a ADR-0005). La usará la migración PR-006.
- Uso de `halfvec` vs `vector`: decidir con benchmark (PR-016), documentar en ADR-0004.

## Deuda técnica / diferido

- **Búsqueda por clip + consistencia temporal** (FR-011, SC-004) — diferida (Decisión D1),
  próxima feature.
- Crawler, `SourceAdapter` de fuentes reales (erome, xvideos, xhamster, redgifs, pornhub),
  FastAPI, frontend Next.js, admin, compliance pública — features posteriores del MVP
  (ver `docs/PRODUCT_IDEA.md`).
- ~~Persistir pHash en frames~~ → **RESUELTO (FIX-phash)**: `FrameRecord.phash` añadido;
  InMemory+Pg persisten el pHash real con codec bigint↔uint64. Pendiente para PR-013:
  exponer lectura del phash desde PgVectorStore (`get_frame` solo existe en InMemory).

## Plan de coste (objetivo)

| Escenario | Infra | Coste mensual | Coste puntual (indexación) |
| --- | --- | --- | --- |
| **Desarrollo / spike** | Local + Docker + Supabase Free + embeddings CPU local | **~0 €** | 0 € (dataset local pequeño) |
| **MVP** (~3k vídeos / 90k emb.) | Supabase Free/Pro, R2 free, crawler local/VPS ~5 €, GPU serverless por uso | **~0–25 €** | GPU serverless puntual (créditos Modal / bajo) |
| **~100k vídeos** (~3M emb.) | Supabase Pro, pgvector (medir), VPS crawler, GPU por lotes | **~25–50 €+** | Indexación por lotes en GPU serverless |
| **~1M vídeos** (~30M emb.) | Evaluar Qdrant / infra vectorial dedicada (ADR futuro) | según uso | mayor; planificar por lotes |

Principio: **cheap first, scale when proven**. No añadir servicios de pago sin ADR con
coste estimado (AGENTS.md / prompt maestro §91).

## Puertas de calidad (recordatorio)

- Python (por PR del spike): `ruff` + `mypy` + `pytest` (+ integration/pgTAP cuando aplique).
- DB: `pnpm test:db` (pgTAP).
- Skeleton JS: `pnpm verify` permanece verde (el spike no toca el app Next.js).
- Sin merge a `main` sin CI verde y **aprobación humana**. El implementador no aprueba su
  propio trabajo.
