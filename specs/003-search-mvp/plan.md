# Implementation Plan: MVP de Búsqueda — API REST + Frontend Mínimo (XTrace)

**Branch**: `feature/003-search-mvp` | **Date**: 2026-08-16 | **Spec**:
[`spec.md`](./spec.md)

**Input**: Feature specification from `specs/003-search-mvp/spec.md` (`APPROVED`,
2026-08-16, decisiones D1..D5).

> Este plan define **cómo** se implementa la spec aprobada. No altera requisitos.
> Decisiones registradas como ADRs en `docs/adr/`.

## Summary

Construir el **MVP de búsqueda usable** de XTrace: un **servicio REST** (`services/api/`,
paquete `xtrace_api`, **FastAPI**) que **reutiliza el pipeline del spike** (`xtrace_spike`
como dependencia editable, patrón ADR-0011) para exponer 4 endpoints (D1): **`POST
/search`** (subida de imagen → resultados rankeados con el **mismo contrato JSON de la CLI
`search`**), **`GET /health`**, **`GET /stats`** y **`GET /videos/{id}`** (ficha con
metadatos, fuente y enlace original; 404). La validación de media (fichero regular, ≤ 10 MB,
firma MIME por cabecera) y el **borrado inmediato** reutilizan `security.py` del spike
(FR-002/003, ASSUMPTION-6). El servicio se ejecuta **solo en local** (bind `127.0.0.1`, sin
auth; SEC-001/D3) y **no se despliega** (D4).

El **frontend mínimo** (D2) es **una página en el skeleton Next.js** (`/buscar`, español):
subir captura → ver resultados con título, fuente, score y timestamp y enlace a la URL
original (FR-009/010). Llama a la API desde el cliente con la base URL por env
`NEXT_PUBLIC_XTRACE_API_URL` (default `http://127.0.0.1:8000`). El **E2E WebdriverIO
obligatorio** (constitución §6, SC-005) testea la página en CI **sin API real** (stub del
`fetch` con `browser.mock` + fixture sintético).

**Sin cambios de esquema** (DATA-001): se reutilizan `searches` (analítica sin media con TTL
configurable por cleanup de `created_at`, FR-012), `videos` y `frames` tal cual; el corpus es
el **índice real actual** (D5: 104 vídeos web `indexed` del tag `buttfucking` + 43 del dataset
local del spike), sin reindexar (FR-013/DATA-003). El escalado (`halfvec`, vector DB dedicada)
sigue **diferido** (ADR-0004; no es de esta fase).

## Technical Context

**Language/Version**: Python 3.11 (mismo toolchain que spike/crawler: `uv` + `ruff` + `mypy`
+ `pytest`) para el nuevo servicio; TypeScript/Next.js 16 (App Router, React 19, zod) para el
frontend del skeleton (solo se **añade** una página; no se reestructura).

**Primary Dependencies**:
- API: `fastapi` (framework HTTP, habilitado ahora — ADR-0008 lo difirió), `uvicorn`
  (servidor ASGI local), `python-multipart` (parsing de subida), `pydantic-settings`
  (config por env, mismo patrón que spike/crawler).
- Reutilización del spike: `xtrace_spike` como **dependencia de camino editable**
  (`[tool.uv.sources]`, ADR-0011): `ImageSearch`, `rank_candidates`,
  `validate_query_image`/`QueryMediaContext` (security.py), `PgVectorStore`,
  `FakeEmbeddingProvider`/`SiglipLocalProvider`, `PgRepo` — **sin modificar el spike**
  (ADR-0012).
- Frontend: **sin librerías nuevas**: `fetch` nativo (cliente), zod para validar el contrato
  de respuesta (ya en el skeleton). Tailwind/shadcn **no** están en el skeleton
  (`package.json` solo tiene Next/React/Supabase/zod); la UI usa CSS módulo o estilos
  inline/globals.css existentes — sin añadir dependencias (constitución §9).
