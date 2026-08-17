# Idea del producto — XTrace

> Rellenado durante la **entrevista de descubrimiento** (skill `project-discovery`).
> Fuente: prompt maestro aportado por el usuario (2026-08-14).

## Estado

`SPEC_APPROVED_PLANNED` (spec 001 APPROVED; plan+ADRs+tasks listos para implementar)

## Idea inicial

**XTrace — Adult Visual Video Search Engine.** Motor de búsqueda visual inversa
especializado en contenido audiovisual adulto **legal**. El usuario sube una imagen,
una captura, un clip corto o (eventualmente) una URL y XTrace localiza dentro de su
propio índice el vídeo de origen, vídeos visualmente similares, la fuente indexada y
un timestamp aproximado de la escena.

Funciona con **frames + perceptual hashes + visual embeddings + vector search**, no
con búsqueda textual. El activo principal es **el índice visual**, no el frontend.

Decisión central: **VALIDATE SEARCH FIRST, SCALE CRAWLING SECOND.** Primero un spike
barato (100-3.000 vídeos) que demuestre `captura -> embedding -> índice -> vídeo correcto`.

---

## Decisiones confirmadas

### Problema y valor
- Localizar el vídeo de origen a partir de contenido visual aislado (captura, frame,
  GIF, clip, recorte, recompresión, watermark) cuando no hay texto útil.
- Ventaja futura = cantidad/calidad de fuentes, frames, embeddings, ranking y frescura.

### Usuarios
- **Invitado** (principal en v1): sin cuenta obligatoria. Busca por imagen/clip/URL,
  ve resultados, abre ficha, accede a la fuente original, ve relacionados.
- **Administrador** (requiere auth): gestiona fuentes, crawlers, jobs, reindexado,
  métricas, reports/takedowns, salud del sistema.
- **Crawler/worker**: actor interno (discover -> metadata -> assets -> frames -> hashes ->
  embeddings -> índice).

### Flujos principales
- **Imagen**: upload -> validación -> normalización -> pHash -> embedding (SigLIP) -> ANN ->
  frames candidatos -> agrupar por vídeo -> ranking -> resultados -> detalle + timestamp.
- **Vídeo**: clip -> FFmpeg -> frames representativos -> embeddings -> búsquedas -> coherencia
  temporal -> agrupar -> ranking -> vídeo probable + timestamp. La consistencia temporal
  eleva el confidence.

### Datos (modelo mínimo a diseñar, no todas tablas en v1)
`sources`, `videos` (UNIQUE(source_id, external_id); estados discovered/pending/
indexing/indexed/failed/unavailable/removed), `visual_assets`, `frames` (timestamp_ms,
phash, embedding), `jobs` (DISCOVER/FETCH_METADATA/INDEX_VIDEO/EXTRACT_FRAMES/
GENERATE_EMBEDDINGS/REINDEX/CHECK_AVAILABILITY), `searches`, `reports`.

### Principios técnicos (confirmados por el usuario)
- **Coste mínimo**: dev 0-10 EUR/mes; MVP 0-25 EUR/mes. Free tiers y proceso local primero.
- **Stack**: Frontend Next.js/TS/React/Tailwind/shadcn (Vercel Hobby). Backend Python/
  FastAPI. Crawler Python (httpx/selectolax/BeautifulSoup). Media worker FFmpeg. DB
  Supabase Postgres + **pgvector + HNSW**. Storage Cloudflare R2 solo cuando haga falta.
  Colas = tabla `jobs` en Postgres (`FOR UPDATE SKIP LOCKED`), **no Redis** en v1. GPU
  serverless (Modal) solo cuando haya embeddings pendientes.
- **Abstracciones desde el principio**: `VectorStore`, `ObjectStorage`,
  `EmbeddingProvider`, `SourceAdapter`.
- **Matching doble**: pHash (near-exact) + SigLIP2 (semántico). Sin entrenar modelo propio en v1.
- **No almacenar vídeos completos permanentemente**; procesar temporal -> frames ->
  embedding -> borrar. Priorizar storyboards/sprites y galerías de thumbnails frente a
  procesar vídeo.
- **E2E: WebdriverIO obligatorio** (no Playwright como framework E2E; Playwright solo
  evaluable como herramienta interna del crawler si una fuente lo exige).
- **Idempotencia, retries con backoff, cleanup en try/finally, adapters aislados.**

### Límites del producto (confirmados)
- Contenido adulto **legal**. Solo fuentes públicas / legalmente accesibles / autorizadas.
- **Prohibido**: reconocimiento facial / identificación biométrica de personas; saltarse
  auth, paywalls, CAPTCHA, DRM, anti-bot; acceder a contenido privado/robado.
- Búsquedas de usuario **temporales** con TTL configurable; no usarlas como dataset sin
  consentimiento.
- Auth solo para admin (Supabase Auth). Monetización fuera de alcance pero no bloqueada.

