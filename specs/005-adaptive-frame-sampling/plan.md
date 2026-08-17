# Implementation Plan: Muestreo adaptativo de frames

**Branch**: `feature/005-adaptive-frame-sampling` | **Date**: 2026-08-17 | **Spec**:
[spec.md](spec.md)

**Input**: Feature specification from `specs/005-adaptive-frame-sampling/spec.md`

## Summary

Añadir una política de muestreo compartida que calcule entre 1 y 8 frames según la
duración, usando como objetivo un frame por cada 120 segundos y posiciones centradas en
intervalos uniformes. El modo adaptativo será explícito mientras se compara con el
comportamiento histórico de 30 frames. La extracción local solicitará directamente el
conteo calculado; el crawler seleccionará de forma determinista los frames permitidos más
cercanos a los puntos objetivo. La reindexación sustituirá atómicamente la representación
completa de cada vídeo para eliminar frames obsoletos.

## Technical Context

**Language/Version**: Python 3.11

**Primary Dependencies**: Typer, Pillow, FFmpeg/FFprobe, psycopg, pydantic-settings;
interfaces existentes `VectorStore`, `EmbeddingProvider` y adapters del crawler

**Storage**: Supabase PostgreSQL + pgvector; backend en memoria para tests

**Testing**: pytest, Ruff y mypy estricto; pgvector integration tests cuando Supabase
local está disponible

**Target Platform**: Procesos backend/CLI locales y workers Linux/macOS

**Project Type**: Librería y CLI Python compartidas entre el spike y el crawler

**Performance Goals**: Máximo 8 embeddings por vídeo en el índice base y reducción mínima
del 70 % frente a 30 frames cuando existen suficientes assets

**Constraints**: No descargar permanentemente vídeos web completos; solo assets públicos
permitidos; sin reconocimiento facial; reindexación idempotente y sustitución atómica por
vídeo; conservar el default histórico de 30 hasta superar el benchmark

**Scale/Scope**: Corpus local actual de 147 vídeos, preparado para un catálogo global de
millones de vídeos con aproximadamente 8 millones de vectores base por millón de vídeos

## Constitution Check

_Gate evaluado antes de Phase 0 y nuevamente después del diseño de Phase 1._

| Principio | Estado | Evidencia |
| --- | --- | --- |
| Spec-first | PASS | Spec 005 aprobada explícitamente el 2026-08-17. |
| Trazabilidad | PASS | El contrato y las tareas referencian FR/SC/SEC de la spec. |
| Rama/PR independiente | PASS | Rama `feature/005-adaptive-frame-sampling`; no se hará push directo a `main`. |
| Test-first | PASS | La política, selección y reemplazo se implementarán desde tests unitarios e integración. |
| E2E crítico | PASS | No cambia UI; la validación E2E es CLI/integración Python y la búsqueda existente permanece cubierta por WDIO. |
| Seguridad y Supabase | PASS | No cambia esquema; reemplazo transaccional con tests de pgvector y sin secretos. |
| Calidad | PASS | Se ejecutarán Ruff, mypy, pytest, tests DB aplicables y verificación global. |
| Dependencias | PASS | No se añaden dependencias; se reutilizan las existentes. |

**Post-design gate**: PASS. El diseño no introduce servicios, migraciones ni excepciones a
la constitución.

## Phase 0 — Research

Las decisiones y alternativas están consolidadas en [research.md](research.md). No quedan
marcadores `NEEDS CLARIFICATION`.

## Phase 1 — Design

- Modelo y transiciones: [data-model.md](data-model.md)
- Contratos de política, reemplazo y CLI: [contracts/sampling.md](contracts/sampling.md)
- Validación reproducible: [quickstart.md](quickstart.md)

## Project Structure

### Documentation (this feature)

```text
specs/005-adaptive-frame-sampling/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── sampling.md
└── tasks.md
```

### Source Code (repository root)