- Tests API: `pytest` + `httpx` (TestClient de FastAPI). Tests frontend: Vitest + Testing
  Library (existentes) y WebdriverIO (existente).

**Storage**: Supabase PostgreSQL local (Docker) — mismas tablas `videos`, `frames`,
`searches` de las fases 1-2, **sin migraciones nuevas**. Acceso con el mismo `PgRepo`
(`SUPABASE_DB_URL` o default local `127.0.0.1:55322`). La media de consulta **nunca** se
persiste (SEC-005, ASSUMPTION-6).

**Testing**: `pytest` (unit + integration con FastAPI TestClient; integration DB con
skipif sin BD, mismo patrón que crawler) para el servicio; Vitest/Testing Library para la
página; **WebdriverIO** (smoke suite) para el E2E en CI sin API real. `pgTAP` no aplica
(no hay migraciones nuevas).

**Target Platform**: Ejecución local (uvicorn + navegador) en Linux/macOS + Docker; Supabase
local. Sin despliegue de la API (D4); el frontend usa el Preview automático de Vercel del PR.

**Project Type**: Tercer servicio Python en monorepo (web-service FastAPI), junto a
`services/search-spike/` y `services/crawler/`; el skeleton Next.js se toca solo para añadir
la página de búsqueda.

**Performance Goals**: Búsqueda vía API **< 3 s p95** (SC-004, **objetivo medido y
reportado**, no garantía — embeddings SigLIP en CPU local miden 7-11 s/consulta en el spike).
La API mide y registra `processing_ms` por búsqueda (FR-012/SC-004).

**Constraints**: Coste dev ~0 € (CPU local, Supabase local). **Sin auth y sin bind público**
(SEC-001). Sin migraciones ni tablas nuevas (DATA-001). Media de consulta borrada
inmediatamente (SEC-003). Sin reindexar el corpus (FR-013). Sin añadir dependencias JS
(constitución §9). RLS deny-by-default intacta (SEC-004).

**Scale/Scope**: Operador único local; corpus real de la fase (D5: 147 vídeos, ~90k frames
máx). Sin concurrencia alta: cada petición `POST /search` se ejecuta en su propio thread
(threadpool de FastAPI) y usa `asyncio.run` para la cadena async del spike (mismo patrón que
la CLI). Búsquedas concurrentes independientes con su propio `search_id` (edge case de la
spec).

## Constitution Check

_GATE: debe pasar antes de implementar._

- **Spec-first** ✔ Spec `APPROVED` (2026-08-16, "Especificación aprobada") antes de este plan.
- **Aprobación humana** ✔ Registrada con decisiones D1..D5.
- **Trazabilidad** ✔ Cada componente mapea FR/SEC/DATA/NFR/UX/SC (ver *Requirements
  coverage*); los tests indicarán el requisito que validan.
- **PRs aislados** ✔ El roadmap (`tasks.md`, fase siguiente) descompone en PRs pequeños, sin
  `XL`.
- **Multiagente** ✔ Plan pensado para orquestador + implementador + revisor independiente:
  `allowed_paths` por tarea, handoffs obligatorios.
- **Testing test-first** ✔ Mapeo validación→HTTP, TTL, paridad y contrato con tests primero;
  E2E obligatorio en la smoke suite (SC-005).
- **Fronteras seguridad** ✔ `service_role` solo en el servicio Python (servidor); RLS
  deny-by-default **intacta** (sin políticas nuevas, sin grants a anon/authenticated); API
  bind a `127.0.0.1` (SEC-001); media nunca persistida ni logueada (SEC-005); CORS restringido
  a allowlist local (default `http://localhost:3000`).
- **Preview por PR (constitución §4)** ✔ El frontend (app web, página nueva) se despliega en
  el **Preview automático de Vercel del PR** (D4; `vercel.json` ya lo permite para PRs); la
  **API no se despliega** (servicio local sin artefacto web — interpretación registrada en la
  fase 2, constitución §4 aplica a la app web).

