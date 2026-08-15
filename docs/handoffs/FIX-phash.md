# Handoff — FIX-phash

## Contrato de tarea (skill task-execution)

```yaml
task_id: FIX-phash
spec_id: 001-visual-search-spike
title: "Persistir el pHash real de cada frame en el índice (prerrequisito FR-004/006 y evidencia pHash del ranking PR-013)"
status: READY_FOR_REVIEW
assigned_agent: implementer
provider: deepseek-v4-flash
branch: feature/001-visual-search-spike-FIX-phash-persistence
worktree: /Users/robertomorgadoluengo/work/code/XTrace (rama dedicada checkeada)
requirements:
  - FR-004 (firma perceptual pHash por frame representativo)
  - FR-006 (persistir por frame: video_id, timestamp, pHash, embedding)
  - Prerrequisito de la evidencia pHash del ranking (FR-013 / PR-013)
  - Hallazgo PR-010: el contrato FrameRecord no transporta phash y PgVectorStore inserta el centinela 0
acceptance_criteria:
  - "FrameRecord (vectorstore/base.py) transporta el pHash real de cada frame"
  - "InMemoryVectorStore conserva el pHash del FrameRecord (get_frame lo devuelve)"
  - "El pipeline de indexación pone en el FrameRecord el pHash calculado (compute_phash, sin relectura de disco)"
  - "PgVectorStore persiste el pHash real en frames.phash (columna != 0 tras upsert) sin duplicar cálculos"
  - "Sin cambios en la semántica de dedupe/ranking/búsqueda"
  - "gates: ruff check && ruff format --check && mypy xtrace_spike tests && pytest (exit 0)"
dependencies: [PR-004 (hashing/phash.py), PR-009 (dedupe), PR-010 (pipeline), PR-007 (PgVectorStore)]
allowed_paths:
  - services/search-spike/xtrace_spike/vectorstore/base.py
  - services/search-spike/xtrace_spike/vectorstore/in_memory.py
  - services/search-spike/xtrace_spike/vectorstore/pgvector.py
  - services/search-spike/xtrace_spike/indexing/pipeline.py
  - services/search-spike/tests/unit/test_vectorstore_inmemory.py
  - services/search-spike/tests/integration/test_indexing_pipeline.py
  - services/search-spike/tests/integration/test_pgvector_store.py
  - docs/handoffs/FIX-phash.md
forbidden_paths: [search/**, cli.py, ingest/dedupe.py, ingest/frames.py, repo.py, supabase/**, specs/**, docs/adr/**, resto del monorepo]
required_tests:
  - "InMemory: el FrameRecord conserva el pHash real (no centinela)"
  - "Pipeline: el FrameRecord lleva el pHash calculado (== compute_phash del frame)"
  - "PgVectorStore: columna frames.phash != 0 tras el upsert (con round-trip del rango completo 64 bits)"
reviewer: pendiente (agente distinto vía task-review)
started_at: 2026-08-16
completed_at: 2026-08-16
```

- **Resumen**: Se cierra el gap de contrato detectado en PR-010 (ver handoff
  PR-010, Limitaciones): el `FrameRecord` ahora transporta el **pHash real**
  de cada frame representativo (FR-004) y ambas implementaciones del
  `VectorStore` lo persisten — el doble in-memory lo conserva tal cual
  (`get_frame` lo devuelve) y `PgVectorStore` lo escribe en la
  columna `frames.phash` (bigint) **sin el centinela 0**. El pipeline
  calcula el pHash con `hashing.phash.compute_phash` — la misma función
  que usa el dedupe (PR-009) — reutilizando la imagen ya abierta de cada lote
  de embedding (sin relectura de disco). No se cambia la semántica de
  dedupe/ranking/búsqueda ni se tocan archivos fuera de allowed_paths.
- **Hallazgo nuevo (bloqueante para persistir el pHash crudo)**: la columna
  `frames.phash` de la migración PR-006 es `bigint` (con signo),
  pero el pHash de 64 bits sin signo de imagehash casi siempre tiene el bit 63
  a 1 (el coeficiente DC del DCT es el mayor del bloque 8×8 y se empaqueta en
  el MSB del entero). Verificado empíricamente: 7/7 imágenes probadas
  (sintéticas, plana y frame real de testsrc2) dan valores >= 2^63, que
  desbordan bigint ("bigint out of range", error 22003) y marcarían todos los
  vídeos como `failed` con PgVectorStore. Solución: codificación
  biyectiva con signo (complemento a dos) en `phash_to_db` /
  `phash_from_db` (pgvector.py), documentada para los lectores futuros.
