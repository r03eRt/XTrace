# 0001. Uso de @supabase/ssr para el patrón de clientes

- **Estado**: Aceptada
- **Fecha**: 2026-08-05
- **Spec/Requisitos relacionados**: 000-platform-foundation · SEC-002, DATA-002

## Contexto

Next.js App Router necesita acceder a Supabase desde el navegador y desde el servidor
(Server Components / route handlers) con manejo correcto de sesión vía cookies.

## Decisión

Usar `@supabase/ssr` con dos clientes: `getBrowserClient()` (solo `anon`) y
`getServerClient()` (cookies). El cliente con `service_role` (`getAdminClient`) queda
aislado en `src/server/` e importa `server-only`.

## Alternativas consideradas

- `@supabase/auth-helpers-nextjs` — deprecado en favor de `@supabase/ssr`.
- Un único cliente — no gestiona bien las fronteras servidor/cliente ni las cookies.

## Consecuencias

- Fronteras servidor/cliente claras y seguras.
- `service_role` nunca llega al bundle de cliente.
