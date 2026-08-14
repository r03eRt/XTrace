# XTrace — Adult Visual Video Search Engine

> Motor de **búsqueda visual inversa** para contenido audiovisual adulto **legal**.

XTrace localiza, dentro de su propio **índice visual**, el **vídeo de origen** (y vídeos
visualmente similares, su fuente y un **timestamp aproximado**) a partir de una **imagen,
captura de pantalla, clip corto o — eventualmente — una URL**. No busca por texto:
combina **frames + perceptual hashes (pHash) + visual embeddings (SigLIP) + vector search
(pgvector/HNSW)**.

**El activo del proyecto es el índice visual, no el frontend.**

> **Principio rector — `VALIDATE SEARCH FIRST, SCALE CRAWLING SECOND`.**
> Primero demostrar, de la forma más barata posible, que
> `captura → embedding → índice → vídeo correcto` funciona con 100–3.000 vídeos.
> Solo entonces invertir en crawling masivo.

---

## Estado actual

- **Producto definido**: `docs/PRODUCT_IDEA.md` (descubrimiento cerrado, `SPEC_APPROVED_PLANNED`).
- **Spec 001 — Visual Search Spike**: **`APPROVED`** (2026-08-14). Diseño completo en
  `specs/001-visual-search-spike/` (`spec.md`, `plan.md`, `data-model.md`, `contracts/`,
  `tasks.md`) + ADR-0003…0008 + `docs/architecture/visual-search-spike.md`.
- **Implementación en curso** (Fase 1 · spike): **PR-001** (bootstrap del servicio Python
  - CI) y la **Ola A** (PR-002 `EmbeddingProvider`, PR-003 `VectorStore`, PR-004 `pHash`,
    PR-008 ingest FFmpeg) **completados** y mergeados a `feature/001-visual-search-spike`.
    Siguiente: **Ola B** (PR-005 SigLIP + PR-009 dedupe).
- Detalle vivo (PRs completados/abiertos, blockers, decisiones pendientes, costes):
  **`docs/STATUS.md`**.

---

## Qué resuelve

Localizar el vídeo original a partir de contenido visual aislado — una captura, un frame,
un GIF, un clip recortado, una imagen recomprimida, con watermark o con el color alterado —
cuando no hay texto útil asociado. La ventaja competitiva futura es la **cantidad y calidad
de fuentes, frames, embeddings, ranking y frescura** del índice.

### Actores

| Actor                          | Rol                                                                                                        |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Invitado** (principal en v1) | Busca por imagen/clip/URL, ve resultados, abre ficha, accede a la fuente original. Sin cuenta obligatoria. |
| **Administrador**              | Gestiona fuentes, crawlers, jobs, reindexado, métricas y reports/takedowns. Requiere auth.                 |
| **Crawler / worker** (interno) | discover → metadata → visual assets → frames → hashes → embeddings → índice.                               |

### Flujos principales

- **Imagen**: upload → validación → normalización → pHash → embedding (SigLIP) → ANN →
  frames candidatos → agrupar por vídeo → ranking → resultados + timestamp.
- **Vídeo (clip)**: FFmpeg → frames representativos → embeddings → búsquedas individuales →
  **consistencia temporal** → ranking → vídeo probable + timestamp.

---

## Arquitectura

```mermaid
flowchart LR
    subgraph Fase 1 (spike, en curso)
      CLI[xtrace-spike CLI] --> IDX[Indexing: frames→dedupe→embed]
      CLI --> SRCH[Image search: pHash+embed→ANN→rank]
      CLI --> BENCH[Benchmark: Top-K/latencia]
      Dataset[(Dataset local de vídeos)] --> IDX
      IDX --> DB[(Supabase Postgres + pgvector/HNSW)]
      SRCH --> DB
    end
    subgraph Post-spike (diferido)
      Crawler[Crawler + SourceAdapters]
      API[FastAPI: /search/*]
      Web[Frontend Next.js]
      Admin[Admin]
    end
```

