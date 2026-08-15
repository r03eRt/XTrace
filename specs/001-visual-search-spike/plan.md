# Implementation Plan: Visual Search Spike (XTrace)

**Branch**: `feature/001-visual-search-spike` | **Date**: 2026-08-14 | **Spec**: [`spec.md`](./spec.md)

**Input**: Feature specification from `specs/001-visual-search-spike/spec.md` (`APPROVED`).

> Este plan define **cómo** se implementa la spec aprobada. No altera requisitos.
> Decisiones registradas como ADRs en `docs/adr/`.

## Summary

Construir un **servicio Python (CLI)** que ingiere un **dataset local de vídeos**, extrae
**frames representativos** (FFmpeg), los **deduplica** por perceptual hash, calcula
**pHash** + **embedding visual (SigLIP)** por frame y los persiste en **Supabase
PostgreSQL + pgvector (HNSW)**. Una **búsqueda por imagen** normaliza la consulta, calcula
pHash + embedding, ejecuta **ANN**, agrupa candidatos por vídeo, aplica un **ranking
configurable** (similitud visual + nº de frames + evidencia pHash) y devuelve vídeos con
**match score** y **timestamp aproximado**. Un **benchmark** de ~210 casos mide
Top-1/5/10 y latencia para decidir la puerta **SC-001: Top-5 ≥ 80%**.

El spike **no** añade frontend, endpoints HTTP, crawler ni auth (todo diferido). La
interfaz es una **CLI interna** (Decisión D2). La búsqueda por **clip** queda diferida (D1).

## Technical Context

**Language/Version**: Python 3.11 (servicio del spike) sobre el skeleton Next.js/TS ya
existente (no modificado por esta feature).

**Primary Dependencies** (dirección; versiones exactas se fijan en el PR de bootstrap):
- Proceso de vídeo/imagen: `ffmpeg` (binario), `Pillow`, `imagehash` (pHash), `numpy`.
- Embeddings: `open_clip_torch` / `transformers` con modelo **SigLIP2** (o SigLIP);
  `torch` (CPU en dev; GPU/serverless opcional). Detrás de `EmbeddingProvider`.
- Vector store: Supabase Postgres + **pgvector** (extensión `vector`), acceso vía
  `psycopg`/`asyncpg`. Detrás de `VectorStore`.
- CLI: `typer`. Config: `pydantic-settings`. Tests: `pytest` (+ `pytest-cov`).
- Lint/format Python: `ruff`. Typecheck Python: `mypy` (o `pyright`).

**Storage**: Supabase PostgreSQL (`videos`, `frames`, `searches`) con `pgvector`. Sin
almacenamiento permanente de vídeos (FR-018). Frames físicos temporales borrados (FR-009).

**Testing**: `pytest` (unit + integration Python), `pgTAP` vía `supabase test db` (esquema
y constraints), sin E2E de UI (no hay UI en el spike; WebdriverIO N/A para esta feature).

**Target Platform**: Ejecución local (CLI) en Linux/macOS + Docker; DB Supabase local o
cloud Free.

**Project Type**: Servicio/CLI Python en monorepo, junto al skeleton web (no usado aquí).

**Performance Goals**: Búsqueda **< 3 s** p95 sobre el dataset del spike (SC-003, objetivo
medido). Throughput de embeddings reportado por el benchmark.

**Constraints**: Coste dev **0–10 €/mes** (free tier + local). Idempotencia (FR-008),
cleanup garantizado (FR-009), abstracciones reemplazables (`VectorStore`,
`EmbeddingProvider`).

**Scale/Scope**: Spike 100–3.000 vídeos, ~30 frames/vídeo, ~90k vectores. No se resuelve
escalado más allá.

## Constitution Check

_GATE: debe pasar antes de implementar._

- **Spec-first** ✔ Spec `APPROVED` antes de este plan.
- **Aprobación humana** ✔ Registrada 2026-08-14.
- **Trazabilidad** ✔ Cada componente mapea FR/SC (ver *Requirements coverage*). Tests
  indicarán el requisito que validan.
