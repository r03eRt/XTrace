# Quickstart — Source SDK + Primer Crawler (spec 002)

> Cómo ejecutar el crawler en local (desarrollo). El adapter real de xvideos permanece
> **deshabilitado** hasta la revisión legal/ToS/robots del humano (SEC-002); todo lo demás
> funciona con el mock adapter, sin red.

## Prerrequisitos

- `uv` (Python 3.11) y Docker (Supabase local con pgvector, la misma instancia del spike).
- Variables de entorno (mismas que el spike):
  `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (service_role, solo servidor).

## Setup

```bash
cd services/crawler
uv sync --all-extras        # instala deps + xtrace_spike editable (ADR-0011)
# Supabase local:
supabase start              # aplica la migración <ts>_source_sdk_crawler.sql
supabase test db            # pgTAP: esquema + RLS
```

## Uso (mock adapter, sin red)

```bash
# Listar fuentes registradas y sus manifiestos
uv run xtrace-crawler sources --json

# BACKFILL con el mock (encola DISCOVER + resto del pipeline)
uv run xtrace-crawler backfill --source mock --limit 20

# Ejecutar el worker (despacha jobs con FOR UPDATE SKIP LOCKED)
uv run xtrace-crawler run-worker --once
uv run xtrace-crawler run-worker --concurrency 2     # en bucle

# Estado de la operación
uv run xtrace-crawler stats --json

# INCREMENTAL (solo IDs nuevos/cambiados)
uv run xtrace-crawler backfill --source mock --limit 20 --incremental

# Disponibilidad de vídeos (marca unavailable/removed)
uv run xtrace-crawler check-availability --source mock --limit 10
```

## Rate limits (D5)

```bash
XTRACE_CRAWLER_RATE_XVIDEOS_MIN_INTERVAL_MS=2000 \
XTRACE_CRAWLER_RATE_XVIDEOS_MAX_RPS=0.5 \
uv run xtrace-crawler backfill --source xvideos --limit 10
```

## Validación de calidad

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy xtrace_crawler
uv run pytest          # unit + integration (mock, sin red)
```

## Habilitar xvideos (requiere revisión legal humana)

1. Revisar ToS/robots de xvideos (responsabilidad del humano) y documentarlo.
2. Actualizar `manifest` en `sources` con `robots_reviewed=true`,
   `terms_reviewed=true`, `rate_limit` y `review_date`.
3. `update public.sources set enabled=true where name='xvideos';` (aprobación humana).
4. Backfill acotado manual: `uv run xtrace-crawler backfill --source xvideos --limit N`.
