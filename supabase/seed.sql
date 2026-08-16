-- Seed reproducible para desarrollo local. NO usar datos reales de producción.
-- Se ejecuta con `pnpm supabase:reset`.
-- Añade aquí datos de test y usuarios de test cuando existan tablas.

-- ============================================================================
-- PR-038 · Fuentes de desarrollo (DATA-001 · SEC-002) — converge F4
-- ----------------------------------------------------------------------------
-- Solo datos de desarrollo: `mock` habilitado (sin red, FR-003) y `xvideos`
-- DESHABILITADO hasta la revisión legal/ToS/robots del humano (SEC-002:
-- robots_reviewed=false, terms_reviewed=false, review_date=null).
-- Idempotente: `on conflict (name) do nothing` — `supabase db reset` aplica el
-- seed una vez; una re-ejecución manual no duplica ni sobrescribe filas.
-- ============================================================================

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
