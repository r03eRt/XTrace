# Quickstart — Source SDK + Primer Crawler (spec 002)

> Cómo ejecutar el crawler en local (desarrollo). El adapter real de xvideos permanece
> **deshabilitado** hasta la revisión legal/ToS/robots del humano (SEC-002); todo lo demás
> funciona con el mock adapter, **sin red** (FR-003, SC-001). Validado end-to-end el
> 2026-08-16 (PR-033: 50/50 vídeos indexados, 0 fallos, INCREMENTAL sin duplicados).

## Prerrequisitos

- `uv` (Python 3.11) y Docker (Supabase local con pgvector, la misma instancia del spike).
- Variables de entorno (mismas que el spike):
  `XTRACE_CRAWLER_SUPABASE_URL`, `XTRACE_CRAWLER_SUPABASE_SERVICE_ROLE_KEY`
  (service_role, solo servidor). El DSN de Postgres usa `SUPABASE_DB_URL` o el default local
  `postgresql://postgres:postgres@127.0.0.1:55322/postgres`.

## Setup

```bash
cd services/crawler
uv sync --locked            # deps + xtrace_spike editable (ADR-0011)
# Supabase local (desde la raíz del repo):
supabase start              # aplica la migración <ts>_source_sdk_crawler.sql
supabase db reset           # + seed.sql: registra las fuentes mock/xvideos (PR-038)
supabase test db            # pgTAP: esquema + RLS
```

## Registrar fuentes (automático con `supabase db reset` — seed.sql)

Las dos fuentes de desarrollo se registran **automáticamente** al ejecutar
`supabase db reset` (o `pnpm supabase:reset`): el seed `supabase/seed.sql`
inserta `mock` (`enabled=true`, sin red — FR-003) y `xvideos`
(`enabled=false`, gate SEC-002) de forma **idempotente**
(`on conflict (name) do nothing`, DATA-001). No hace falta insertarlas a mano.

Verificación:

```sql
select name, enabled from public.sources order by name;
-- mock    | t
-- xvideos | f
```

El SQL manual de abajo queda solo como **referencia** de los manifiestos que
siembra el seed y para la **habilitación** de xvideos (ver "Habilitar xvideos",
que requiere la revisión legal humana — SEC-002):

```sql
-- mock: habilitado para desarrollo (sin red, FR-003)
insert into public.sources (name, adapter, manifest, enabled) values (
  'mock', 'mock',
  '{"source":"mock","access_method":"json",
    "assets_accessed":["storyboard","thumbnail","preview"],
    "robots_reviewed":true,"terms_reviewed":true,
    "rate_limit":{"min_interval_ms":0,"max_rps":1000.0},
    "review_date":"2026-08-15"}', true)
on conflict (name) do nothing;

-- xvideos: DESHABILITADO hasta la revisión legal humana (SEC-002)
insert into public.sources (name, adapter, manifest, enabled) values (
  'xvideos', 'xvideos',
  '{"source":"xvideos","access_method":"html",
    "assets_accessed":["storyboard","thumbnail"],
    "robots_reviewed":false,"terms_reviewed":false,
    "rate_limit":{"min_interval_ms":2000,"max_rps":0.5},
    "review_date":null}', false)
on conflict (name) do nothing;
```

## Uso (mock adapter, sin red)

```bash
uv run xtrace-crawler sources --json          # fuentes + manifiestos

# BACKFILL: encola DISCOVER (y el resto del pipeline por jobs)
uv run xtrace-crawler backfill --source mock --limit 20

# Worker: despacha jobs con FOR UPDATE SKIP LOCKED (sin Redis)
uv run xtrace-crawler run-worker --once
uv run xtrace-crawler run-worker --concurrency 2     # en bucle

# Estado de la operación
uv run xtrace-crawler stats --json           # 50/50 indexed esperado con limit 20

# INCREMENTAL (solo IDs nuevos; no duplica — SC-003)
uv run xtrace-crawler backfill --source mock --limit 20 --incremental
uv run xtrace-crawler run-worker --once

# Disponibilidad de vídeos (marca unavailable/removed)
uv run xtrace-crawler check-availability --source mock --limit 10
uv run xtrace-crawler run-worker --once
```

## Rate limits (D5)

```bash
XTRACE_CRAWLER_RATE_XVIDEOS_MIN_INTERVAL_MS=2000 \
XTRACE_CRAWLER_RATE_XVIDEOS_MAX_RPS=0.5 \
uv run xtrace-crawler backfill --source xvideos --limit 10   # rechazado: gate SEC-002
```

## Validación de calidad

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy xtrace_crawler
uv run pytest          # unit + integration (mock, sin red) — 280 tests
```

## Habilitar xvideos (requiere revisión legal humana — SEC-002)

1. Revisar ToS/robots de xvideos (responsabilidad del humano) y documentarlo.
2. Actualizar el manifest con la revisión y habilitar:
   ```sql
   update public.sources
      set manifest = jsonb_set(jsonb_set(manifest, '{robots_reviewed}', 'true'),
                               '{terms_reviewed}', 'true')
   where name = 'xvideos';
   update public.sources
      set manifest = jsonb_set(manifest, '{review_date}', '"2026-MM-DD"')
   where name = 'xvideos';
   update public.sources set enabled = true where name = 'xvideos';
   ```
3. Backfill acotado manual y verificación de SC-002/003/005/006:
   `uv run xtrace-crawler backfill --source xvideos --limit N && uv run xtrace-crawler run-worker --once`
