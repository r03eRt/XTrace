# Quickstart: Validación del muestreo adaptativo

## Prerrequisitos

- Supabase local para pgvector, dataset autorizado y fuente web ya habilitada.
- SigLIP solo para validación real; CI usa el proveedor fake.

## 1. Calidad rápida

```bash
cd services/search-spike
uv run ruff check . && uv run mypy xtrace_spike && uv run pytest
cd ../crawler
uv run ruff check . && uv run mypy xtrace_crawler && uv run pytest
```

## 2. Reindexar local

```bash
cd services/search-spike
SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres \
uv run xtrace-spike index --dataset ../../dataset --sampling adaptive \
  --max-frames 8 --target-interval-seconds 120 --provider siglip
```

Esperado: 1–8 frames por vídeo y repetición sin cambios.

## 3. Reindexar web

```bash
cd services/crawler
XTRACE_CRAWLER_EMBEDDINGS=siglip \
SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres \
uv run xtrace-crawler reindex --source xvideos --limit 104 --sampling adaptive \
  --max-frames 8 --target-interval-seconds 120

XTRACE_CRAWLER_EMBEDDINGS=siglip \
SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres \
uv run xtrace-crawler run-worker --once
```

Esperado: solo assets permitidos, reemplazo completo y máximo 8.

## 4. Caso observado y benchmark

Buscar una captura alrededor de 07:24 de `video.udvlvofc556`: debe conservar el vídeo
correcto y usar el frame realmente más cercano, no 12:38 por falta de cobertura. Después,
comparar adaptativo vs 30 con las mismas consultas y verificar SC-004..SC-008 antes de
cambiar defaults.