```text
services/search-spike/
├── xtrace_spike/
│   ├── sampling.py
│   ├── ingest/frames.py
│   ├── indexing/pipeline.py
│   ├── vectorstore/{base,in_memory,pgvector}.py
│   ├── benchmark/
│   └── cli.py
└── tests/{unit,integration}/

services/crawler/
├── xtrace_crawler/{pipeline,config,cli}.py
└── tests/{unit,integration}/
```

**Structure Decision**: La política pura vive en `xtrace_spike`, dependencia ya reutilizada
por el crawler. El crawler conserva la lógica de acceso legal a assets; el módulo
compartido solo trabaja con duración, timestamps y secuencias ya obtenidas.

## Design Decisions

### Política adaptativa

- `target = clamp(ceil(duration_ms / 120_000), 1, 8)` con duración fiable.
- Puntos ideales centrados: `round((i + 0.5) * duration_ms / target)`.
- Una duración nula, cero o negativa se considera no fiable. Sin duración fiable, se
  conservan hasta 8 assets únicos en orden estable y sus timestamps permanecen `None` si
  la fuente no los proporciona.
- Con menos assets que el objetivo, se usan todos; nunca se fabrican.
- Los frames con timestamp se asignan una sola vez al punto ideal más próximo; empates
  por timestamp y orden de entrada.
- Timestamps negativos o fuera de duración se degradan a `None`. Antes de seleccionar se
  deduplican claves de posición y pHashes exactos, conservando el primer representante.

### Compatibilidad y activación

- `DEFAULT_FRAMES_PER_VIDEO = 30` se conserva para reproducir el spike histórico.
- El modo adaptativo se activa explícitamente y siempre produce 1–8 frames. El modo
  `legacy_fixed=30` queda fuera de SC-001/SC-008 y se usa solo como referencia; el
  adaptativo no se convierte en default hasta cumplir SC-004/SC-008.
- El crawler usa `REINDEX` para pruebas; `INDEX_VIDEO` mantiene su contrato actual hasta
  la decisión posterior al benchmark.

### Sustitución coherente

- `VideoIndexWriter.replace_video_index(video_id, records, duration_ms)` es la frontera
  coordinada. Pgvector reemplaza frames y actualiza `videos.status`, `frame_count`,
  `duration_ms` e `indexed_at` en una única transacción.
- In-memory prepara snapshots de frames y estado y solo publica ambos al completar.
- Extracción, dedupe y embeddings terminan antes del reemplazo; un fallo anterior conserva
  la representación completa previa.
- El reemplazo no cambia `excluded`. Un fallo conserva el índice completo anterior; si no
  existía, deja cero frames y estado `failed`.

### Reindexación web y elegibilidad

- Cada lote genera un `run_id` y solo selecciona vídeos `indexed` o `failed` recuperables,
  con `excluded=false` y fuente habilitada.
- El handler revalida estado, exclusión y fuente antes de acceder a assets.
- La `dedupe_key` incluye fuente, vídeo y hash canónico del perfil de muestreo.
- `reindex-status --run-id` informa pendientes, completados, omitidos y fallidos, además
  del resultado por vídeo.

### Benchmark

- Usa las mismas consultas y verdad conocida para cada política.
- Informa Top-1, Top-5, error temporal absoluto mediano/p95, frames y reducción.
- Segmenta por fuente y duración: `<5 min`, `5–15 min`, `>15 min`.
- La pérdida Top-5 se calcula en puntos porcentuales sobre los mismos casos. El error
  normalizado es `abs(predicho-real) / (duration / timestamped_frames)`.
- Debe haber al menos 30 casos, local y una fuente web, tres tramos y 3 casos por segmento
  no vacío; la falta de cobertura falla cerrado.
- “Assets suficientes” significa que la referencia produce al menos 30 frames únicos. Se
  bloquea la adopción si Top-5 cae más de 5 puntos, baja del 80 %, la mediana normalizada
  supera 0,5 o la reducción no alcanza el 70 %.

## Complexity Tracking

No hay violaciones constitucionales que justificar.