- **Requisitos implementados**:
  - **FR-004/FR-006 (FIX-phash)**: el pHash real de cada frame representativo
    viaja en el contrato y se persiste en ambos stores. El ranking de PR-013
    ya no encontrará el centinela 0 en la DB y podrá usar la evidencia pHash
    (FR-013) leyendo con `phash_from_db`.
  - Trazabilidad: los tests nuevos marcan FIX-phash · FR-004/FR-006
    (constitución §3).
- **Archivos modificados** (7 + este handoff; todos dentro de allowed_paths):
  - services/search-spike/xtrace_spike/vectorstore/base.py — contrato:
    `FrameRecord.phash` (int sin signo de 64 bits) requerido; docstrings
    actualizadas.
  - services/search-spike/xtrace_spike/vectorstore/in_memory.py —
    `_StoredFrame.phash`, upsert lo conserva y nuevo método público
    `get_frame(frame_id) -> FrameRecord | None` (inspección/tests; no
    forma parte del Protocol).
  - services/search-spike/xtrace_spike/vectorstore/pgvector.py — codec
    `phash_to_db`/`phash_from_db` (bigint con signo), upsert
    persiste `phash_to_db(record["phash"])`; eliminado `_PHASH_PLACEHOLDER = 0`.
  - services/search-spike/xtrace_spike/indexing/pipeline.py —
    `_embed_frames` calcula `compute_phash(image)` sobre la misma
    imagen abierta del lote y lo incluye en el FrameRecord.
  - tests/unit/test_vectorstore_inmemory.py — helper con `phash` +
    test de conservación del pHash real.
  - tests/integration/test_indexing_pipeline.py — test de que el FrameRecord
    lleva el pHash real (== compute_phash) por frame.
  - tests/integration/test_pgvector_store.py — helper con pHash derivado del
    frame_id (bit 63 activo en todos los upserts) + test de persistencia y
    round-trip del rango completo [0, 2^64).
  - docs/handoffs/FIX-phash.md — este handoff.
- **Decisiones tomadas**:
  - **Codec con signo para bigint (obligatorio, no cosmético)**: el pHash sin
    signo crudo desborda `bigint` (hallazgo arriba). Se persiste la
    reinterpretación en complemento a dos: biyección determinista
    (`phash_to_db(0) = 0`, `phash_to_db(2^64-1) = -1`), que
    preserva igualdad y distancia de Hamming tras decodificar. Los lectores
    (PR-013) DEBEN usar `phash_from_db`; queda anotado en docstrings y
    en Trabajo pendiente.
  - **Validación de rango en el codec**: `phash_to_db` rechaza valores
    fuera de [0, 2^64) (error temprano, mismo espíritu que la validación de
    dimensión del embedding).
  - **Sin duplicar lecturas de disco**: el pHash se calcula sobre la imagen ya
    abierta del lote de embedding; el único coste extra es el DCT de
    imagehash. Reutilizar el valor interno del dedupe no es posible sin tocar
    `ingest/dedupe.py` (fuera de allowed_paths) — ver Limitaciones.
  - **`get_frame` en InMemoryVectorStore**: método propio (no del
    Protocol) para que tests y consumidores inspeccionen el registro
    almacenado, incluido el pHash.
  - **Sin cambios de contrato en FrameHit ni en la semántica del ANN**:
    búsqueda, agrupación y ranking existentes (PR-012) siguen intactos.
- **Tests añadidos** (3 nuevos; todos pasan):
  1. test_vectorstore_inmemory.py::test_upsert_preserves_real_phash_in_frame_record —
     FIX-phash: upsert con pHash real (bit 63 a 1) y `get_frame` lo
     devuelve idéntico y != 0; get_frame de un id inexistente → None.
  2. test_indexing_pipeline.py::test_pipeline_persists_real_phash_per_frame —
     FIX-phash · FR-004/FR-006: se compara el pHash almacenado de CADA frame
     contra `compute_phash` del frame en una extracción+dedupe
     independiente con la misma configuración (frame_ids estables, FR-008):
     igualdad exacta y nunca 0.
  3. test_pgvector_store.py::test_upsert_persists_real_phash — FIX-phash:
     upsert de pHashes [0, 42, 2^63-1, 2^63, 2^64-1]; la columna queda
     codificada (los != 0 no son 0) y `phash_from_db` devuelve el valor
     exacto del contrato (round-trip completo).
