-- 000-platform-foundation: tabla de ejemplo con RLS como patrón de referencia.
-- SEC-001 / DATA-001: RLS habilitado por defecto en tablas accesibles desde clientes.

create extension if not exists "pgcrypto";

create table if not exists public.health_check (
  id uuid primary key default gen_random_uuid(),
  note text,
  created_at timestamptz not null default now()
);

-- RLS habilitado por defecto.
alter table public.health_check enable row level security;

-- Política: los usuarios autenticados pueden leer; nadie escribe desde el cliente
-- (las escrituras se harían desde servidor con service_role si fuese necesario).
drop policy if exists "health_check_select_authenticated" on public.health_check;
create policy "health_check_select_authenticated"
  on public.health_check
  for select
  to authenticated
  using (true);

-- Convención Supabase: se concede el privilegio de tabla y es la RLS quien filtra filas.
-- anon obtiene el privilegio pero, al no tener política, no ve ninguna fila.
grant select on public.health_check to anon, authenticated;
