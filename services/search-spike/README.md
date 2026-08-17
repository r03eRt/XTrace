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

## Benchmark de muestreo adaptativo (TASK-005-004)

El spike conserva `legacy_fixed` y sus 30 frames por vídeo como referencia. La
política adaptativa se ejecuta de forma explícita (`--sampling adaptive`) y no
se convierte en default por el resultado de una medición aislada.

El benchmark reproducible compara las mismas consultas conocidas contra dos
ficheros de observaciones (`dense` y `adaptive`). El sidecar de casos debe
incluir `case_id`, `source` (`local`/`web`), `duration_ms`,
`timestamp_ms`/`truth_timestamp_ms` y `expected_video_ref`:

```bash
uv run xtrace-spike sampling-benchmark \
  --cases benchmark/sidecar.json \
  --dense-results benchmark/dense.json \
  --adaptive-results benchmark/adaptive.json \
  --out benchmark/report.json
```

La orden exige al menos 30 casos positivos únicos, ambas fuentes, los tramos
`<5m`, `5-15m` y `>15m`, y tres positivos por segmento no vacío. Los casos
negativos pueden omitir duración/timestamp y siguen requiriendo observación
pareada exacta, pero no cuentan para esos mínimos. Informa Top-1/Top-5,
error temporal mediano/p95, error normalizado, frames/reducción y resultados
por fuente/tramo, conservando métricas separadas para dense y adaptive en cada
segmento. Los mínimos (`30` casos, `3` por segmento) no se pueden rebajar.
Si falla SC-004..SC-008 imprime `accepted=false` y termina
con código 2; nunca autoriza por sí sola el cambio del default.

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

- `mypy` + `pytest` sobre Python 3.11. Es aditivo: no altera la pipeline JS
  (`quality.yml`).