- **Componentes y abstracciones** (ADR-0007): `VectorStore`, `EmbeddingProvider`,
  `ObjectStorage` y `SourceAdapter` para poder cambiar pgvector↔Qdrant, CPU↔GPU↔Modal y
  las fuentes sin tocar el core.
- **Matching doble** (ADR-0005): **pHash** para near-exact (recompresión/resize) y
  **SigLIP2** para similitud semántica (crop, watermark, color, encuadre).
- **No almacenar vídeos completos** (ADR-0006): procesar temporal → frames → embedding →
  borrar (cleanup `try/finally`). La media de consulta se borra inmediatamente.

Ver diagramas detallados en `docs/architecture/`.

---

## Stack y decisiones

| Componente         | Elección                                            | Notas                                             |
| ------------------ | --------------------------------------------------- | ------------------------------------------------- |
| Frontend           | Next.js · TypeScript · React · Tailwind · shadcn/ui | Vercel Hobby. **Diferido** (post-spike).          |
| Backend API        | Python · FastAPI                                    | `/search/*`, admin. **Diferido**.                 |
| Servicio del spike | Python 3.11 · Typer (CLI) · uv                      | `services/search-spike/` (ADR-0003/0008).         |
| Vector store       | Supabase Postgres + **pgvector + HNSW**             | `VectorStore` (ADR-0004). Evaluar `halfvec`.      |
| Visual embeddings  | SigLIP2 (fallback OpenCLIP)                         | `EmbeddingProvider`. Batch.                       |
| Perceptual hash    | pHash (64-bit) + Hamming                            | `imagehash` + Pillow (ADR-0005).                  |
| Media              | FFmpeg / FFprobe                                    | frames, storyboards/sprites, previews.            |
| Crawler            | Python · httpx · selectolax · BeautifulSoup         | `SourceAdapter`. **Post-spike**.                  |
| Colas              | tabla `jobs` en Postgres (`FOR UPDATE SKIP LOCKED`) | **No Redis** en v1.                               |
| GPU                | serverless (Modal) solo con embeddings pendientes   | no GPU 24/7.                                      |
| E2E                | **WebdriverIO** (`.e2e.ts`, Chrome headless)        | Playwright/Cypress prohibidos como framework E2E. |
| Infra objetivo     | Vercel Hobby + Supabase Free + R2 free + local      | dev **0–10 €/mes**; MVP **0–25 €/mes**.           |

Principios: **cheap first · local first · managed when useful · scale when proven ·
measure first · replaceable infrastructure**.

---

## Roadmap de alto nivel

1. **Fase 0 — Arquitectura/spec** (docs, ADRs, schemas, contracts) — **hecho**.
2. **Fase 1 — Visual search spike** (dataset local → frames → pHash → SigLIP → pgvector →
   image query → benchmark) — **en curso** (18 PRs, ver `tasks.md`).
3. **Fase 2 — Source SDK** (`SourceAdapter`, normalización, mock source, fixtures).
4. **Fase 3 — First crawler** (discover, metadata, visual assets, jobs).
5. **Fase 4 — Index pipeline** (media worker, dedupe, embedding worker, status, retry).
6. **Fase 5 — Search API** (imagen, resultados, ranking).
7. **Fase 6 — Frontend** (home, upload, resultados, detalle).
8. **Fase 7 — Video query** (clip, multi-frame, consistencia temporal).
9. **Fase 8 — Multi-source** (adapters adicionales).
10. **Fase 9 — Admin** · **Fase 10 — Hardening** (seguridad, rate limit, observabilidad,
    compliance).

> La puerta de decisión del spike es **SC-001: Top-5 ≥ 80%** (con latencia reportada,
> SC-003) en el benchmark de ~210 casos. Si no se cumple, **no** se escala el crawling.

---

## Estructura del repositorio

