# services/search-spike — Spike de búsqueda visual (XTrace)

Servicio Python del spike de búsqueda visual inversa
(`specs/001-visual-search-spike/spec.md`, `APPROVED`). Paquete `xtrace_spike` con
CLI interna en Typer (FR-017, ADR-0003/0008).

> **Estado (PR-001, bootstrap)**: scaffolding + calidad + CI. La CLI solo expone
> `--help`/`--version`; los comandos de negocio (`index`, `search`, `benchmark`,
> `exclude`, `stats`) llegan en PRs posteriores (ver `contracts/README.md`).

## Requisitos

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## Setup y comandos de desarrollo

```bash
cd services/search-spike
uv sync --locked          # crea .venv e instala el proyecto + dev deps (lock reproducible)
uv run xtrace-spike --help
```

## Calidad (gate del workflow `python-quality`)

```bash
uv run ruff check
uv run ruff format --check
uv run mypy xtrace_spike tests
uv run pytest
```

## Docker

```bash
docker build -t xtrace-spike services/search-spike
docker run --rm xtrace-spike --help
```

O vía Compose (perfil `spike`, no interfiere con `web`):

```bash
docker compose --profile spike up spike
```

## CI

`.github/workflows/python-quality.yml` ejecuta `ruff check` + `ruff format --check`
+ `mypy` + `pytest` sobre Python 3.11. Es aditivo: no altera la pipeline JS
(`quality.yml`).