Sin violaciones que justificar → *Complexity Tracking* vacío. Nota: el **tercer** servicio
Python dispara el criterio de ADR-0011 ("si un tercer servicio necesita lo mismo, reevaluar
extraer paquete compartido"); la reevaluación se registra en **ADR-0012** (se mantiene la
dependencia editable; extracción diferida con trigger explícito).

## Project Structure

### Documentation (this feature)

```text
specs/003-search-mvp/
├── spec.md            # Qué/por qué (APPROVED)
├── plan.md            # Este archivo (cómo)
├── data-model.md      # Nota: reutiliza el esquema existente (sin migraciones)
├── contracts/         # Contratos REST + frontend + invariantes
├── quickstart.md      # Cómo arrancar API + frontend y probar con una captura
└── tasks.md           # Roadmap de PRs (creado por task-planning)
```

### Source Code (repository root)

```text
services/
├── search-spike/                 # EXISTENTE — reutilizado como dep editable (intocado)
├── crawler/                      # EXISTENTE — no se toca en esta feature
└── api/                          # NUEVO — API REST de búsqueda (FastAPI, ADR-0012)
    ├── pyproject.toml            # xtrace-api; dep editable → ../search-spike (ADR-0011)
    ├── README.md
    ├── Dockerfile                # (opcional, paridad con spike/crawler)
    ├── xtrace_api/
    │   ├── __init__.py
    │   ├── main.py               # app FastAPI: lifespan (TTL), CORS, exception handlers,
    │   │                         #   routers (search/stats/videos/health)
    │   ├── config.py             # pydantic-settings: DSN, work_root, top_k, min_score,
    │   │                         #   proveedor embeddings, TTL, CORS (SEC-006)
    │   ├── schemas.py            # modelos pydantic del contrato REST (paridad CLI §1)
    │   ├── media.py              # subida → temporal seguro (0600) + mapeo validación→HTTP
    │   │                         #   (413/415/400) reutilizando xtrace_spike.security
    │   ├── search_service.py     # orquesta la cadena del spike (ImageSearch + ranking +
    │   │                         #   refs) — MISMA cadena que la CLI (FR-005)
    │   ├── analytics.py          # insert en searches (FR-012) + TTL configurable (cleanup)
    │   ├── deps.py               # DI: store/provider/backend por petición (paridad CLI)
    │   └── routers/
    │       ├── search.py         # POST /search (multipart)
    │       ├── stats.py          # GET /stats (mismos campos que CLI stats, FR-007)
    │       └── videos.py         # GET /videos/{id} (ficha + 404, FR-008)
    └── tests/
        ├── unit/                 # media→HTTP, schemas, analytics/TTL, search_service (fakes)
        ├── integration/          # TestClient + Supabase local (skipif sin BD): /search real,
        │                         #   stats, ficha, paridad API-CLI (SC-001)
        └── fixtures/             # imagen sintética (PNG 1×1) para TestClient

src/                              # skeleton Next.js (modificación MÍNIMA y aditiva)
├── app/buscar/page.tsx           # página única de búsqueda (D2; la home no se toca)
├── features/search/              # componente cliente de la página (upload + resultados)
│   └── buscar-page.tsx           #   estados: idle | loading | results | error (UX-002)
├── lib/
│   ├── env.ts                    # + NEXT_PUBLIC_XTRACE_API_URL (default http://127.0.0.1:8000)
│   └── api/
│       ├── schemas.ts            # zod del contrato de respuesta (paridad FR-004)
│       └── xtrace.ts             # cliente fetch (multipart → API base por env)
└── app/globals.css               # (sin cambios o estilos mínimos de la página)

tests/
├── unit/buscar-page.test.tsx     # Vitest + Testing Library (fetch mockeado)
└── e2e/
    ├── specs/search.smoke.e2e.ts  # WDIO: upload → resultados vía browser.mock (sin API real)
    └── fixtures/
        ├── search-response.json   # fixture sintético del contrato (sin media real)
        └── query.png              # imagen sintética 1×1 (sin contenido real)
```

