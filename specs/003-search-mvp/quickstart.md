# Quickstart — MVP de Búsqueda (spec 003)

> Cómo arrancar la **API REST** (FastAPI) + el **frontend** (página `/buscar`) en local y
> probar la búsqueda con una captura contra el **índice real** (D5: 104 vídeos web
> `indexed` del tag `buttfucking` + 43 del dataset local del spike). Solo local, sin auth,
> sin exponer nada (SEC-001/D3).

## Prerrequisitos

- `uv` (Python 3.11), Node ≥ 22 + `pnpm`, Docker + Supabase CLI (la misma instancia local
  de las fases 1-2, con el índice ya indexado — el corpus de la fase no se reindexa, FR-013).
- Variables de entorno (mismas que el spike/crawler): `SUPABASE_DB_URL` (o default local
  `postgresql://postgres:postgres@127.0.0.1:55322/postgres`) y, opcional,
  `XTRACE_EMBEDDING_PROVIDER=fake|siglip` (default `fake`; **`siglip`** para búsqueda real
  contra el índice real).

## 1. Base de datos

```bash
supabase start        # aplica las migraciones de las fases 1-2 (sin migraciones nuevas)
supabase db reset     # opcional: seed + estado limpio
```

Verifica el índice real (debe reflejar el corpus de la fase):

```bash
cd services/search-spike && uv run xtrace-spike stats
# {"videos": 147, "frames": ..., "vectors": ..., "backend": "postgres", ...}
```

## 2. API (servicio `services/api/`)

```bash
cd services/api
uv sync --locked                 # deps + xtrace_spike editable (ADR-0011/0012)
uv sync --extra siglip           # SOLO si quieres embeddings reales (torch; opcional)
export XTRACE_EMBEDDING_PROVIDER=siglip   # fake (default) para dev rápido
uv run uvicorn xtrace_api.main:app --host 127.0.0.1 --port 8000
```

> La API escucha **solo en `127.0.0.1`** (SEC-001). No la expongas (0.0.0.0) — la
> exposición pública espera a cerrar compliance (ASSUMPTION-2, D3/D4).

Comprobación rápida:

```bash
curl -s http://127.0.0.1:8000/health   # {"status":"ok", ...}
curl -s http://127.0.0.1:8000/stats    # videos/frames/vectors/backend/embedding_provider
```

Swagger/docs interactivos: http://127.0.0.1:8000/docs (operador local; sin auth, D3).

## 3. Frontend (skeleton Next.js)

```bash
# desde la raíz del repo
pnpm install
cp .env.example .env.local        # opcional; NEXT_PUBLIC_XTRACE_API_URL ya tiene default
pnpm dev                          # http://localhost:3000
```

- Abre **http://localhost:3000/buscar**: sube una captura → resultados con título, fuente,
  score y timestamp, y enlace "Ver original" cuando la fuente tenga `page_url`.
- La página llama a `NEXT_PUBLIC_XTRACE_API_URL` (default `http://127.0.0.1:8000`) — si tu
  API corre en otro puerto, ajústalo en `.env.local` y reinicia `pnpm dev`.

## 4. Probar la búsqueda con una captura

Por API (curl):

```bash
curl -F "image=@capturas-test/captura.png" http://127.0.0.1:8000/search | python3 -m json.tool
```

Por navegador: sube la misma captura en http://localhost:3000/buscar y compara los
resultados.

> La media de consulta se **borra inmediatamente** tras procesar (FR-003/SEC-003): tras la
> búsqueda no queda nada en disco ni en la DB (SC-003). Las capturas de prueba viven en
> `capturas-test/` (gitignored) — **nunca** se commitean.

### Paridad API-CLI (SC-001)

Misma imagen, mismo índice, misma configuración:

```bash
cd services/search-spike
uv run xtrace-spike search --image ../capturas-test/captura.png --top-k 10 --min-score 0.0 \
  --provider siglip
# vs. la respuesta de POST /search de arriba: mismos video_id, mismo orden, mismos match_score
```

Las diferencias permitidas son solo la extensión MAY de la API (`title`/`page_url` por
resultado). El test automatizado `tests/integration/test_parity_cli_api.py` cubre ≥ 5
imágenes en CI.

## 5. E2E (WebdriverIO) y calidad

```bash
pnpm test:e2e:smoke   # smoke suite: incluye la página /buscar (stub del fetch, sin API real)
pnpm verify           # puertas JS completas (format/lint/typecheck/test/test:db/e2e/build)
```

Calidad del servicio API (gate `python-api-quality`):

```bash
cd services/api
uv run ruff check && uv run ruff format --check
uv run mypy xtrace_api
uv run pytest          # unit + integration (TestClient; los tests de DB saltan sin BD local)
```

## Notas operativas

- **Analítica (FR-012)**: cada búsqueda inserta una fila en `searches` (sin media) con TTL
  configurable (`XTRACE_API_SEARCHES_TTL_DAYS`, default 30).
- **Latencia (SC-004)**: con SigLIP en CPU local la búsqueda puede tardar 7-11 s (objetivo
  < 3 s p95, no garantía); `processing_ms` se reporta en la respuesta y en la analítica.
- **Validación real (SC-002)**: una captura real de un vídeo del corpus debe devolver su
  vídeo en el Top-5 vía API — prueba manual del operador (responsabilidad legal del
  contenido de prueba, spec).
