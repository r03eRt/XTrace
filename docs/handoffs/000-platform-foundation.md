# Handoff — 000-platform-foundation

- **Resumen**: Implementación del esqueleto técnico ejecutable (spec 000). App Next.js 16
  (App Router) + TS estricto, clientes Supabase (`@supabase/ssr`), validación de entorno
  con `zod`, tests unitarios/componentes (Vitest + Testing Library), smoke E2E (WDIO 9),
  migración con RLS + tests pgTAP, y puertas de calidad cableadas.

- **Requisitos implementados**: FR-001, FR-004, FR-006, FR-008, FR-009 (plantilla previa),
  NFR-001, NFR-002, NFR-003, SEC-001, SEC-002, DATA-001, DATA-002, UX-001.

- **Archivos añadidos/modificados** (principales):
  - `src/app/{layout.tsx,page.tsx,globals.css}`
  - `src/lib/env.ts`, `src/lib/supabase/{client,server}.ts`, `src/server/supabase-admin.ts`
  - `src/types/supabase.ts`
  - `tests/unit/{env.test.ts,home.test.tsx}`, `tests/setup.ts`
  - `tests/e2e/specs/home.smoke.e2e.ts`, `tests/e2e/tsconfig.json`, `wdio.conf.ts`
  - `supabase/migrations/20260805171159_init_health_check.sql`, `supabase/tests/health_check_rls.test.sql`
  - `eslint.config.mjs` (flat), `vitest.config.ts`, `tsconfig.json`
  - `docs/adr/0001-supabase-ssr.md`, `docs/adr/0002-env-zod.md`
  - `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`

- **Decisiones tomadas** (compatibilidad de versiones, constitución §9 "última estable _compatible_"):
  - TypeScript fijado a **6.0.3** (TS 7.0 no soportado por typescript-eslint).
  - ESLint fijado a **9.39.5** (ESLint 10 incompatible con eslint-plugin-react de Next).
  - `next lint` eliminado en Next 16 → lint con ESLint directo (`eslint .`) y flat config.

- **Tests añadidos**: 3 unit/componente (env +/- y home), 1 smoke E2E, 3 asserts pgTAP RLS.

- **Comandos ejecutados y resultados**:
  - `pnpm format:check` → ✅
  - `pnpm lint` → ✅
  - `pnpm typecheck` (root) → ✅ · e2e tsconfig → ✅
  - `pnpm test` → ✅ 3/3
  - `pnpm build` → ✅
  - `pnpm test:db` / `pnpm test:e2e` → requieren Supabase local (Docker) y Chrome.

- **Limitaciones / trabajo pendiente**:
  - `pnpm supabase:types` regenerará `src/types/supabase.ts` (ahora es placeholder).
  - `test:e2e` requiere navegador Chrome + app en marcha; se ejecuta en CI (`e2e.yml`).
  - Falta enlazar el proyecto Vercel (Preview) — acción de infraestructura externa.

- **Riesgos**: entorno con versiones muy nuevas; lockfile fija las combinaciones válidas.

- **Instrucciones para el revisor** (skill `task-review`, agente/proveedor distinto):
  verificar env fail-fast, aislamiento de `service_role`, RLS + tests +/-, selectores
  `data-testid`, y que `pnpm verify` pase con servicios levantados.