**Structure Decision**: Monorepo. Tercer servicio Python en `services/api/` (FastAPI),
aislado del skeleton Next.js, que **reutiliza** `xtrace_spike` como dependencia editable
(ADR-0011/0012) y no toca `services/crawler/`. El frontend vive en el skeleton existente
(`src/app/buscar/**` + `src/lib/api/**`) de forma **aditiva**: la home actual y sus tests
(unit + smoke) permanecen intactos. La DB se comparte vía `supabase/migrations` existentes
(sin nuevas).

## Data model (resumen; detalle en `data-model.md`)

- **Sin migraciones nuevas** (DATA-001). Se reutilizan las tablas existentes de las fases
  1-2:
  - `videos` — leída por la **ficha** (`GET /videos/{id}`, FR-008: `local_ref`, `title`,
    `page_url`, `status`, `duration_ms`, `frame_count`, `tags`, `published_at`,
    `thumbnail_url`, `excluded`) y por el enriquecimiento de resultados de `/search`
    (`title`, `page_url`, FR-004 MAY).
  - `frames` — leída por el ANN (via `PgVectorStore.ann_search`) y por la evidencia pHash
    (`PgRepo.get_frame_phashes`), sin cambios.
  - `searches` — **registro analítico sin media** (FR-012): la API inserta una fila por
    búsqueda aceptada (`id = search_id`, `search_type='image'`, `processing_ms`,
    `results_count`). **TTL configurable sin cambio de esquema**: cleanup periódico
    (lifespan de FastAPI) que borra filas con `created_at < now() - TTL` (`env
    XTRACE_API_SEARCHES_TTL_DAYS`, default 30; intervalo de cleanup configurable).
- Corpus: **índice real actual** (D5) — 104 vídeos web `indexed` (tag `buttfucking`) + 43 del
  dataset local del spike; sin reindexar (FR-013/DATA-003).
- RLS: deny-by-default intacta; `service_role` solo en el servicio Python (SEC-004). pgTAP
  existente (fases 1-2) no cambia.

## Contracts (detalle en `contracts/README.md`)

**API REST** (base local `http://127.0.0.1:8000`, D1):

| Método | Path | Request | Response |
| --- | --- | --- | --- |
| `POST` | `/search` | multipart: `image` (JPEG/PNG/WebP ≤ 10 MB) + form opcional `top_k` (10), `min_score` (0.0) | `200` SearchResponse (paridad CLI §1) |
| `GET` | `/health` | — | `200` `{"status":"ok", ...}` |
| `GET` | `/stats` | — | `200` `{"videos","frames","vectors","backend","embedding_provider"}` (coherente con CLI stats) |
| `GET` | `/videos/{id}` | — | `200` VideoCard · `404` si no existe · `400` si id no es UUID |

**SearchResponse** (reutiliza el JSON de la CLI `search`, spec 001 contracts §1; FR-004):
`search_id` (uuid), `processing_ms` (int), `results[]` con `video_id`, `local_ref` (null
posible), `match_score`, `matching_frames`, `match_timestamp_ms` (**null** si no hay
timestamp fiable), `evidence.visual`, `evidence.phash`; **MAY** amplía cada resultado con
`title` y `page_url` (metadatos de visualización, nullable) sin cambiar los campos
existentes — el frontend los usa para FR-009/010.

**Errores estructurados** (FR-011, mensajes en español — UX-001): cuerpo
`{"error": "<mensaje>", "error_type": "<tipo-máquina>"}` + código HTTP: `400`
(solicitud/media inválida, parte de fichero ausente, nombre vacío, contenido ilegible, UUID
malformado), `404` (vídeo inexistente), `413` (> 10 MB), `415` (firma MIME no soportada),
`503` (índice/BD no disponible, mensaje claro), `500` (fallo interno).