### Éxito v1
- Dataset de validación del spike: ~3 fuentes, ~3.000 vídeos, ~30 frames/vídeo, ~90k
  embeddings (objetivos de validación, no límites arquitectónicos). Para el catálogo
  global multi-proveedor, la política adoptada es un índice base de **8 frames/vídeo** y
  refinamiento bajo demanda sobre candidatos (ADR-0013).
- Búsqueda por imagen encuentra el vídeo entre los primeros resultados, muestra fuente y
  timestamp aproximado. Búsqueda por vídeo aprovecha consistencia temporal.
- Añadir una fuente nueva no requiere tocar el core (solo un `SourceAdapter`).
- Objetivo de latencia de búsqueda **< 3 s** (meta a medir, no garantía).

---

## Supuestos (ASSUMPTION - estado tras Ronda 2)

- `ASSUMPTION-1` -> **CONFIRMADO**: El primer entregable es **solo el spike de búsqueda
  visual con dataset local propio, sin crawler real**.
- `ASSUMPTION-2` -> **CONFIRMADO**: El spike/MVP **no será públicamente accesible** hasta
  cerrar las tareas de compliance (18+, privacidad, ToS, takedown).
- `ASSUMPTION-3` -> **CONFIRMADO**: Jurisdicción objetivo = **España/UE**.
- `ASSUMPTION-4` -> **CONFIRMADO**: Umbral de éxito del spike = **Top-5 >= 80%** en el
  set de benchmark (capturas exactas, comprimidas, recortadas, con watermark,
  redimensionadas, color alterado y clips cortos).
- `ASSUMPTION-5` -> **ACTUALIZADO**: El usuario **sí aporta fuentes reales concretas**
  (pendientes de recibir). Aun así se construirán fixtures/mock (algunos derivados de
  URLs que el usuario proporcione) y la habilitación de cada fuente real pasará por
  revisión legal/ToS por adapter. Los adapters de fuentes reales son **fase post-spike**.
- `ASSUMPTION-6` -> **CONFIRMADO**: Media de consulta = **borrado inmediato** tras
  procesar la búsqueda.

## Fuentes aportadas por el usuario (candidatas - POST-SPIKE)

> Estas fuentes son **candidatas** y **no se crawlean en el spike**. Cada una requiere
> revisión legal/ToS/robots antes de habilitar su adapter, y solo se accederá a recursos
> públicos permitidos (sin saltarse auth, paywalls, CAPTCHA, DRM ni anti-bot). Se usarán
> además para generar fixtures capturados de forma permitida.

| # | Dominio | Tipo de contenido | Notas |
|---|---------|-------------------|-------|
| 1 | erome.com | gifs / vídeos / galerías | Revisar ToS/robots por adapter |
| 2 | xvideos.com | vídeos | Probable storyboard/sprite + thumbnails |
| 3 | xhamster.com | vídeos | Probable storyboard/sprite + thumbnails |
| 4 | redgifs.com | gifs cortos | Posible API/feed; previews animados |
| 5 | pornhub.com | vídeos / gifs | Probable storyboard/sprite + thumbnails |

Por cada fuente, su futuro `SourceAdapter` documentará: source, access method,
assets accessed, robots reviewed, terms reviewed, rate limit, review date.

## Recomendación (cierre de descubrimiento)

Descubrimiento suficiente para avanzar. Siguiente paso segun `AGENTS.md`:
**`spec-authoring`** para redactar la primera spec del **spike de búsqueda visual**
(`specs/001-visual-search-spike`), que definirá el qué/por qué del pipeline
`dataset local -> frames -> pHash -> SigLIP -> pgvector -> query imagen -> resultado`
con la puerta de éxito **Top-5 >= 80%**. Tras `clarify` y la **aprobación humana**
(`Especificación aprobada`) se pasará a `technical-planning` (arquitectura + ADRs) y
`tasks` (roadmap de PRs). Las fuentes reales y el crawler quedan para fases posteriores.

## Contradicciones detectadas

- `CONTRADICCION-1`: El prompt maestro (sección 100) pide **producir ya** arquitectura
  completa, ADRs, schema y roadmap de PRs. El contrato del skeleton (`AGENTS.md` par.2 y la
  skill `project-discovery`) **prohíbe** elegir arquitectura durante el descubrimiento y
  exige **aprobación humana de la spec** (`Especificación aprobada`) antes de `plan`/
  arquitectura. **Resolución propuesta**: seguir el flujo por puertas - cerrar
  descubrimiento -> `spec-authoring` -> `clarify` -> aprobación humana -> `technical-planning`
  (arquitectura/ADRs) -> `tasks` (roadmap de PRs). La riqueza técnica del prompt se
  conserva como insumo para esas fases.

---

## Preguntas abiertas - Ronda 2

1. Fuentes del spike (`ASSUMPTION-1/5`).
2. Puerta de compliance vs. acceso público (`ASSUMPTION-2/3`).
3. Umbral de éxito del spike (`ASSUMPTION-4`).
4. TTL de privacidad de la media de consulta (`ASSUMPTION-6`).
5. Alcance del primer entregable (solo spike de búsqueda vs. incluir crawler).
