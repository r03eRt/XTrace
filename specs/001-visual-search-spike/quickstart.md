# Quickstart — Visual Search Spike

Cómo ejecutar el spike en local (validado 2026-08-15 con el dataset real del operador).

## Requisitos

- Python 3.11, `uv`, `ffmpeg` en el PATH.
- Docker Desktop corriendo + Supabase CLI (local en puertos `55321-55328`) **o** un
  proyecto Supabase Free con `pgvector`.
- (Opcional, embeddings reales) extra `siglip` (torch 2.2.2 + open_clip). Sin él se usa
  `FakeEmbeddingProvider` (determinista, sin Torch).

## Setup

```bash
# 1) Supabase local (migraciones con pgvector + tablas del spike)
pnpm supabase:start
pnpm supabase:reset            # aplica migrations/ y seed

# 2) Servicio Python (uv, reproducible)
cd services/search-spike
uv sync --locked               # entorno base
uv sync --extra siglip         # opcional: embeddings reales SigLIP (CPU local)

# 3) Backend pgvector (Supabase local)
export SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres
```

## Uso

```bash
# Indexar un dataset local (43 vídeos del operador, p. ej.) — 30 frames/vídeo es el punto dulce
xtrace-spike index --dataset /ruta/al/dataset --frames-per-video 30 --provider siglip

# Buscar por imagen (umbral de match recomendado: 0.8)
xtrace-spike search --image /ruta/captura.jpg --provider siglip --min-score 0.8

# Ejecutar el benchmark (~210 casos) y obtener el informe (puerta SC-001)
xtrace-spike benchmark --cases /ruta/casos --provider siglip --min-score 0.8

# Métricas del índice
xtrace-spike stats

# Excluir un vídeo de los resultados (FR-014)
xtrace-spike exclude --video <video_id>
```

Salida siempre JSON por stdout; logs por stderr.

## Resultado medido (2026-08-15, dataset real del operador, SigLIP v1, pgvector/HNSW)

- **SC-001: Top-5 ≥ 80% → CUMPLE: 95,6%** (Top-1 93,9%) con umbral 0.8.
- **SC-002: FPR ≤ 10% → CUMPLE: 0%** con umbral 0.8.
- **30 frames/vídeo = punto dulce** (10 viable: 94,4% · 60 no aporta).
- Latencia SC-003 < 3 s; throughput de embedding ~0.75-4 fps en CPU (GPU serverless
  opcional vía `EmbeddingProvider`, ADR-0007).

## Tests

```bash
# Python: calidad + unit + integration
cd services/search-spike
uv run ruff check && uv run ruff format --check
uv run mypy xtrace_spike tests
uv run pytest                       # FakeEmbeddingProvider por defecto

# DB (pgTAP)
cd ../.. && pnpm test:db

# JS (skeleton, intacto)
pnpm verify
```

## Docker

```bash
docker compose up   # levanta el servicio del spike + ffmpeg
```
