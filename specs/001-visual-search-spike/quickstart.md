# Quickstart — Visual Search Spike

Cómo ejecutar el spike en local (para implementadores/operador).

## Requisitos

- Python 3.11, `ffmpeg` en el PATH.
- Supabase local (CLI + Docker) **o** un proyecto Supabase Free con `pgvector`.
- (Opcional real) modelo SigLIP vía `open_clip`/`transformers` + Torch (CPU sirve para el
  spike). Para tests/CI se usa `FakeEmbeddingProvider` (sin Torch).

## Setup

```bash
# 1) DB local con migraciones (incluye pgvector + tablas del spike)
pnpm supabase:start
pnpm supabase:reset          # aplica migrations/ y seed

# 2) Servicio Python
cd services/search-spike
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3) Variables (ver .env.example): SUPABASE_DB_URL / service_role para el servicio
cp .env.example .env
```

## Uso

```bash
# Indexar un dataset local
xtrace-spike index --dataset /ruta/al/dataset --frames-per-video 30

# Buscar por imagen
xtrace-spike search --image /ruta/captura.jpg --top-k 10

# Ejecutar el benchmark (~210 casos) y obtener el informe
xtrace-spike benchmark --cases services/search-spike/tests/fixtures/benchmark

# Métricas del índice
xtrace-spike stats
```

## Tests

```bash
# Python (unit + integration) y calidad
cd services/search-spike
ruff check . && ruff format --check .
mypy .
pytest                      # usa FakeEmbeddingProvider por defecto

# DB (pgTAP)
cd ../../ && pnpm test:db
```

## Puerta de éxito

El spike se considera **validado** si el benchmark cumple **SC-001: Top-5 ≥ 80%** en los
casos positivos y **SC-002** en negativas, con latencia reportada (SC-003).

## Docker

```bash
docker compose up   # levanta el servicio del spike + ffmpeg (+ DB si aplica)
```
