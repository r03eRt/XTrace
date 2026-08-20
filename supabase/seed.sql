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

-- mock: habilitado para desarrollo (sin red, FR-003). Manifest CANÓNICO del
-- MockAdapter en código (adapters/mock.py, alineación PR-036):
-- rate_limit min_interval_ms=100 / max_rps=10.0 y review_date=null (el mock
-- está exento del gate SEC-002 por ser real=False en el registry, PR-028).
insert into public.sources (name, adapter, manifest, enabled) values (
  'mock', 'mock',
  '{"source":"mock","access_method":"json",
    "assets_accessed":["storyboard","thumbnail","preview"],
    "robots_reviewed":true,"terms_reviewed":true,
    "rate_limit":{"min_interval_ms":100,"max_rps":10.0},
    "review_date":null}', true)
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

-- erome: DESHABILITADO hasta la acción humana explícita (SEC-002). El manifest
-- del adapter (código) ya documenta robots_reviewed=true/terms_reviewed=true
-- (revisión de robots.txt/ToS previa a escribir el adapter); el seed se queda
-- deliberadamente sin fecha/enabled hasta que un humano decida habilitarla.
insert into public.sources (name, adapter, manifest, enabled) values (
  'erome', 'erome',
  '{"source":"erome","access_method":"html",
    "assets_accessed":["thumbnail","storyboard"],
    "robots_reviewed":false,"terms_reviewed":false,
    "rate_limit":{"min_interval_ms":2000,"max_rps":0.5},
    "review_date":null}', false)
on conflict (name) do nothing;

-- redgifs: DESHABILITADO hasta la habilitación explícita en BD (SEC-002),
-- pero con la revisión legal/ToS/robots ya dada por el humano **en modo
-- prueba** (Decisión D4 de la spec 008, 2026-08-19; PR-067): a diferencia de
-- xvideos/erome arriba, aquí robots_reviewed/terms_reviewed/review_date SÍ
-- reflejan esa revisión (paridad exacta con el manifest en código,
-- adapters/redgifs.py) — solo falta `enabled=true`, acción humana explícita
-- reservada para la validación real (PR-069).
insert into public.sources (name, adapter, manifest, enabled) values (
  'redgifs', 'redgifs',
  '{"source":"redgifs","access_method":"api",
    "assets_accessed":["thumbnail"],
    "robots_reviewed":true,"terms_reviewed":true,
    "rate_limit":{"min_interval_ms":2000,"max_rps":0.5},
    "review_date":"2026-08-19"}', false)
on conflict (name) do nothing;
