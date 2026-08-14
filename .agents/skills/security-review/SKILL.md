---
name: "security-review"
description: "Revisa autenticación, autorización, RLS, exposición de datos, validación, secretos, dependencias y rutas de servidor."
---

# Skill: security-review

## Propósito

Detectar problemas de seguridad antes de completar una feature.

## Cuándo activarse

Antes de finalizar cualquier tarea que toque auth, datos, rutas de servidor, subida de
archivos o dependencias; obligatoria en `pr-finalization`.

## Archivos que debe leer

`src/server/`, `supabase/migrations/`, `supabase/tests/`, `plan.md`, la spec, `.env.example`.

## Debe revisar

Autenticación · autorización · RLS · exposición de datos · validación (servidor, no solo
UI) · secretos · dependencias · rutas de servidor · almacenamiento · subida de archivos ·
abuso y rate limiting cuando corresponda.

## Comprobaciones

- Sin secretos en el repositorio ni en imágenes/compose.
- `service_role` solo en servidor.
- RLS habilitado con tests positivos y negativos.
- Permisos validados en servidor.

## Resultados esperados

Informe con hallazgos, severidad, evidencia y remediación.

## Condiciones de bloqueo

Vulnerabilidad de alta severidad o exposición de datos sensibles.

## Formato de salida

```markdown
## Hallazgos de seguridad

- [SEC-###] <severidad> <descripción> — evidencia — remediación

## Veredicto: PASS | FAIL
```