- **Comandos ejecutados** (en services/search-spike, exit codes reales;
  UV_CACHE_DIR=/tmp/uv-cache-xtrace):
  - uv run ruff check → 0 ("All checks passed!").
  - uv run ruff format --check → 0 (37 files already formatted; 3 tests
    reformateados automáticamente antes de la verificación final).
  - uv run mypy xtrace_spike tests → 0 ("Success: no issues found in 36
    source files").
  - uv run pytest → 0 (108 passed, 1 skipped — el @slow de SigLIP; la suite
    anterior en esta rama tenía 82 passed + 1 skipped: +3 tests de FIX-phash
    y los +26 de PR-011/PR-012 previos).
  - uv run pytest tests/integration/test_pgvector_store.py
    tests/integration/test_indexing_pipeline.py -v → 0 (25 passed; Supabase
    local en postgresql://postgres:postgres@127.0.0.1:55322/postgres arriba,
    sin skips).
- **Resultados**: los 4 gates en verde (exit 0). `git status` muestra
  solo los 7 archivos de allowed_paths (7 files changed, +221/−19); ningún
  archivo fuera de alcance tocado (search/**, cli.py, ingest/**,
  embeddings/**, repo.py, supabase/**, specs/** intactos). Los tests de DB
  corrieron de verdad contra Supabase local.
- **Limitaciones**:
  - **No se reutiliza el pHash interno del dedupe**: dedupe_frames (PR-009)
    calcula el pHash y lo descarta; su módulo está fuera de allowed_paths, así
    que el pipeline recalcula el pHash de los frames conservados con la misma
    función (compute_phash). Coste extra: solo el DCT (no relectura de disco,
    se usa la imagen del lote ya abierta). Si se quisiera eliminar esa
    duplicación, una tarea futura con acceso a ingest/dedupe.py podría hacer
    que devuelva los phashes junto a los frames.
  - **El valor almacenado en Pg está codificado con signo**: quien lea
    frames.phash (PR-013, benchmark, SQL manual) debe aplicar
    phash_from_db. Los tests lo cubren; los consumidores futuros no.
  - **contracts/README.md no se actualizó** (fuera de allowed_paths): la
    sección §2 del contrato documenta FrameRecord sin phash. El orquestador
    debe reflejar el nuevo campo (docs) o autorizar una tarea de docs.
- **Riesgos**:
  - Que PR-013 lea frames.phash sin phash_from_db (comparación incorrecta):
    mitigado con docstrings en el codec y esta sección de Trabajo pendiente.
  - Si una futura migración cambiara phash a otra representación (p. ej.
    bit(64) o halfvec para embeddings), el codec quedaría obsoleto: es un
    detalle localizado en pgvector.py.
- **Trabajo pendiente**:
  - PR-013 (ranking): usar la columna frames.phash con phash_from_db como
    evidencia pHash (FR-013); el query_phash de image_search (PR-012) ya está
    preparado para comparar.
  - El orquestador: actualizar specs/001-visual-search-spike/contracts/README.md
    §2 para reflejar FrameRecord.phash.
- **Instrucciones para el revisor**:
  1. git diff main...HEAD --stat: solo los 7 archivos de allowed_paths +
     este handoff; search/**, cli.py, ingest/**, repo.py sin cambios.
  2. En services/search-spike: UV_CACHE_DIR=/tmp/uv-cache-xtrace uv run ruff
     check → ruff format --check → mypy xtrace_spike tests → pytest (exit 0);
     con Supabase local arriba, pytest tests/integration/test_pgvector_store.py
     tests/integration/test_indexing_pipeline.py -v (25 passed).
  3. Revisar el codec phash_to_db/phash_from_db (bigint con signo), el
     transporte del pHash por el pipeline (compute_phash sobre la imagen del
     lote) y los 3 tests de regresión (InMemory, pipeline, Pg).
  4. Confirmar que la semántica de dedupe/ranking/búsqueda no cambió y que
     no queda ningún centinela 0 salvo que el registro lo pida explícitamente.
