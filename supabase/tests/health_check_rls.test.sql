-- Tests de base de datos (pgTAP) para la política RLS de health_check.
-- Ejecuta con: `pnpm test:db` (requiere Supabase local en marcha).
-- SEC-001: toda política RLS debe tener tests positivos y negativos.

begin;

select plan(4);

-- La tabla existe.
select has_table('public', 'health_check', 'health_check existe');

-- RLS está habilitado.
select is(
  (select relrowsecurity from pg_class where oid = 'public.health_check'::regclass),
  true,
  'RLS habilitado en health_check'
);

-- Sembramos una fila como owner (el runner conecta como superusuario, salta RLS).
insert into public.health_check (note) values ('ping');

-- Test positivo: un usuario autenticado ve la fila (policy using(true)).
set local role authenticated;
select isnt_empty(
  $$ select 1 from public.health_check $$,
  'authenticated puede leer (RLS permite via policy)'
);
reset role;

-- Test negativo: anon no ve filas (sin política para anon, RLS deniega).
set local role anon;
select is_empty(
  $$ select 1 from public.health_check $$,
  'anon no lee ninguna fila (RLS deniega)'
);
reset role;

select * from finish();

rollback;
