-- Tests pgTAP del esquema Source SDK + Crawler (PR-025).
-- Ejecuta con: supabase test db (o pnpm test:db) — requiere Supabase local en marcha.
-- Valida: FR-006 (jobs FOR UPDATE SKIP LOCKED), FR-012 (estados de vídeo ampliados),
--         DATA-001 (sources/jobs/videos-web), DATA-002 (tipos de job),
--         DATA-003 (local_ref y external_id coexisten), SEC-003 (RLS deny-by-default),
--         ADR-0010 (índice de despacho), specs/002-source-sdk-crawler/data-model.md.

begin;

select plan(103);

-- ── Tablas (DATA-001) ──────────────────────────────────────────────────────
select has_table('public', 'sources', 'tabla sources existe (DATA-001)');
select has_table('public', 'jobs', 'tabla jobs existe (FR-006/DATA-001)');

-- ── sources: columnas (data-model.md) ──────────────────────────────────────
select has_column('public', 'sources', 'id', 'sources.id (uuid PK)');
select has_column('public', 'sources', 'name', 'sources.name (text UNIQUE)');
select has_column('public', 'sources', 'adapter', 'sources.adapter (registro del adapter)');
select has_column('public', 'sources', 'manifest', 'sources.manifest (jsonb compliance)');
select has_column('public', 'sources', 'enabled', 'sources.enabled (gate SEC-002)');
select has_column('public', 'sources', 'created_at', 'sources.created_at');
select has_column('public', 'sources', 'updated_at', 'sources.updated_at');
select col_type_is('public', 'sources', 'manifest', 'jsonb',
  'sources.manifest es jsonb (data-model.md)');
select col_type_is('public', 'sources', 'enabled', 'boolean',
  'sources.enabled es boolean (data-model.md)');
select col_has_default('public', 'sources', 'enabled',
  'sources.enabled tiene default (SEC-002)');

-- ── jobs: columnas (FR-006 · ADR-0010 · data-model.md) ─────────────────────
select has_column('public', 'jobs', 'id', 'jobs.id (uuid PK)');
select has_column('public', 'jobs', 'job_type', 'jobs.job_type (DATA-002)');
select has_column('public', 'jobs', 'status', 'jobs.status');
select has_column('public', 'jobs', 'source_id', 'jobs.source_id (FK sources)');
select has_column('public', 'jobs', 'video_id', 'jobs.video_id (FK videos)');
select has_column('public', 'jobs', 'payload', 'jobs.payload (jsonb parámetros)');
select has_column('public', 'jobs', 'attempts', 'jobs.attempts (FR-008)');
select has_column('public', 'jobs', 'max_attempts', 'jobs.max_attempts (FR-008)');
select has_column('public', 'jobs', 'not_before', 'jobs.not_before (backoff, ADR-0010)');
select has_column('public', 'jobs', 'locked_by', 'jobs.locked_by (lease)');
select has_column('public', 'jobs', 'locked_at', 'jobs.locked_at (lease)');
select has_column('public', 'jobs', 'error', 'jobs.error');
select has_column('public', 'jobs', 'created_at', 'jobs.created_at');
select has_column('public', 'jobs', 'updated_at', 'jobs.updated_at');
select col_type_is('public', 'jobs', 'payload', 'jsonb',
  'jobs.payload es jsonb (data-model.md)');

-- ── videos: columnas web nuevas (DATA-001/003, no destructiva) ─────────────
select has_column('public', 'videos', 'source_id', 'videos.source_id (FK sources)');
select has_column('public', 'videos', 'external_id', 'videos.external_id');
select has_column('public', 'videos', 'page_url', 'videos.page_url');
select has_column('public', 'videos', 'title', 'videos.title');
select has_column('public', 'videos', 'tags', 'videos.tags (jsonb)');
select has_column('public', 'videos', 'published_at', 'videos.published_at');
select has_column('public', 'videos', 'thumbnail_url', 'videos.thumbnail_url');
select has_column('public', 'videos', 'preview_url', 'videos.preview_url');
select has_column('public', 'videos', 'storyboard_urls', 'videos.storyboard_urls (jsonb)');
select col_type_is('public', 'videos', 'tags', 'jsonb',
  'videos.tags es jsonb (data-model.md)');
select col_type_is('public', 'videos', 'storyboard_urls', 'jsonb',
  'videos.storyboard_urls es jsonb (data-model.md)');
