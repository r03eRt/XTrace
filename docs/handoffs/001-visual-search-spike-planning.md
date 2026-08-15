# Handoff — 001-visual-search-spike (Planificación)

- **Resumen**: Fase de arranque completada por Opus como orquestador. Descubrimiento →
  spec (APPROVED) → plan técnico → ADRs → roadmap de PRs. Deja el proyecto listo para que
  DeepSeek (Pro v4 orquestador + v3 Flash implementador) ejecute la implementación del
  spike de búsqueda visual. **No se ha escrito código de producto.**
- **Requisitos implementados**: ninguno (fase de diseño). Todos los FR/SC de la spec 001
  quedan trazados a PRs en `tasks.md`.
- **Archivos modificados/creados**:
  - `docs/PRODUCT_IDEA.md` (descubrimiento cerrado, estado `SPEC_APPROVED_PLANNED`).
  - `specs/001-visual-search-spike/spec.md` (**APPROVED**), `plan.md`, `data-model.md`,
    `contracts/README.md`, `quickstart.md`, `tasks.md`.
  - `docs/adr/0003..0008-*.md` (6 ADRs).
  - `docs/architecture/visual-search-spike.md` (+ enlace en `docs/architecture/README.md`).
  - `docs/STATUS.md` (nuevo).
- **Decisiones tomadas**:
  - D1: spike solo búsqueda por **imagen** (clip diferido). D2: interfaz **CLI**. D3:
    benchmark **~210 casos**.
  - ADR-0003 servicio Python; 0004 pgvector+HNSW; 0005 pHash+SigLIP; 0006 media temporal;
    0007 abstracciones `VectorStore`/`EmbeddingProvider`; 0008 CLI (FastAPI/frontend diferidos).
- **Tests añadidos**: ninguno aún; cada PR define sus tests requeridos en `tasks.md`.
- **Comandos ejecutados**: solo lectura/escritura de documentación. Sin builds ni tests.
- **Resultados**: documentación de diseño completa y coherente; `NEEDS CLARIFICATION` = 0.
- **Limitaciones**: la dimensión `D` del embedding se fija en PR-005 antes de la migración
  PR-006. El benchmark requiere un dataset local aportado por el operador.
- **Riesgos**: elección de modelo/dimensión, coste de Torch en CI (mitigado con
  `FakeEmbeddingProvider`), rendimiento pgvector (se mide en PR-016).
- **Trabajo pendiente**: implementar PR-001 … PR-018 (empezar por **PR-001**).
- **Instrucciones para el revisor**: verificar que cada PR respeta `allowed_paths`, que los
  tests citan el requisito que validan, y que no hay merge a `main` sin CI verde ni
  aprobación humana. Con PR-016 (puerta SC-001) usar preferentemente otro proveedor.