**Frontend** (D2): página `/buscar` en el skeleton; flujo *subir imagen → resultados* con
título, fuente, score y timestamp y enlace "Ver original" (`page_url`) o referencia local
(`local_ref`) cuando no hay URL (UX-003, edge cases). Llamada cliente:
`fetch(`${NEXT_PUBLIC_XTRACE_API_URL}/search`, {method:'POST', body: FormData})` con timeout
(AbortController, 60 s) para no colgarse ante 5xx. Sin auth.

**Invariantes** (detalle en `contracts/README.md` §6): media de consulta **nunca persistida**
ni logueada; borrado inmediato garantizado (éxito o fallo); **paridad API-CLI** (misma cadena
y mismo contrato; SC-001 con procedimiento de comparación); sin auth y solo local; RLS
intacta; sin migraciones; errores en español.

## Security strategy

- **Exposición**: uvicorn bind **`127.0.0.1`** por defecto (SEC-001/D3); el quickstart y la
  config lo fijan; se documenta que exponer la API (0.0.0.0) queda prohibido hasta cerrar
  compliance (ASSUMPTION-2). Sin auth en esta fase (D3).
- **Validación en servidor** (SEC-002): reutiliza `xtrace_spike.security.validate_query_image`
  (fichero regular, ≤ 10 MB, firma MIME por magic bytes — no extensión ni Content-Type) +
  `open_query_image` (decodificación forzada → contenido corrupto = 400).
- **Borrado inmediato garantizado** (SEC-003, FR-003): la subida se guarda en un **temporal
  seguro** (`mkstemp`, 0600, en `work_root` gitignored/configurable) y el procesado usa
  `QueryMediaContext` (borrado en `finally`); además el handler borra en `finally` el
  temporal de subida (la media **rechazada** por validación también se borra — en la API el
  fichero es nuestro, a diferencia de la CLI donde el original del operador no se toca). Un
  fallo de borrado se registra como warning sin enmascarar el resultado (edge case spec).
- **Sin almacenamiento ni logs de media** (SEC-005): los logs de búsqueda llevan solo
  `search_id`, `processing_ms`, `results_count`, `status` — nunca rutas ni nombres de la
  imagen subida; la analítica (`searches`) no contiene media.
- **BD**: `service_role`/DSN de servidor solo en el proceso Python (`PgRepo`); **RLS
  deny-by-default intacta** — no se añaden políticas ni grants (SEC-004); el frontend no
  accede a la BD (llama solo a la API).
- **CORS**: allowlist configurable `XTRACE_API_CORS_ORIGINS` (default
  `http://localhost:3000`); los orígenes de Vercel Preview se añaden por env del operador si
  se quiere probar el preview contra la API local.
- **Configuración por env** (SEC-006): `SUPABASE_DB_URL` (ya existente),
  `XTRACE_EMBEDDING_PROVIDER` (fake|siglip, ya existente), `XTRACE_API_WORK_ROOT`,
  `XTRACE_API_SEARCHES_TTL_DAYS`, `XTRACE_API_CORS_ORIGINS`; sin secretos en el repo.
- **Límite de tamaño antes de decodificar**: la subida se limita por streaming a
  `MAX_QUERY_IMAGE_BYTES + 1` al volcarla a temporal (413 sin procesar), defensa contra
  bombas de tamaño; sin decompression bomb nueva (las imágenes de consulta se abren con PIL;
  si se necesita, límite de píxeles como el del crawler — fuera de alcance aquí).

## Testing strategy

