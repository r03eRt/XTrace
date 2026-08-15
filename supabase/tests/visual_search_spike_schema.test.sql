-- Tests pgTAP del esquema del Visual Search Spike (PR-006).
-- Ejecuta con: pnpm test:db (requiere Supabase local en marcha).
-- Valida: FR-006 (ANN/HNSW), FR-007 (videos/estado), FR-008 (uniques/idempotencia),
--         FR-018 (searches sin media), SC-005 (constraint idempotencia),
--         ADR-0004 (pgvector + HNSW), data-model.md (D = 768, RLS deny-by-default).

begin;

select plan(32);

-- ── Extensiones ─────────────────────────────────────────────────────────────
select has_extension('vector', 'extensión pgvector instalada (ADR-0004)');
select has_extension('pgcrypto', 'extensión pgcrypto instalada (gen_random_uuid)');

-- ── Tablas ──────────────────────────────────────────────────────────────────
select has_table('public', 'videos', 'tabla videos existe (FR-007)');
select has_table('public', 'frames', 'tabla frames existe (FR-006)');
select has_table('public', 'searches', 'tabla searches existe (FR-018)');

-- ── Dimensión del embedding ─────────────────────────────────────────────────
select is(
  (select format_type(a.atttypid, a.atttypmod)
     from pg_attribute a
     join pg_class c on c.oid = a.attrelid
    where c.relname = 'frames' and a.attname = 'embedding'),
  'vector(768)',
  'frames.embedding es vector(768) (D fijada por PR-005)'
);

-- ── Constraints únicos (idempotencia, FR-008 / SC-005) ──────────────────────
select is(
  (select count(*)::int from pg_constraint
    where conname = 'uq_videos_local_ref'
      and contype = 'u'
      and conrelid = 'public.videos'::regclass),
  1,
  'UNIQUE(local_ref) en videos: reindexar no duplica vídeo'
);
select is(
  (select count(*)::int from pg_constraint c
    where c.conname = 'uq_frames_video_frame_seq'
      and c.contype = 'u'
      and c.conrelid = 'public.frames'::regclass
      and c.conkey = ARRAY[
        (select attnum from pg_attribute where attrelid = 'public.frames'::regclass and attname = 'video_id'),
        (select attnum from pg_attribute where attrelid = 'public.frames'::regclass and attname = 'frame_seq')]),
  1,
  'UNIQUE(video_id, frame_seq) en frames: clave estable sin timestamp'
);
select is(
  (select count(*)::int from pg_constraint c
    where c.conname = 'uq_frames_video_timestamp_ms'
      and c.contype = 'u'
      and c.conrelid = 'public.frames'::regclass
      and c.conkey = ARRAY[
        (select attnum from pg_attribute where attrelid = 'public.frames'::regclass and attname = 'video_id'),
        (select attnum from pg_attribute where attrelid = 'public.frames'::regclass and attname = 'timestamp_ms')]),
  1,
  'UNIQUE(video_id, timestamp_ms) en frames: reindexar no duplica (FR-008)'
);

-- ── FK: frames.video_id → videos.id ON DELETE CASCADE ───────────────────────
select is(
  (select confdeltype = 'c' from pg_constraint where conname = 'fk_frames_video'),
  true,
  'FK frames.video_id → videos(id) ON DELETE CASCADE'
);

-- ── Índices ─────────────────────────────────────────────────────────────────
select has_index('public', 'videos', 'idx_videos_status',
  'idx_videos_status existe (filtro por estado)');
select has_index('public', 'frames', 'idx_frames_phash',
  'idx_frames_phash existe (near-exact, FR-004/ADR-0005)');
select has_index('public', 'frames', 'idx_frames_video_id',
  'idx_frames_video_id existe (agrupación por vídeo)');
select has_index('public', 'frames', 'idx_frames_embedding_hnsw',
  'idx_frames_embedding_hnsw existe (ANN, FR-006)');

select is(
  (select amname from pg_am where oid = (
     select c.relam from pg_class c
      where c.oid = 'public.idx_frames_embedding_hnsw'::regclass)),
  'hnsw',
  'idx_frames_embedding_hnsw usa acceso HNSW (ADR-0004)'
);

