# 0002. Validación de entorno con zod y fallo explícito

- **Estado**: Aceptada
- **Fecha**: 2026-08-05
- **Spec/Requisitos relacionados**: 000-platform-foundation · FR-008

## Contexto

La app debe fallar de forma explícita cuando falte una variable de entorno obligatoria,
en lugar de comportarse de forma silenciosa o impredecible.

## Decisión

Centralizar la validación en `src/lib/env.ts` con `zod`. Las variables públicas se
validan al importar el módulo (lanza error si faltan); las de servidor mediante
`getServerEnv()`.

## Alternativas consideradas

- Leer `process.env` directamente — sin validación ni tipos, errores tardíos.
- `@t3-oss/env-nextjs` — buena opción, pero añade dependencia; `zod` directo es suficiente.

## Consecuencias

- Errores tempranos y claros ante configuración incompleta.
- Tipos seguros para el resto del código.
