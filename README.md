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
- **Fase 1 — Visual Search Spike**: **`IMPLEMENTED`** (2026-08-15). Pipeline validado con
  el dataset real del operador (43 vídeos): **SC-001 Top-5 95,6%** y **SC-002 FPR 0%**
  (umbral 0.8). Diseño en `specs/001-visual-search-spike/` + ADR-0003…0008.
- **Fase 2 — Source SDK + Primer Crawler**: **`IMPLEMENTED`** (2026-08-16). Contrato
  `SourceAdapter` + entidad normalizada `VideoSource` + cola de jobs en Postgres
  (`FOR UPDATE SKIP LOCKED`, sin Redis) + `XvideosAdapter` real. **Validación real
  completada con xvideos** (ver sección *Validación real* abajo). Diseño en
  `specs/002-source-sdk-crawler/` + ADR-0009…0011.
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

## Validación real (xvideos)

> Pruebas del operador completadas el **2026-08-16** con datos reales de xvideos.com,
> tras la revisión legal/ToS/robots y la aprobación humana de habilitación (SEC-002).
> El adaptador real permanece **deshabilitado por defecto** (`sources.enabled=false`).

**Qué se validó** (bucle completo `captura → crawl de sección → índice → búsqueda`):

| Métrica | Resultado |
| --- | --- |
| Corpus del tag `/tags/buttfucking` | **104 vídeos `indexed`** (267/267 jobs `done`, 239+ frames) |
| Embbeddings | SigLIP real (`openclip-ViT-B-16-SigLIP-webli`, D=768, CPU local) |
| Rate limits | 398 requests con ~16 min de espera medida; **0 violaciones** |
| Descargas | Solo thumbnails del CDN; **0 vídeos completos** (SC-006) |
| Búsqueda con captura real | **Top-1 exacto: score 1.0 (visual 1.0, phash 1.0)**, 2 frames, timestamp aproximado correcto |
| Capturas del operador (4) | Vídeos correctos recuperados entre los primeros resultados |
| Hallazgos corregidos | 6 bugs reales (selectores HTML, slugs, paginación de tags, timestamps de galería, CDN `thumbs-gcore`, extra `siglip`) — PR-042…PR-053 |

**Cómo reproducirlo** (desde `main`, Supabase local arriba):

```bash
cd services/crawler
uv sync --extra siglip                       # una vez: torch + open-clip

# 1) Registrar la fuente (seed automático con `supabase db reset`) y habilitarla:
#    manifest con robots_reviewed/terms_reviewed/review_date + enabled=true
#    (aprobación humana; ver specs/002-source-sdk-crawler/quickstart.md)

# 2) Crawl acotado por tag/categoría con embeddings reales:
XTRACE_CRAWLER_EMBEDDINGS=siglip uv run xtrace-crawler backfill \
  --source xvideos --section /tags/buttfucking --limit 50 --max-videos 200
XTRACE_CRAWLER_EMBEDDINGS=siglip uv run xtrace-crawler run-worker --once
uv run xtrace-crawler stats --json

# 3) Mantener fresco (solo IDs nuevos, sin duplicados):
XTRACE_CRAWLER_EMBEDDINGS=siglip uv run xtrace-crawler backfill \
  --source xvideos --section /tags/buttfucking --max-videos 200 --incremental
XTRACE_CRAWLER_EMBEDDINGS=siglip uv run xtrace-crawler run-worker --once

# 4) Buscar con una captura (borrada inmediatamente tras procesar, FR-018):
cd ../search-spike
SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres \
  uv run xtrace-spike search --image /ruta/a/captura.jpg --provider siglip --top-k 10
```

Notas:

- Los **timestamps** de los frames derivados de galerías de thumbnails son
  **aproximados** (`posición/máx_galería × duración`, clamp a duración) — precisión del
  orden de decenas de segundos, no exacta de escena.
- Los límites de la fuente son: página ≈ 27 vídeos, `--limit` ≥ tamaño de página,
  rate limit 2 000 ms/0,5 rps (manifest), `--max-videos` como cota global del backfill.
- **Escala**: indexar xvideos completo (12–14 M vídeos) no es viable (~1 año de crawl a
  0,5 rps y ~200–400 GB de embeddings); la app está diseñada para **corpus acotados por
  tag/categoría** con frescura INCREMENTAL. Umbral de migración a vector DB dedicada
  (~1 M vídeos): documentado en `docs/STATUS.md` (plan de coste) y ADR-0004.

---

## Roadmap de alto nivel

1. **Fase 0 — Arquitectura/spec** (docs, ADRs, schemas, contracts) — **hecho**.
2. **Fase 1 — Visual search spike** (dataset local → frames → pHash → SigLIP → pgvector →
   image query → benchmark) — **hecho** (18 PRs + FIX-phash; SC-001/SC-002 superadas).
3. **Fase 2 — Source SDK + primer crawler** (contrato `SourceAdapter`, `VideoSource`, mock,
   jobs en Postgres, `XvideosAdapter` real) — **hecho** (PR-019…PR-053; validación real con
   xvideos, spec 002 `IMPLEMENTED`).
4. **Fase 3 — Index pipeline** (media worker, dedupe, embedding worker, status, retry) —
   parcialmente cubierto en Fase 2; restante según `tasks.md`.
5. **Fase 4 — Search API** (imagen, resultados, ranking).
6. **Fase 5 — Frontend** (home, upload, resultados, detalle).
7. **Fase 6 — Video query** (clip, multi-frame, consistencia temporal).
8. **Fase 7 — Multi-source** (adapters adicionales).
9. **Fase 8 — Admin** · **Fase 9 — Hardening** (seguridad, rate limit, observabilidad,
   compliance).

> La puerta de decisión del spike (**SC-001: Top-5 ≥ 80%**) está **superada** (95,6%);
> la validación con fuente real confirma la estrategia de crawling acotado.

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
- `specs/001-visual-search-spike/` — qué/por qué del spike (**IMPLEMENTED**): spec, plan,
  data-model, contracts, tasks, quickstart.
- `specs/002-source-sdk-crawler/` — Fase 2 (**IMPLEMENTED**): spec, plan, data-model,
  contracts, tasks, **quickstart** (cómo crawlear y buscar).
- `docs/adr/` — ADR-0003…0011 (Python service, pgvector/HNSW, pHash+embeddings, media
  temporal, abstracciones, CLI, SourceAdapter, cola de jobs Postgres, spike como
  dependencia editable).
- `docs/architecture/visual-search-spike.md` — diagramas del spike.
- `docs/handoffs/` — handoffs de cada PR (trazabilidad completa).
- `docs/USAGE.md` — prompts de arranque para agentes.

---

## Empezar

```bash
# App Next.js (skeleton)
pnpm install && pnpm dev

# Servicio Python del spike (Fase 1)
cd services/search-spike && uv sync && uv run xtrace-spike --help

# Servicio crawler (Fase 2) — pruebas con xvideos: ver sección "Validación real"
cd services/crawler && uv sync --extra siglip && uv run xtrace-crawler --help
```

> Primer paso de cualquier agente al abrir el repo: leer `AGENTS.md`, la constitución y
> `docs/STATUS.md`, y cargar las skills aplicables antes de tocar archivos.