- **PRs aislados** ✔ El roadmap (`tasks.md`) descompone en PRs pequeños, sin `XL`.
- **Multiagente** ✔ Plan pensado para orquestador + implementador (DeepSeek): tareas
  paralelizables marcadas, `allowed_paths` por tarea, handoffs obligatorios.
- **Testing test-first** ✔ Lógica de dedupe/ranking/vectorstore con tests primero.
- **Fronteras seguridad** ✔ `service_role` solo en el servicio Python del lado servidor;
  nunca en cliente. SSRF N/A (sin descargas remotas en el spike; dataset local).

Sin violaciones que justificar → *Complexity Tracking* vacío.

## Project Structure

### Documentation (this feature)

```text
specs/001-visual-search-spike/
├── spec.md            # Qué/por qué (APPROVED)
├── plan.md            # Este archivo (cómo)
├── data-model.md      # Modelo de datos detallado (creado en PR de esquema)
├── contracts/         # Contratos CLI + interfaces VectorStore/EmbeddingProvider
├── quickstart.md      # Cómo ejecutar index/search/benchmark en local
└── tasks.md           # Roadmap de PRs (creado por task-planning)
```

### Source Code (repository root)

```text
services/
└── search-spike/                 # Servicio Python del spike (nuevo)
    ├── pyproject.toml
    ├── README.md
    ├── Dockerfile
    ├── xtrace_spike/
    │   ├── __init__.py
    │   ├── cli.py                 # Typer: index | search | benchmark | exclude | stats
    │   ├── config.py             # pydantic-settings (thresholds, frames/vídeo, batch)
    │   ├── ingest/
    │   │   ├── dataset.py        # carga dataset local (FR-001)
    │   │   ├── frames.py         # extracción FFmpeg (FR-002)
    │   │   └── dedupe.py         # dedupe por pHash (FR-003)
    │   ├── hashing/phash.py      # pHash (FR-004)
    │   ├── embeddings/
    │   │   ├── provider.py       # EmbeddingProvider (ABC) (FR-005, ADR-0007)
    │   │   └── siglip_local.py   # impl local SigLIP
    │   ├── vectorstore/
    │   │   ├── base.py           # VectorStore (ABC) (FR-006, ADR-0007)
    │   │   └── pgvector.py       # impl pgvector/HNSW (ADR-0004)
    │   ├── search/
    │   │   ├── image_search.py   # pipeline imagen (FR-010)
    │   │   └── ranking.py        # ranking configurable (FR-013)
    │   ├── indexing/pipeline.py  # orquesta ingest→dedupe→hash→embed→persist (FR-007/008/009)
    │   ├── benchmark/
    │   │   ├── dataset.py        # generación/carga casos (FR-015)
    │   │   └── runner.py         # métricas Top-K/latencia (FR-016, SC-001..007)
    │   └── repo.py               # acceso videos/frames/searches (FR-007, FR-014)
    └── tests/
        ├── unit/                 # dedupe, phash, ranking, provider/vectorstore fakes
        ├── integration/          # pipeline + pgvector contra DB local
        └── fixtures/             # ~10 vídeos / 100 frames deterministas (spec §70)

supabase/
└── migrations/
    └── <ts>_visual_search_spike.sql   # pgvector + videos/frames/searches + índices

supabase/tests/
    └── visual_search_spike_schema.test.sql   # pgTAP: constraints, índices, RLS
```

**Structure Decision**: Monorepo. El servicio del spike vive en `services/search-spike/`
(Python), aislado del app Next.js del skeleton (que **no** se toca en esta feature). La DB
se comparte vía `supabase/migrations`. Alinea con el monorepo conceptual del prompt maestro
(§58) sin reestructurar el skeleton.

## Data model (resumen; detalle en `data-model.md`)