- **Unit (pytest, sin red/DB/torch)**: `media.py` (mapeo validación→HTTP: 413 por tamaño,
  415 por firma, 400 por parte ausente/nombre vacío/contenido ilegible), `schemas.py`
  (contrato de respuesta y errores), `analytics.py` (SQL de TTL: borra solo filas viejas;
  límites), `search_service.py` con `InMemoryVectorStore` + `FakeEmbeddingProvider`
  (determinista, ADR-0007 — misma pareja que los tests de la CLI).
- **Integration (pytest, FastAPI TestClient)**: `POST /search` end-to-end contra el índice
  en memoria y contra **Supabase local** (skipif sin BD, mismo patrón que crawler/spike):
  resultados, `search_id` único por búsqueda, fila en `searches` (FR-012), borrado de la
  media (SC-003: no quedan ficheros en `work_root`), `GET /stats` y `GET /videos/{id}`
  (200/400/404).
- **Paridad API-CLI (SC-001)**: test que ejecuta la **misma imagen** por la cadena CLI
  (`search` de `xtrace_spike`, vía Typer CliRunner o invocando las mismas funciones) y por
  `POST /search`, sobre el **mismo índice** (in-memory en CI; pg local cuando hay BD), y
  compara `video_id`, orden y `match_score` (con redondeo estable). SC-001 exige ≥ 5
  imágenes representativas: el test usa un subconjunto determinista de fixtures.
- **Frontend unit (Vitest + Testing Library)**: la página renderiza los estados
  idle/loading/results/error con `fetch` mockeado; resultados ordenados con score/timestamp
  y enlace (UX-003); error en español (UX-001); cancelación sin estados colgados (edge case).
- **E2E (WebdriverIO, Chrome headless, smoke suite)**: `search.smoke.e2e.ts` — abrir
  `/buscar`, subir `tests/e2e/fixtures/query.png` (PNG sintético 1×1, sin contenido real),
  **stub del `fetch` con `browser.mock('**/search', {method:'POST'})` respondiendo el
  fixture `search-response.json`** (sin API real en CI), y verificar: resultados visibles con
  título, fuente, score, timestamp y enlace (SC-005); error claro en español con fixture de
  4xx; feedback de carga. Selectores estables por `data-testid` (paridad con la smoke actual).
- **DB (pgTAP)**: N/A — sin migraciones nuevas; los tests pgTAP de las fases 1-2 permanecen
  intactos (`pnpm test:db` sigue verde sin cambios).
- Tests marcan el requisito que validan (trazabilidad, constitución §3/§6).

## Deployment / CI strategy

- **Local-first** (D3/D4): la API se ejecuta con uvicorn en local y **no se despliega**; el
  frontend (página nueva) sale en el **Preview automático de Vercel del PR** (constitución
  §4; `vercel.json` actual ya permite PRs). En el Preview, la página apuntará por defecto a
  `http://127.0.0.1:8000` (la API local del operador) o al origen que el operador configure
  por env del preview.
- **Nuevo workflow `python-api-quality`** (aditivo, mismo patrón que `python-quality` y
  `python-crawler-quality`): disparo ante cambios en `services/api/**`,
  `services/search-spike/**` (dep editable) y el propio workflow; pasos: `uv sync --locked`
  → `ruff check` → `ruff format --check` → `mypy xtrace_api` (misma política que el crawler:
  xtrace_spike no publica `py.typed`, mypy solo sobre el paquete propio) → `pytest`
  (TestClient; tests de integración DB con skipif sin BD; `FakeEmbeddingProvider`, sin
  torch). Permisos mínimos del job (contenido de solo lectura, paridad PR-037).
- **Frontend**: lo cubren los workflows **existentes** `quality.yml` (format/lint/typecheck/
  unit/build — la página nueva se valida con Vitest) y `e2e.yml` (levanta la app y ejecuta la
  **smoke suite**, donde vive el nuevo spec E2E; sin cambios en e2e.yml). La home y sus tests
  no se tocan.
