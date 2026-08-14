---
name: "supabase-database"
description: "Migraciones, seeds, RLS, políticas, funciones, tipos generados y tests de BD. Bloquea cambios de esquema sin migración ni pruebas."
---

# Skill: supabase-database

## Propósito

Gestionar el esquema y la seguridad de datos de Supabase de forma reversible y probada.

## Cuándo activarse

Cualquier cambio de esquema, política RLS, función, seed o tipo generado.

## Archivos que debe leer

`supabase/config.toml`, `supabase/migrations/`, `supabase/seed.sql`, `supabase/tests/`,
`plan.md`, la spec.

## Pasos obligatorios

- Crear migraciones versionadas en `supabase/migrations/` (compatibles y reversibles).
- Habilitar **RLS por defecto** en tablas accesibles desde clientes.
- Escribir tests **positivos y negativos** de cada política RLS.
- Mantener seed reproducible y usuarios de test.
- Generar tipos TypeScript (`pnpm supabase:types` → `src/types/`).
- Ejecutar `pnpm test:db`.

## Prohibido

Cambiar esquema sin migración + pruebas → **bloquear**. Migraciones destructivas sin
plan de recuperación. Cambios manuales en producción salvo emergencia documentada.
`service_role` fuera de código de servidor. Ejecutar migraciones de producción en el
build de Vercel.

## Resultados esperados

Migración + política + tests + tipos, con entornos local/preview/producción separados.

## Comprobaciones

- Toda tabla accesible desde cliente tiene RLS y tests.
- `pnpm test:db` verde. Tipos regenerados.

## Condiciones de bloqueo

Riesgo de pérdida de datos o falta de plan de migración/recuperación.

## Formato de salida

Archivos en `supabase/migrations/`, `supabase/tests/`, tipos en `src/types/`.
