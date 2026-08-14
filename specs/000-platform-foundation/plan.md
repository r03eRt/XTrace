# Plan técnico: 000-platform-foundation

**Spec**: `specs/000-platform-foundation/spec.md` (APPROVED → IMPLEMENTING)

## 1. Arquitectura

- **Next.js (App Router)** con TypeScript estricto. Fronteras servidor/cliente explícitas.
- **Supabase** para BD/auth/almacenamiento. Acceso desde:
  - cliente navegador → `@supabase/ssr` browser client (solo `anon`).
  - servidor (Server Components / route handlers) → server client (cookies).
  - `service_role` **solo** en utilidades de servidor, nunca expuesto.
- **Validación de entorno** centralizada (`src/lib/env.ts`) con `zod`: la app falla de
  forma explícita si falta una variable obligatoria (FR-008).

## 2. Modelo de datos (mínimo del esqueleto)

- Tabla de ejemplo `public.health_check(id uuid pk, note text, created_at timestamptz)`
  con **RLS habilitado** y política de solo lectura para `authenticated`. Sirve como
  patrón de referencia (SEC-001, DATA-001) y para los tests de BD.

## 3. Contratos / interfaces

- `getBrowserClient()` y `getServerClient()` en `src/lib/supabase/`.
- `env` tipado exportado desde `src/lib/env.ts`.
- Tipos generados en `src/types/supabase.ts` (`pnpm supabase:types`).

## 4. Estrategia de seguridad

- RLS por defecto; política + tests +/- (pgTAP) en `supabase/tests/`.
- Sin secretos en repo; `.env.example` como referencia; gitleaks en CI.
- `service_role` aislado en `src/server/`.

## 5. Estrategia de tests

- **Unit/componentes**: Vitest + Testing Library (`src/**/*.test.ts(x)`, `tests/unit/`).
- **BD**: `supabase test db` (pgTAP) → script `test:db` (requiere Supabase local).
- **E2E**: WebdriverIO, Chrome headless, `tests/e2e/specs/*.e2e.ts`; suite `smoke`.
- Puertas: `pnpm verify` encadena format→lint→typecheck→test→test:db→test:e2e→build.

## 6. Estrategia de despliegue

- Vercel: Preview por rama/PR, producción desde `main` (`vercel.json`).
- Variables por entorno; la app valida su presencia al arrancar.

## 7. Observabilidad

- Logs estructurados en servidor (mínimo en el esqueleto). Health check en `/` (Docker).

## 8. Riesgos

- Ejecutar `test:db`/`test:e2e` requiere Docker/Supabase y Chrome; en máquinas sin ellos,
  esas puertas se ejecutan en CI. Documentado en runbooks.
- Deriva de versiones → lockfile + renovación controlada.

## 9. ADR necesarios

- ADR-0001: elección de `@supabase/ssr` para el patrón de clientes.
- ADR-0002: validación de entorno con `zod` y fallo explícito.

## 10. Versiones

Se instalan **últimas estables** en el momento de la implementación (sin beta/canary/RC),
fijadas por `pnpm-lock.yaml`.
