# Quickstart — Adapter redgifs.com (spec 008)

> Cómo desarrollar/probar el adapter `redgifs` en local. El manifest está
> **revisado en modo prueba** (SEC-002 · Decisión D4, 2026-08-19), pero la
> fuente se registra `enabled=false` en el seed: la habilitación efectiva
> (backfill real) es una acción humana explícita en BD. **Validado
> end-to-end el 2026-08-20 (PR-069)**: backfill real acotado sobre
> `/niches/homemade` (50 vídeos) y `/niches/real-cellphone-clips` (48
> vídeos), 0 duplicados en INCREMENTAL, 0 descargas de mp4.

## Prerrequisitos

- `uv` (Python 3.11) y Docker (Supabase local con pgvector).
- Variables de entorno del crawler (mismas que el resto de fuentes):
  `SUPABASE_DB_URL` o el default local
  `postgresql://postgres:postgres@127.0.0.1:55322/postgres`.

## Setup

```bash
cd services/crawler
uv sync --locked
# Desde la raíz del repo:
supabase start
supabase db reset      # + seed.sql: registra `redgifs` (enabled=false, manifest D4)
```

Verificación:

```bash
uv run xtrace-crawler sources --json
# → incluye {"name":"redgifs","adapter":"redgifs","enabled":false,
#    "manifest":{"access_method":"api","assets_accessed":["thumbnail"],
#    "robots_reviewed":true,"terms_reviewed":true,
#    "review_date":"2026-08-19", ...}}
```

## Desarrollo sin red (fixtures)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy xtrace_crawler
uv run pytest tests/unit/test_redgifs_adapter.py -q       # 46 tests, sin red
uv run pytest tests/unit -q                                # suite completa
```

Los tests de integración con Supabase local (fixtures + `MockTransport`, sin
red real) viven en `tests/integration/test_pipeline.py`
(`test_redgifs_backfill_indexes_videos_without_timestamp`,
`test_redgifs_incremental_does_not_duplicate_videos_nor_frames`,
`test_redgifs_persistent_failure_does_not_block_other_sources`):

```bash
# ⚠️ DESTRUCTIVO: trunca jobs/videos/sources de la BD apuntada. Solo contra
# una BD desechable — NUNCA una con datos reales que quieras conservar
# (p. ej. el backfill real de PR-069 en tu Supabase local de desarrollo).
XTRACE_CRAWLER_ALLOW_DB_RESET=1 uv run pytest tests/integration/test_pipeline.py -k redgifs -q
```

## Habilitar redgifs (acción humana explícita — SEC-002)

El manifest **ya** declara `robots_reviewed=true`/`terms_reviewed=true`/
`review_date="2026-08-19"` (D4); solo falta `enabled=true`:

```bash
supabase db query "update public.sources set enabled=true where name='redgifs';"
```

(La sub-herramienta `supabase db execute --sql` no existe en esta versión del
CLI; usar `supabase db query "<SQL>"`.)

## Backfill real acotado (validado en PR-069)

```bash
uv run xtrace-crawler backfill --source redgifs --section /niches/homemade --max-videos 50
uv run xtrace-crawler backfill --source redgifs --section /niches/real-cellphone-clips --max-videos 50

# Worker: procesa la cola (repetir hasta 0 pendientes; PR-069 usó un bucle
# `run-worker --once` porque el rate limit del manifest, 2 s/petición,
# reparte el trabajo en varias pasadas):
uv run xtrace-crawler run-worker --once --concurrency 3

uv run xtrace-crawler stats --json    # videos_by_status.indexed, jobs_by_status

# INCREMENTAL (solo IDs nuevos; no duplica — SC-003):
uv run xtrace-crawler backfill --source redgifs --section /niches/homemade --max-videos 50 --incremental
uv run xtrace-crawler backfill --source redgifs --section /niches/real-cellphone-clips --max-videos 50 --incremental
uv run xtrace-crawler run-worker --once --concurrency 3
```

Rate limit conservador por defecto (2000 ms / 0.5 rps); overrides por
entorno:

```bash
XTRACE_CRAWLER_RATE_REDGIFS_MIN_INTERVAL_MS=2000 \
XTRACE_CRAWLER_RATE_REDGIFS_MAX_RPS=0.5 \
uv run xtrace-crawler backfill --source redgifs --section /niches/homemade --max-videos 50
```

## Verificación en BD (evidencia real, PR-069)

```sql
-- 0 duplicados por (source_id, external_id):
select external_id, count(*) from public.videos v join public.sources s on s.id=v.source_id
where s.name='redgifs' group by external_id having count(*) > 1;   -- → 0 filas

-- Frames sin timestamp (thumbnail+poster, sin storyboard):
select source_kind, timestamp_ms, count(*) from public.frames f
join public.videos v on v.id=f.video_id join public.sources s on s.id=v.source_id
where s.name='redgifs' group by source_kind, timestamp_ms;
-- → source_kind='video_frame', timestamp_ms=NULL

-- Embeddings/pHash consultables:
select count(*), count(embedding), count(phash) from public.frames f
join public.videos v on v.id=f.video_id join public.sources s on s.id=v.source_id
where s.name='redgifs';
```

## Deshabilitar de nuevo (opcional, reversible)

```bash
supabase db query "update public.sources set enabled=false where name='redgifs';"
```