select is(
  (select opcname from pg_opclass where oid = (
     select i.indclass[0] from pg_index i
      where i.indexrelid = 'public.idx_frames_embedding_hnsw'::regclass)),
  'vector_cosine_ops',
  'idx_frames_embedding_hnsw usa vector_cosine_ops (coseno)'
);

-- ── RLS deny-by-default ─────────────────────────────────────────────────────
select is(
  (select relrowsecurity from pg_class where oid = 'public.videos'::regclass),
  true,
  'RLS habilitado en videos'
);
select is(
  (select relrowsecurity from pg_class where oid = 'public.frames'::regclass),
  true,
  'RLS habilitado en frames'
);
select is(
  (select relrowsecurity from pg_class where oid = 'public.searches'::regclass),
  true,
  'RLS habilitado en searches'
);

select is_empty(
  $$ select schemaname, tablename from pg_policies
      where schemaname = 'public'
        and tablename in ('videos', 'frames', 'searches') $$,
  'sin políticas RLS en videos/frames/searches (deny-by-default)'
);

select is(
  has_table_privilege('anon', 'public.videos', 'SELECT'),
  false,
  'anon NO tiene privilegio SELECT sobre videos'
);
select is(
  has_table_privilege('authenticated', 'public.frames', 'SELECT'),
  false,
  'authenticated NO tiene privilegio SELECT sobre frames'
);

-- Test negativo: anon no puede leer (privilegio denegado + RLS).
set local role anon;
select throws_ok(
  $$ select count(*) from public.videos $$,
  '42501',
  NULL,
  'anon no puede leer videos (deny-by-default)'
);
reset role;

-- ── Trigger updated_at ──────────────────────────────────────────────────────
select has_trigger('public', 'videos', 'trg_videos_set_updated_at',
  'trigger updated_at existe en videos (data-model.md)');

-- ── Flujo funcional service_role (el runner conecta como superusuario) ──────
select lives_ok(
  $$ insert into public.videos (local_ref, duration_ms, status, frame_count)
     values ('fixture/pr006.mp4', 15000, 'indexed', 3) $$,
  'insert videos válido (service_role/owner)'
);

select lives_ok(
  $$ insert into public.frames (video_id, frame_seq, timestamp_ms, phash, embedding)
     select id, 1, 0, 123456789, ('[' || repeat('0,', 767) || '0]')::vector
       from public.videos where local_ref = 'fixture/pr006.mp4' $$,
  'insert frame con timestamp_ms válido'
);

select lives_ok(
  $$ insert into public.frames (video_id, frame_seq, phash, embedding)
     select id, 2, 987654321, ('[' || repeat('0,', 767) || '0]')::vector
       from public.videos where local_ref = 'fixture/pr006.mp4' $$,
  'varios frames con timestamp_ms NULL son válidos (NULLS DISTINCT)'
);

select lives_ok(
  $$ insert into public.searches (search_type, processing_ms, results_count)
     values ('image', 42, 3) $$,
  'insert searches válido (search_type=image, FR-018: sin media)'
);

-- ── Negativos de constraints ────────────────────────────────────────────────
select throws_ok(
  $$ insert into public.frames (video_id, frame_seq, phash, embedding)
     select id, 1, 555, ('[' || repeat('0,', 767) || '0]')::vector
       from public.videos where local_ref = 'fixture/pr006.mp4' $$,
  '23505',
  NULL,
  'UNIQUE(video_id, frame_seq) rechaza frame duplicado (SC-005)'
);

select throws_ok(
  $$ insert into public.frames (video_id, frame_seq, timestamp_ms, phash, embedding)
     select id, 10, 0, 444, ('[' || repeat('0,', 767) || '0]')::vector
       from public.videos where local_ref = 'fixture/pr006.mp4' $$,
  '23505',
  NULL,
  'UNIQUE(video_id, timestamp_ms) rechaza frame duplicado (FR-008)'
);

select throws_ok(
  $$ insert into public.videos (local_ref, status) values ('bad.mp4', 'bogus') $$,
  '23514',
  NULL,
  'CHECK status limita a discovered/pending/indexing/indexed/failed (FR-007)'
);

select throws_ok(
  $$ insert into public.searches (search_type, processing_ms, results_count)
     values ('clip', 10, 0) $$,
  '23514',
  NULL,
  'CHECK search_type limita a image (spike)'
);

select * from finish();

rollback;
