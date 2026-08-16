# Servicio API de búsqueda de XTrace

Servicio Python (3.11) REST de búsqueda visual de XTrace (FastAPI).
Paquete: `xtrace_api` · spec: `specs/003-search-mvp/` · ADR-0012.

> Estado actual (PR-054): **bootstrap** — scaffolding, toolchain (uv + ruff + mypy +
> pytest), CI y `GET /health` (FR-006). La lógica de búsqueda (POST /search), stats y
> ficha de vídeo llegan en PR-055/056.

## Toolchain

```bash
cd services/api
uv sync            # instala dependencias + crea uv.lock (primera vez)
uv sync --extra siglip   # SOLO si quieres embeddings reales (torch; opcional)
uv run ruff check .
uv run ruff format --check .
uv run mypy xtrace_api
uv run pytest
```

## Arranque local (SEC-001: solo 127.0.0.1)

```bash
cd services/api
uv run uvicorn xtrace_api.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"xtrace-api","version":"0.1.0"}
```

La API escucha **solo en `127.0.0.1`** por defecto (SEC-001, D3). No la expongas
(`0.0.0.0`) — la exposición pública espera a cerrar compliance (ASSUMPTION-2, D4).

## Dependencia editable al spike (ADR-0011/0012)

`xtrace_api` reutiliza el pipeline validado del spike (`xtrace_spike`: pHash,
embeddings, vector store, ranking, seguridad de media) como **dependencia de camino
editable**:

```toml
[tool.uv.sources]
xtrace-spike = { path = "../search-spike", editable = true }
```

El spike permanece **intocado** (solo lectura). Cualquier cambio necesario en él
debe ser un PR propio trazado a la spec 003.

## Configuración (variables de entorno)

Sin secretos en el repositorio; todo se inyecta por env (ver `xtrace_api/config.py`):

| Variable                           | Descripción                                            |
| ---------------------------------- | ------------------------------------------------------ |
| `XTRACE_API_HOST`                  | bind del servidor (default `127.0.0.1`, SEC-001)       |
| `XTRACE_API_PORT`                  | puerto del servidor (default `8000`)                   |
| `SUPABASE_DB_URL`                  | DSN de servidor (convenio spike/crawler; vacío → tests in-memory) |
| `XTRACE_EMBEDDING_PROVIDER`        | `fake` (default) \| `siglip` (convenio del spike)      |
| `XTRACE_API_WORK_ROOT`             | directorio de temporales de media (default `<tempdir>/xtrace-api`) |
| `XTRACE_API_CORS_ORIGINS`          | allowlist CORS como JSON (default `["http://localhost:3000"]`) |

## Docker

El build context debe ser la **raíz del repositorio** (la dependencia editable
vive en `../search-spike`):

```bash
docker build -f services/api/Dockerfile -t xtrace-api .
docker run --rm -p 127.0.0.1:8000:8000 xtrace-api
```

## Seguridad

- Bind `127.0.0.1` por defecto (SEC-001); CORS allowlist restringida por env
  (default solo `http://localhost:3000`).
- Credenciales de BD solo de servidor (`SUPABASE_DB_URL`); RLS deny-by-default
  intacta (SEC-004).
- La media de consulta nunca se persiste ni se loguea (SEC-005; temporales en
  `work_root` — PR-055).