- **videos**: `id` (uuid pk), `local_ref` (text, unique), `duration_ms` (int null),
  `status` (enum: discovered/pending/indexing/indexed/failed), `frame_count` (int),
  `excluded` (bool default false, FR-014), `error` (text null), `indexed_at`,
  `created_at`, `updated_at`. Unicidad: `UNIQUE(local_ref)`.
- **frames**: `id` (uuid pk), `video_id` (fk→videos, on delete cascade), `timestamp_ms`
  (int null), `phash` (bit(64) o bigint), `embedding` (`vector(D)` o `halfvec(D)`, ADR-0004),
  `width`, `height`, `source_kind` (text), `created_at`.
  Idempotencia (FR-008): `UNIQUE(video_id, timestamp_ms)` (o `(video_id, frame_seq)`).
  Índices: **HNSW** sobre `embedding` (`vector_cosine_ops`), índice sobre `phash`,
  índice sobre `video_id`.
- **searches**: `id` (uuid pk), `search_type` (enum: image), `processing_ms` (int),
  `results_count` (int), `created_at`. **No** persiste media de consulta (FR-018, privacidad).

`D` = dimensión del embedding del modelo elegido (se fija al elegir SigLIP en el PR de
embeddings). Se evaluará `halfvec` para ~½ almacenamiento (ADR-0004).

RLS: tablas internas; el servicio Python usa `service_role`. RLS habilitada con políticas
restrictivas por defecto (deny) + acceso vía service key desde el servidor.

## Contracts (detalle en `contracts/`)

**CLI (Typer)** — Decisión D2:
- `xtrace-spike index --dataset <path> [--frames-per-video N] [--dedupe-threshold T]`
- `xtrace-spike search --image <path> [--top-k K]` → JSON de resultados (video, score, ts).
- `xtrace-spike benchmark --cases <path>` → informe Top-1/5/10, latencia p50/p95, tamaño.
- `xtrace-spike exclude --video <id>` (FR-014). `xtrace-spike stats` (métricas del índice).

**`VectorStore` (ABC)**: `upsert_frames(frames)`, `ann_search(embedding, k) -> [FrameHit]`,
`delete_video(video_id)`, `stats()`. Impl: `PgVectorStore`.

**`EmbeddingProvider` (ABC)**: `embed_images(list[Image]) -> ndarray` (batch, FR-005),
`dimension`, `model_id`. Impl: `SiglipLocalProvider`.

## Security strategy

- `service_role` de Supabase solo en el servicio Python (servidor); nunca en cliente.
- Validación de entrada de la CLI: MIME/firma de imagen, tamaño (≤10 MB img), rutas
  temporales seguras (SEC). Sin descargas remotas → **sin superficie SSRF** en el spike.
- Media de consulta **borrada inmediatamente** tras procesar (privacidad, `ASSUMPTION-6`).
- Cleanup en `try/finally` para todos los temporales (FR-009).
- RLS habilitada; políticas deny-by-default verificadas con pgTAP.

## Testing strategy

- **Unit (pytest)**: `phash` (distancia/estabilidad), `dedupe` (umbral), `ranking`
  (combinación de señales), `EmbeddingProvider`/`VectorStore` con dobles de prueba.
- **Integration (pytest)**: pipeline de indexación end-to-end sobre fixtures + `PgVectorStore`
  real contra Supabase local; búsqueda por imagen y verificación de agrupación/ranking.
- **DB (pgTAP, `supabase test db`)**: existencia de tablas, constraints únicos (FR-008),
  índices HNSW/phash, RLS deny-by-default.
- **Benchmark (pytest-driven o comando)**: reproducibilidad (SC-007), Top-K (SC-001),
  negativas (SC-002), idempotencia (SC-005), cleanup (SC-006).
- **E2E de UI**: N/A en el spike (sin frontend). El `test:e2e:smoke` del skeleton se
  mantiene intacto.
- Tests marcan el requisito que validan (trazabilidad, constitución §3/§6).

## Deployment / CI strategy

