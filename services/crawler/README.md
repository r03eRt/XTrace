# Servicio crawler de XTrace

Servicio Python (3.11) de ingesta de fuentes web al índice visual de XTrace.
Paquete: `xtrace_crawler` · CLI: `xtrace-crawler` · spec: `specs/002-source-sdk-crawler/`.

> Estado actual (PR-019): **bootstrap** — scaffolding, toolchain (uv + ruff + mypy +
> pytest) y CI. Sin lógica de dominio todavía (adapters, jobs y pipeline llegan en
> PR-020..PR-032).

## Toolchain

```bash
cd services/crawler
uv sync            # instala dependencias + crea uv.lock (primera vez)
uv run ruff check .
uv run ruff format --check .
uv run mypy xtrace_crawler
uv run pytest
uv run xtrace-crawler --help
```

## Dependencia editable al spike (ADR-0011)

`xtrace_crawler` reutiliza el pipeline validado del spike (`xtrace_spike`: pHash,
embeddings, vector store, ranking) como **dependencia de camino editable**:

```toml
[tool.uv.sources]
xtrace-spike = { path = "../search-spike", editable = true }
```

El spike permanece **intocado** (solo lectura). Cualquier cambio necesario en él
debe ser un PR propio trazado a la spec 002.

## Configuración (variables de entorno)

Sin secretos en el repositorio; todo se inyecta por env con prefijo
`XTRACE_CRAWLER_` (ver `xtrace_crawler/config.py`):

| Variable                                   | Descripción                          |
| ------------------------------------------ | ------------------------------------ |
| `XTRACE_CRAWLER_SUPABASE_URL`              | URL del proyecto Supabase (SEC-003)  |
| `XTRACE_CRAWLER_SUPABASE_SERVICE_ROLE_KEY` | clave `service_role` (solo servidor) |
| `XTRACE_CRAWLER_LOG_LEVEL`                 | nivel de log (default `INFO`)        |
| `XTRACE_CRAWLER_REQUEST_TIMEOUT_SECONDS`   | timeout HTTP global (default `30.0`) |

## Docker

El build context debe ser la **raíz del repositorio** (la dependencia editable
vive en `../search-spike`):

```bash
docker build -f services/crawler/Dockerfile -t xtrace-crawler .
docker run --rm xtrace-crawler --help
```

## Referencias

- Spec: `specs/002-source-sdk-crawler/spec.md` (APPROVED) · Plan: `plan.md` · Tareas: `tasks.md`
- ADR-0011: reutilización del spike como dependencia editable