- Gate por PR: `ruff && mypy && pytest` (API) + `pnpm verify` (JS, incluye el E2E smoke de la
  página) verde; sin merge sin CI verde y aprobación humana.
- El API real contra el índice real (SigLIP) se valida **manualmente en local** por el
  operador (quickstart), nunca en CI.

## Observability

- **Logs estructurados** de cada búsqueda: `search_id`, `processing_ms`, `results_count`,
  `status`; **sin media** (SEC-005: sin rutas/nombres de la imagen subida). Errores con
  `error_type` estable y mensaje en español (UX-001).
- **`GET /health`**: estado del servicio (responde siempre que el proceso vive; no depende de
  la BD — FR-006/edge "índice no disponible").
- **`GET /stats`**: `videos`, `frames`, `vectors`, `backend`, `embedding_provider` —
  coherentes con la CLI `stats` (FR-007).
- **Analítica `searches`** con TTL configurable (FR-012): volumen de búsquedas consultable
  por SQL en Supabase local (opcional para el operador).
- **SC-004 (latencia)**: `processing_ms` se registra por búsqueda; la validación manual del
  operador (captura real → Top-5, SC-002) reporta p95 en el handoff de cierre, sin bloquear
  la fase (objetivo, no garantía — spec).

## Requirements coverage (trazabilidad)

| Requisito | Cubierto por |
| --- | --- |
| FR-001 | `routers/search.py` + `search_service.py` (reutiliza `xtrace_spike.search.image_search`, ADR-0012) |
| FR-002 | `media.py` (reutiliza `xtrace_spike.security.validate_query_image`) |
| FR-003 | `media.py` + `QueryMediaContext` (try/finally) + cleanup en handler |
| FR-004 | `schemas.py` (SearchResponse) + `contracts/README.md` §1 (paridad CLI, MAY title/page_url) |
| FR-005 | `search_service.py` (misma cadena que la CLI) + test de paridad (SC-001) |
| FR-006 | `GET /health` (routers) |
| FR-007 | `routers/stats.py` (mismos campos que CLI stats) |
| FR-008 | `routers/videos.py` (ficha + 400/404) |
| FR-009 | `src/app/buscar/page.tsx` + `src/features/search/buscar-page.tsx` |
| FR-010 | `schemas.py` (`page_url` por resultado) + UI con enlace/ref local |
| FR-011 | `schemas.py` (Error) + exception handlers en `main.py` (400/404/413/415/5xx, español) |
| FR-012 | `analytics.py` (insert `searches` + TTL configurable por env) |
| FR-013 | `deps.py` (PgVectorStore contra el índice real, sin reindexar) |
| SEC-001 | bind 127.0.0.1 (config/main) + quickstart; API sin deploy (D4) |
| SEC-002 | `media.py` (validación en servidor) |
| SEC-003 | `media.py` + `QueryMediaContext` (borrado garantizado, warning sin enmascarar) |
| SEC-004 | `PgRepo` con credenciales de servidor; RLS deny-by-default intacta (sin cambios) |
| SEC-005 | temporales borrados; logs y `searches` sin media ni nombres de fichero |
| SEC-006 | `config.py` (pydantic-settings, env) + `.env.example` (solo `NEXT_PUBLIC_XTRACE_API_URL`; DSN ya existente) |
| DATA-001 | `data-model.md` + `analytics.py` (reutiliza `searches`; sin migraciones) |
| DATA-002 | `search_service.py` (mismo índice y ranking que la CLI) |
| DATA-003 | `deps.py` (corpus real D5, sin reindexar) |
| NFR-001 | CPU local + Supabase local + sin servicios de pago |
| NFR-002 | `search_service.py` mide `processing_ms`; SC-004 reportado en validación manual |
| NFR-003 | `pyproject.toml` (dep editable a `xtrace_spike`) + ADR-0012 |
| NFR-004 | `quickstart.md` (arranque mínimo documentado) |
| UX-001 | mensajes de error de API y frontend en español |
| UX-002 | estado `loading` con feedback visible en la página |
| UX-003 | resultados ordenados por score, score+timestamp visibles, enlace identificable o ref local |
| SC-001 | `tests/integration/test_parity_cli_api.py` (≥ 5 imágenes, mismo índice) |
| SC-002 | validación manual del operador (quickstart: captura real del corpus → Top-5) |
| SC-003 | tests que verifican `work_root` vacío tras búsqueda (éxito y error) |
| SC-004 | `processing_ms` logueado y reportado (objetivo, no garantía) |
| SC-005 | `tests/e2e/specs/search.smoke.e2e.ts` (smoke suite de `e2e.yml`, stub sin API) |
| SC-006 | tests unit de `media.py` + integration (4xx sin ejecutar búsqueda) |