```text
AGENTS.md                       Contrato universal para agentes
.specify/                       Constitución, plantillas y scripts Spec Kit
.agents/skills/                 Skills Spec Kit + skills de dominio
specs/                          Specs por feature (000-platform-foundation, 001-visual-search-spike)
services/search-spike/          Servicio Python del spike (xtrace_spike + CLI Typer)
src/                            app Next.js (skeleton; no se toca en el spike)
tests/                          unit / e2e (WebdriverIO) / fixtures / helpers
supabase/                       config, migraciones, seeds, tests (pgTAP)
docs/                           adr / architecture / handoffs / runbooks + PRODUCT_IDEA + STATUS
scripts/                        verificaciones de workflow, specs, contratos, migraciones
.github/                        Workflows CI (quality JS, python-quality, e2e, security, …)
```

---

## Calidad

**Skeleton JS** (Next.js):

```bash
pnpm format:check && pnpm lint && pnpm typecheck \
  && pnpm test && pnpm test:db && pnpm test:e2e && pnpm build
pnpm verify   # todo lo anterior en orden
```

**Servicio Python del spike** (`services/search-spike/`):

```bash
cd services/search-spike
uv sync --locked            # entorno reproducible
uv run ruff check           # lint
uv run ruff format --check  # formato
uv run mypy xtrace_spike tests   # typecheck
uv run pytest               # tests
uv run xtrace-spike --help  # CLI
```

Puertas por PR del spike: `ruff` + `mypy` + `pytest` (+ pgTAP con `pnpm test:db` y build
cuando aplique). La pipeline JS permanece verde (el spike no toca el app Next.js).

---

## Flujo Spec-Driven y multiagente

```text
constitution → specify → clarify → checklist → APROBACIÓN HUMANA
→ plan → tasks → analyze → implementación → converge → revisión → PR
```

- Fuente de verdad: `AGENTS.md` + `.specify/memory/constitution.md` + skills + specs.
- Cada feature: su spec (`APPROVED` con la frase **`Especificación aprobada`**), su rama
  (`feature/NNN-*` / `feature/NNN-PR-0XX-slug`) y su PR aislado. Sin push directo a `main`.
- La **revisión la hace un agente distinto** al implementador (idealmente otro proveedor).
- Cada tarea deja un **handoff** en `docs/handoffs/`. El orquestador mantiene `tasks.md` y
  `docs/STATUS.md`.

---

## Límites del producto y compliance

- Contenido adulto **legal**. Solo fuentes **públicas, legalmente accesibles o autorizadas**
  (candidatas en `docs/PRODUCT_IDEA.md`); cada `SourceAdapter` documenta acceso, robots,
  ToS y rate limit.
- **Prohibido**: reconocimiento facial / identificación biométrica de personas; saltarse
  auth, paywalls, CAPTCHA, DRM o anti-bot; acceder a contenido privado o robado.
- Búsquedas de usuario **temporales** (TTL configurable); nunca usarlas como dataset sin
  consentimiento.
- **Sin lanzamiento público** hasta cerrar las tareas de compliance (18+, privacidad,
  ToS, takedown, retención, requisitos España/UE). El spike técnico no se bloquea por ello.
- Autenticación solo para admin (Supabase Auth). Monetización fuera del alcance inicial.

---

## Documentación clave

- `docs/PRODUCT_IDEA.md` — requisitos y decisiones de producto.
- `docs/STATUS.md` — estado vivo (fase, PRs, blockers, pendientes, costes).
- `specs/001-visual-search-spike/spec.md` — qué/por qué del spike (**APPROVED**).
- `specs/001-visual-search-spike/plan.md` · `data-model.md` · `contracts/` · `tasks.md`.
- `docs/adr/` — ADR-0003…0008 (Python service, pgvector/HNSW, pHash+embeddings, media
  temporal, abstracciones, CLI).
- `docs/architecture/visual-search-spike.md` — diagramas del spike.
- `docs/USAGE.md` — prompts de arranque para agentes.

---

## Empezar

```bash
# App Next.js (skeleton)
pnpm install && pnpm dev

# Servicio Python del spike
cd services/search-spike && uv sync && uv run xtrace-spike --help
```

> Primer paso de cualquier agente al abrir el repo: leer `AGENTS.md`, la constitución y
> `docs/STATUS.md`, y cargar las skills aplicables antes de tocar archivos.
