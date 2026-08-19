-- Tests pgTAP del esquema de telemetría de refinamiento (TASK-006-T002).
-- Ejecuta con `pnpm test:db` / `supabase test db` con Supabase local.

begin;

select plan(34);

select has_table('public', 'search_refinements', 'search_refinements existe (DATA-001)');
select has_table('public', 'search_refinement_evidence',
  'search_refinement_evidence existe (DATA-001)');

select has_column('public', 'search_refinements', 'bytes_downloaded',
  'métrica de bytes agregados (NFR-003)');
select has_column('public', 'search_refinements', 'embedding_count',
  'número de embeddings (NFR-003)');
select has_column('public', 'search_refinements', 'embedding_elapsed_ms',
  'tiempo de embeddings (NFR-003)');
select has_column('public', 'search_refinement_evidence', 'asset_url_hash',
  'hash de URL de evidencia (DATA-001)');
select has_column('public', 'search_refinement_evidence', 'timestamp_ms',
  'timestamp de evidencia (DATA-001)');

select col_type_is('public', 'search_refinements', 'bytes_downloaded', 'bigint',
  'bytes_downloaded es bigint');
select col_type_is('public', 'search_refinements', 'embedding_count', 'integer',
  'embedding_count es integer');
select col_type_is('public', 'search_refinement_evidence', 'similarity', 'double precision',
  'similarity es double precision');

select has_index('public', 'search_refinements', 'idx_search_refinements_status',
  'índice por estado');
select has_index('public', 'search_refinements', 'idx_search_refinements_policy',
  'índice por policy_version');
select has_index('public', 'search_refinement_evidence', 'idx_refinement_evidence_source',
  'índice de métricas por fuente');
select has_index('public', 'search_refinement_evidence', 'uq_refinement_evidence_asset',
  'índice único de evidencia');

select is(
  (select relrowsecurity from pg_class where oid = 'public.search_refinements'::regclass),
  true, 'RLS habilitado en search_refinements');
select is(
  (select relrowsecurity from pg_class where oid = 'public.search_refinement_evidence'::regclass),
  true, 'RLS habilitado en search_refinement_evidence');
select is_empty(
  $$ select * from pg_policies where schemaname = 'public'
     and tablename in ('search_refinements', 'search_refinement_evidence') $$,
  'sin políticas RLS para mantener deny-by-default');
select is(has_table_privilege('anon', 'public.search_refinements', 'SELECT'), false,
  'anon no tiene SELECT sobre summary');
select is(has_table_privilege('authenticated', 'public.search_refinement_evidence', 'SELECT'), false,
  'authenticated no tiene SELECT sobre evidence');
select is(
  (
    select bool_and(not has_table_privilege(role_name, table_name, privilege_name))
    from unnest(array['anon', 'authenticated']::name[]) as roles(role_name)
    cross join unnest(array['public.search_refinements', 'public.search_refinement_evidence']::text[])
      as tables(table_name)
    cross join unnest(array['SELECT', 'INSERT', 'UPDATE', 'DELETE']::text[])
      as privileges(privilege_name)
  ),
  true,
  'anon/authenticated no tienen SELECT/INSERT/UPDATE/DELETE en ninguna tabla'
);

select lives_ok(
  $$ insert into public.videos (local_ref) values ('fixture/task-006-t002.mp4') $$,
  'vídeo fixture insertable por server-side');
select lives_ok(
  $$ insert into public.searches (id, search_type, processing_ms, results_count)
     values ('00000000-0000-0000-0000-000000000601', 'image', 10, 1) $$,
  'search fixture insertable por server-side');
select lives_ok(
  $$ insert into public.search_refinements
       (search_id, policy_version, candidates_requested, candidates_processed,
        assets_evaluated, bytes_downloaded, embedding_count, embedding_elapsed_ms)
     values ('00000000-0000-0000-0000-000000000601', 'v1', 3, 1, 1, 128, 1, 2) $$,
  'summary válido (DATA-002/003)');
select lives_ok(
  $$ insert into public.search_refinement_evidence
       (search_id, video_id, source, candidate_rank, asset_kind, asset_url,
        asset_url_hash, timestamp_ms, similarity, selected)
     select '00000000-0000-0000-0000-000000000601', id, 'xvideos', 1, 'thumbnail',
       'https://thumb-cdn77.xvideos-cdn.com/xv_1_t.jpg', 'hash-1', 1000, 0.99, true
       from public.videos where local_ref = 'fixture/task-006-t002.mp4' $$,
  'evidence pública válida (DATA-001)');
select is(
  (select count(*)::int from public.search_refinement_evidence
    where search_id = '00000000-0000-0000-0000-000000000601'),
  1, 'evidence enlazada al summary');

select throws_ok(
  $$ insert into public.search_refinements (search_id, policy_version, candidates_requested,
       candidates_processed) values ('00000000-0000-0000-0000-000000000601', 'v1', 1, 2) $$,
  '23514', NULL, 'candidates_processed no puede superar requested');
select throws_ok(
  $$ insert into public.search_refinement_evidence
       (search_id, video_id, source, candidate_rank, asset_kind, asset_url,
        asset_url_hash, similarity)
     select '00000000-0000-0000-0000-000000000601', id, 'xvideos', 1, 'preview',
       'https://cdn.example/preview.mp4', 'hash-preview', 0.9 from public.videos
       where local_ref = 'fixture/task-006-t002.mp4' $$,
  '23514', NULL, 'preview no es un asset de evidencia');
select throws_ok(
  $$ insert into public.search_refinement_evidence
       (search_id, video_id, source, candidate_rank, asset_kind, asset_url,
        asset_url_hash, similarity)
     select '00000000-0000-0000-0000-000000000601', id, 'xvideos', 1, 'thumbnail',
       'https://thumb-cdn77.xvideos-cdn.com/xv_1_t.jpg', 'hash-invalid-score', 1.1
       from public.videos where local_ref = 'fixture/task-006-t002.mp4' $$,
  '23514', NULL, 'similarity fuera de [0,1]');
select throws_ok(
  $$ insert into public.search_refinement_evidence
       (search_id, video_id, source, candidate_rank, asset_kind, asset_url,
        asset_url_hash, similarity, selected)
     select '00000000-0000-0000-0000-000000000601', id, 'xvideos', 1, 'thumbnail',
       'https://thumb-cdn77.xvideos-cdn.com/xv_2_t.jpg', 'hash-selected-without-time', 0.9, true
       from public.videos where local_ref = 'fixture/task-006-t002.mp4' $$,
  '23514', NULL, 'evidencia seleccionada necesita timestamp');

set local role anon;
select throws_ok(
  $$ insert into public.search_refinements (search_id, policy_version)
     values ('00000000-0000-0000-0000-000000000601', 'anon') $$,
  '42501', NULL, 'anon no puede insertar summary');
select throws_ok(
  $$ select count(*) from public.search_refinement_evidence $$,
  '42501', NULL, 'anon no puede leer evidence');
reset role;

select lives_ok(
  $$ delete from public.searches where id = '00000000-0000-0000-0000-000000000601' $$,
  'delete search permitido al server-side');
select is((select count(*)::int from public.search_refinements), 0,
  'cascade elimina summary con searches (SEC-005)');
select is((select count(*)::int from public.search_refinement_evidence), 0,
  'cascade elimina evidence con summary (SEC-005)');

select * from finish();

rollback;