select col_is_null('public', 'videos', 'source_id',
  'videos.source_id NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'external_id',
  'videos.external_id NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'page_url',
  'videos.page_url NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'title',
  'videos.title NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'tags',
  'videos.tags NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'published_at',
  'videos.published_at NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'thumbnail_url',
  'videos.thumbnail_url NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'preview_url',
  'videos.preview_url NULL por defecto (no destructiva)');
select col_is_null('public', 'videos', 'storyboard_urls',
  'videos.storyboard_urls NULL por defecto (no destructiva)');

-- ── sources: fixtures funcionales (el runner conecta como superusuario) ────
select lives_ok(
  $$ insert into public.sources (name, adapter, manifest)
     values ('src-a', 'mock',
             '{"access_method":"html","assets_accessed":["storyboard"],"robots_reviewed":false,"terms_reviewed":false,"rate_limit":{"min_interval_ms":1000},"review_date":null}'::jsonb) $$,
  'insert sources válido (DATA-001)'
);

select is(
  (select enabled from public.sources where name = 'src-a'),
  false,
  'sources.enabled default false (gate SEC-002)'
);

select throws_ok(
  $$ insert into public.sources (name, adapter, manifest) values ('src-a', 'mock', '{}'::jsonb) $$,
  '23505',
  NULL,
  'UNIQUE(name) en sources rechaza nombre duplicado (data-model.md)'
);

-- ── videos: unicidad parcial y coexistencia local/web (DATA-001/003) ───────
select lives_ok(
  $$ insert into public.videos (local_ref, source_id, external_id)
     select 'web/pr025-a', id, 'ext-1' from public.sources where name = 'src-a' $$,
  'insert vídeo web válido (source_id + external_id)'
);

select throws_ok(
  $$ insert into public.videos (local_ref, source_id, external_id)
     select 'web/pr025-a2', id, 'ext-1' from public.sources where name = 'src-a' $$,
  '23505',
  NULL,
  'UNIQUE parcial (source_id, external_id) rechaza duplicado web (DATA-001)'
);

select lives_ok(
  $$ insert into public.videos (local_ref, source_id, external_id)
     select 'web/pr025-b1', id, NULL from public.sources where name = 'src-a' $$,
  'vídeo con external_id NULL válido (el índice parcial no aplica)'
);

select lives_ok(
  $$ insert into public.videos (local_ref, source_id, external_id)
     select 'web/pr025-b2', id, NULL from public.sources where name = 'src-a' $$,
  'varios vídeos con external_id NULL no colisionan (índice parcial)'
);

select lives_ok(
  $$ insert into public.videos (local_ref) values ('local/pr025-1') $$,
  'vídeo local del spike (source_id NULL) sigue siendo válido (DATA-003)'
);

select throws_ok(
  $$ insert into public.videos (local_ref) values ('local/pr025-1') $$,
  '23505',
  NULL,
  'UNIQUE(local_ref) se mantiene para vídeos locales (DATA-003)'
);

-- CHECK de status ampliado con unavailable/removed (FR-012).
select lives_ok(
  $$ insert into public.videos (local_ref, status) values ('web/pr025-u', 'unavailable') $$,
  'status unavailable aceptado (FR-012)'
);

select lives_ok(
  $$ insert into public.videos (local_ref, status) values ('web/pr025-r', 'removed') $$,
  'status removed aceptado (FR-012)'
);

select throws_ok(
  $$ insert into public.videos (local_ref, status) values ('web/pr025-x', 'bogus') $$,
  '23514',
  NULL,
  'CHECK status rechaza estado no contemplado (FR-012)'
);

