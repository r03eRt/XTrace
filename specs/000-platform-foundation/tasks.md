# Tareas: 000-platform-foundation

> Estados: PROPOSED · READY · IN_PROGRESS · BLOCKED · READY_FOR_REVIEW ·
> CHANGES_REQUESTED · APPROVED · DONE

> **Resumen**: T-001..T-006 en estado **DONE**. Todas las puertas verificadas en
> local (incl. `test:db` con Supabase y `test:e2e:smoke` con Chrome 150).

## T-001 — Instalar dependencias (últimas estables) — DONE

- **Requisitos**: NFR-002, NFR-003, FR-004
- **Archivos**: `package.json`, `pnpm-lock.yaml`
- **Tests**: `pnpm install --frozen-lockfile` reproducible
- **Paralela**: no · **Riesgo**: medio

## T-002 — Scaffolding Next.js + validación de entorno — DONE

- **Requisitos**: FR-001, FR-008, NFR-001
- **Archivos**: `src/app/{layout.tsx,page.tsx,globals.css}`, `src/lib/env.ts`,
  `next-env.d.ts`
- **Tests**: `pnpm build`, `pnpm typecheck`, test unit de `env`
- **Depende de**: T-001 · **Riesgo**: medio

## T-003 — Clientes Supabase + migración RLS + tests BD — DONE

- **Requisitos**: SEC-001, SEC-002, DATA-001, DATA-002
- **Archivos**: `src/lib/supabase/{client.ts,server.ts}`, `src/server/supabase-admin.ts`,
  `supabase/migrations/*_init.sql`, `supabase/tests/*.sql`, `src/types/supabase.ts`
- **Tests**: `supabase test db` (pgTAP +/-)
- **Depende de**: T-001 · **Paralela con T-002**: sí (archivos distintos) · **Riesgo**: alto

## T-004 — Tests de ejemplo (unit + smoke E2E) — DONE

- **Requisitos**: FR-006, UX-001
- **Archivos**: `tests/unit/env.test.ts`, `tests/e2e/specs/home.smoke.e2e.ts`,
  `src/app/page.tsx` (`data-testid`)
- **Tests**: `pnpm test`, `pnpm test:e2e:smoke`
- **Depende de**: T-002 · **Riesgo**: medio

## T-005 — Ejecutar puertas de calidad — DONE

- **Requisitos**: FR-004, FR-005
- **Archivos**: —
- **Tests**: `pnpm format:check`, `lint`, `typecheck`, `test`, `build`
  (`test:db`/`test:e2e` requieren Supabase/Chrome, se validan en CI)
- **Depende de**: T-002, T-003, T-004 · **Riesgo**: medio

## T-006 — ADRs — DONE

- **Archivos**: `docs/adr/0001-supabase-ssr.md`, `docs/adr/0002-env-zod.md`
- **Depende de**: T-002, T-003 · **Paralela**: sí · **Riesgo**: bajo