- El spike es **local-first**: CLI + Supabase local/cloud Free. **No** despliega a Vercel.
- Nuevo job de CI (GitHub Actions) para el servicio Python: `ruff` (lint+format check),
  `mypy` (typecheck), `pytest` (unit) y, con servicios de Postgres+pgvector, `pytest`
  integration + `pgTAP`. Se añade sin romper la pipeline JS existente.
- `docker compose` levanta el servicio del spike + FFmpeg para reproducibilidad (§59).
- Gate por PR: `ruff && mypy && pytest && supabase test db` para tareas del spike; la
  pipeline JS (`format:check/lint/typecheck/test/build`) permanece verde sin cambios.

## Observability

- Logs estructurados de la CLI (job id, vídeo, nº frames, tiempos por etapa).
- `stats` expone: total vídeos, indexados, fallidos, frames, vectores, tamaño índice,
  tiempo medio de indexación y de búsqueda (subconjunto de §69 relevante al spike).
- Informe de benchmark persistido como artefacto reproducible (SC-007).

## Requirements coverage (trazabilidad)

| Requisito | Cubierto por |
| --- | --- |
| FR-001 | `ingest/dataset.py`, CLI `index` |
| FR-002 | `ingest/frames.py` (FFmpeg) |
| FR-003 | `ingest/dedupe.py` (pHash threshold) |
| FR-004 | `hashing/phash.py` |
| FR-005 | `embeddings/provider.py` + `siglip_local.py` (batch) |
| FR-006 | `vectorstore/pgvector.py` + migración (HNSW) |
| FR-007 | `repo.py`, tabla `videos`/estado |
| FR-008 | `UNIQUE(video_id,timestamp_ms)`, upsert idempotente |
| FR-009 | cleanup `try/finally` en `indexing/pipeline.py` |
| FR-010 | `search/image_search.py` |
| FR-011 | DIFERIDO (D1) |
| FR-012 | `search/ranking.py` (score + timestamp) |
| FR-013 | `search/ranking.py` (pesos configurables) |
| FR-014 | `repo.py` `exclude`, columna `excluded`, filtro en búsqueda |
| FR-015 | `benchmark/dataset.py` (~210 casos) |
| FR-016 | `benchmark/runner.py` (informe) |
| FR-017 | `cli.py` (Typer) |
| FR-018 | esquema sin blobs de vídeo; searches sin media |
| SC-001..007 | `benchmark/runner.py` + tests integration/pgTAP |

## Risks (plan)

- **Elección de modelo/dimensión** afecta esquema (`vector(D)`): fijar en PR de embeddings
  antes del PR de esquema definitivo, o migración de dimensión. Mitigación: PR de embeddings
  precede al índice HNSW definitivo; `halfvec` evaluado con benchmark.
- **Torch/embeddings en CI**: pesado. Mitigación: proveedor *fake/deterministic* para unit/
  integration en CI; SigLIP real solo en benchmark local/opcional.
- **Rendimiento pgvector** con 90k vectores: medido en benchmark; si insuficiente, se
  documenta como hallazgo (no se migra a Qdrant en el spike).
- **Dataset poco representativo**: responsabilidad del operador; el benchmark exige ~30
  casos por variante para robustez (D3).

## ADRs (creados en `docs/adr/`)

- `0003` Servicio Python para indexación/búsqueda visual junto al skeleton Next.js.
- `0004` pgvector + HNSW como VectorStore del spike/MVP (y evaluación de `halfvec`).
- `0005` Matching doble: pHash (near-exact) + embeddings visuales SigLIP (semántico).
- `0006` Procesamiento temporal de media: no almacenar vídeos; cleanup garantizado.
- `0007` Abstracciones reemplazables: `VectorStore` y `EmbeddingProvider`.
- `0008` CLI como interfaz de validación del spike; FastAPI/frontend diferidos.

## Bloqueos

Ninguno. Spec sin ambigüedades. Próximo paso: `task-planning` → `tasks.md`.