-- ── jobs: tipos (DATA-002) y estados (ADR-0010) ────────────────────────────
select lives_ok(
  $$ insert into public.jobs (job_type) values ('DISCOVER') $$,
  'job_type DISCOVER válido (DATA-002)'
);
select lives_ok(
  $$ insert into public.jobs (job_type) values ('FETCH_METADATA') $$,
  'job_type FETCH_METADATA válido (DATA-002)'
);
select lives_ok(
  $$ insert into public.jobs (job_type) values ('INDEX_VIDEO') $$,
  'job_type INDEX_VIDEO válido (DATA-002)'
);
select lives_ok(
  $$ insert into public.jobs (job_type) values ('EXTRACT_FRAMES') $$,
  'job_type EXTRACT_FRAMES válido (DATA-002)'
);
select lives_ok(
  $$ insert into public.jobs (job_type) values ('GENERATE_EMBEDDINGS') $$,
  'job_type GENERATE_EMBEDDINGS válido (DATA-002)'
);
select lives_ok(
  $$ insert into public.jobs (job_type) values ('CHECK_AVAILABILITY') $$,
  'job_type CHECK_AVAILABILITY válido (DATA-002)'
);
select lives_ok(
  $$ insert into public.jobs (job_type) values ('REINDEX') $$,
  'job_type REINDEX válido (DATA-002)'
);
select throws_ok(
  $$ insert into public.jobs (job_type) values ('BOGUS') $$,
  '23514',
  NULL,
  'CHECK job_type rechaza tipo desconocido (DATA-002)'
);

select lives_ok(
  $$ insert into public.jobs (job_type, status) values ('DISCOVER', 'pending') $$,
  'status pending válido (ADR-0010)'
);
select lives_ok(
  $$ insert into public.jobs (job_type, status) values ('DISCOVER', 'running') $$,
  'status running válido (ADR-0010)'
);
select lives_ok(
  $$ insert into public.jobs (job_type, status) values ('DISCOVER', 'done') $$,
  'status done válido (ADR-0010)'
);
select lives_ok(
  $$ insert into public.jobs (job_type, status) values ('DISCOVER', 'failed') $$,
  'status failed válido (ADR-0010)'
);
select lives_ok(
  $$ insert into public.jobs (job_type, status) values ('DISCOVER', 'unavailable') $$,
  'status unavailable válido (ADR-0010)'
);
select throws_ok(
  $$ insert into public.jobs (job_type, status) values ('DISCOVER', 'bogus') $$,
  '23514',
  NULL,
  'CHECK status rechaza estado desconocido (ADR-0010)'
);

-- ── jobs: defaults (FR-008 · data-model.md) ────────────────────────────────
select lives_ok(
  $$ insert into public.jobs (job_type, payload) values ('FETCH_METADATA', '{"defaults": true}'::jsonb) $$,
  'insert job mínimo válido (solo job_type + payload marcador)'
);
select is(
  (select status from public.jobs where payload = '{"defaults": true}'::jsonb),
  'pending',
  'jobs.status default pending (ADR-0010)'
);
select is(
  (select attempts from public.jobs where payload = '{"defaults": true}'::jsonb),
  0,
  'jobs.attempts default 0 (FR-008)'
);
select is(
  (select max_attempts from public.jobs where payload = '{"defaults": true}'::jsonb),
  3,
  'jobs.max_attempts default 3 (FR-008)'
);
select is(
  (select payload from public.jobs
    where job_type = 'FETCH_METADATA' and payload = '{}'::jsonb
    limit 1),
  '{}'::jsonb,
  'jobs.payload default {} (data-model.md)'
);

-- ── Índices (data-model.md · ADR-0010) ─────────────────────────────────────
select has_index('public', 'jobs', 'idx_jobs_dispatch',
  'idx_jobs_dispatch existe (despacho SKIP LOCKED, ADR-0010)');
select has_index('public', 'jobs', 'idx_jobs_source',
  'idx_jobs_source existe (filtro por fuente)');
select has_index('public', 'jobs', 'idx_jobs_type',
  'idx_jobs_type existe (filtro por tipo)');
select is(
  (select array_agg(a.attname order by g)::text[]
     from pg_index i
     cross join generate_series(0, i.indnkeyatts - 1) as g
     join pg_attribute a on a.attrelid = i.indrelid and a.attnum = i.indkey[g]
    where i.indexrelid = 'public.idx_jobs_dispatch'::regclass),
  ARRAY['status', 'not_before']::text[],
  'idx_jobs_dispatch indexa (status, not_before) (ADR-0010)'
);
select has_index('public', 'videos', 'idx_videos_source_external',
  'idx_videos_source_external existe (data-model.md)');
select is(
  (select array_agg(a.attname order by g)::text[]
     from pg_index i
     cross join generate_series(0, i.indnkeyatts - 1) as g
     join pg_attribute a on a.attrelid = i.indrelid and a.attnum = i.indkey[g]
    where i.indexrelid = 'public.idx_videos_source_external'::regclass),
  ARRAY['source_id', 'external_id']::text[],
  'idx_videos_source_external indexa (source_id, external_id)'
);
select has_index('public', 'videos', 'idx_videos_status',
  'idx_videos_status del spike se mantiene');