## Risks (plan)

- **Paridad API-CLI**: si la API desvía la cadena de búsqueda (top_k, umbral, proveedor,
  backend), los resultados divergen de la CLI. Mitigación: `search_service.py` ejecuta
  exactamente `ImageSearch` + `rank_candidates` con los mismos defaults (top_k=10,
  min_score=0.0, pesos DEFAULT_WEIGHTS) y el mismo `build_backend`; SC-001 con test de
  paridad en CI.
- **Latencia embeddings CPU (7-11 s medidos)**: SC-004 (< 3 s p95) puede no cumplirse en
  local; se mide y reporta sin bloquear la fase (spec). Mitigación de diseño: `POST /search`
  como handler **sync** (FastAPI lo ejecuta en threadpool) para no bloquear el event loop;
  timeout del frontend de 60 s con abort.
- **Media sensible**: validación en servidor, temporal 0600 en directorio gitignored, borrado
  inmediato (SEC-002/003), sin logs de media (SEC-005). La analítica no guarda nada de la
  imagen.
- **Exposición accidental**: bind por defecto `127.0.0.1`; revisión de seguridad en el PR;
  CORS allowlist restringida (SEC-001).
- **Acoplamiento frontend-API**: contrato fijado en `contracts/README.md` (FR-004 como
  frontera estable) + validación zod en el cliente; el frontend tolera campos opcionales
  (`match_timestamp_ms: null`, sin `page_url`).
- **TTL sin migración**: el cleanup por `created_at` no es "expiración real" de filas; se
  documenta como limitación aceptada (DATA-001 exige no tocar esquema).
- **Concurrencia SigLIP**: el provider carga el modelo una vez (lazy) y lo comparte entre
  peticiones; embeddings concurrentes en CPU pueden contenerse. Aceptable para operador único
  local; documentado.
- **Contenido adulto en CI/E2E**: fixtures sintéticos (PNG 1×1 + JSON sin media real);
  ninguna captura real se commitea (paridad con fases 1-2, `.gitignore` `capturas-test/`).

## ADRs (creados en `docs/adr/`)

- `0012` Servicio FastAPI de búsqueda (`services/api/`) reutilizando `xtrace_spike` como
  dependencia editable (patrón ADR-0011; reevaluación del trigger de ADR-0011 para el
  tercer servicio; extracción de `packages/xtrace-core` diferida).

Relacionados (sin cambios): `0003` (servicio Python), `0004` (pgvector/HNSW; halfvec y
escalado siguen **diferidos** de esta fase), `0007` (abstracciones VectorStore/
EmbeddingProvider), `0008` (CLI del spike; FastAPI ahora habilitado para el MVP),
`0011` (dep editable).

## Bloqueos

Ninguno. Spec sin ambigüedades (D1..D5 resueltas). Notas para task-planning: la **validación
real con capturas del corpus** (SC-002) es responsabilidad del operador en local; el
**halfvec/escalado** (ADR-0004) no es de esta fase. Próximo paso: `task-planning` →
`tasks.md`.