-- ── Constraints únicos (catálogo; DATA-001/003) ────────────────────────────
select is(
  (select count(*)::int from pg_constraint
    where conname = 'uq_sources_name' and contype = 'u'
      and conrelid = 'public.sources'::regclass),
  1,
  'constraint UNIQUE uq_sources_name en sources (data-model.md)'
);
select is(
  (select count(*)::int from pg_constraint
    where conname = 'uq_videos_local_ref' and contype = 'u'
      and conrelid = 'public.videos'::regclass),
  1,
  'UNIQUE(local_ref) de vídeos locales se mantiene (DATA-003)'
);
select is(
  (select i.indisunique and i.indpred is not null
     from pg_index i
    where i.indexrelid = 'public.uq_videos_source_external'::regclass),
  true,
  'uq_videos_source_external: índice único PARCIAL (DATA-001)'
);
select is(
  (select array_agg(a.attname order by g)::text[]
     from pg_index i
     cross join generate_series(0, i.indnkeyatts - 1) as g
     join pg_attribute a on a.attrelid = i.indrelid and a.attnum = i.indkey[g]
    where i.indexrelid = 'public.uq_videos_source_external'::regclass),
  ARRAY['source_id', 'external_id']::text[],
  'uq_videos_source_external indexa (source_id, external_id)'
);

-- ── FKs (data-model.md) ────────────────────────────────────────────────────
select is(
  (select confdeltype = 'n' from pg_constraint where conname = 'fk_videos_source'),
  true,
  'FK videos.source_id → sources(id) ON DELETE SET NULL (data-model.md)'
);
select is(
  (select confdeltype = 'n' from pg_constraint where conname = 'fk_jobs_source'),
  true,
  'FK jobs.source_id → sources(id) ON DELETE SET NULL (data-model.md)'
);
select is(
  (select confdeltype = 'c' from pg_constraint where conname = 'fk_jobs_video'),
  true,
  'FK jobs.video_id → videos(id) ON DELETE CASCADE (data-model.md)'
);

-- ── RLS deny-by-default (SEC-003, paridad con el spike) ────────────────────
select is(
  (select relrowsecurity from pg_class where oid = 'public.sources'::regclass),
  true,
  'RLS habilitado en sources (SEC-003)'
);
select is(
  (select relrowsecurity from pg_class where oid = 'public.jobs'::regclass),
  true,
  'RLS habilitado en jobs (SEC-003)'
);
select is(
  (select relrowsecurity from pg_class where oid = 'public.videos'::regclass),
  true,
  'RLS sigue habilitado en videos (paridad spike)'
);
select is_empty(
  $$ select schemaname, tablename from pg_policies
      where schemaname = 'public'
        and tablename in ('sources', 'jobs') $$,
  'sin políticas RLS en sources/jobs (deny-by-default)'
);
select is(
  has_table_privilege('anon', 'public.sources', 'SELECT'),
  false,
  'anon NO tiene privilegio SELECT sobre sources'
);
select is(
  has_table_privilege('authenticated', 'public.jobs', 'SELECT'),
  false,
  'authenticated NO tiene privilegio SELECT sobre jobs'
);

-- Test negativo: anon no puede leer (privilegio denegado + RLS).
set local role anon;
select throws_ok(
  $$ select count(*) from public.sources $$,
  '42501',
  NULL,
  'anon no puede leer sources (deny-by-default, SEC-003)'
);
select throws_ok(
  $$ select count(*) from public.jobs $$,
  '42501',
  NULL,
  'anon no puede leer jobs (deny-by-default, SEC-003)'
);
reset role;

-- ── Triggers updated_at (función del spike reutilizada, NO recreada) ───────
select has_trigger('public', 'sources', 'trg_sources_set_updated_at',
  'trigger updated_at en sources (set_updated_at reutilizada)');
select has_trigger('public', 'jobs', 'trg_jobs_set_updated_at',
  'trigger updated_at en jobs (set_updated_at reutilizada)');
select is(
  (select count(*)::int from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'set_updated_at'),
  1,
  'set_updated_at del spike se reutiliza (NO se recrea, contrato PR-025)'
);

select * from finish();

rollback;
