---
name: "pr-finalization"
description: "Validación final, analyze, converge, evidencias, Preview de Vercel, ausencia de secretos y resumen de trazabilidad para el PR."
---

# Skill: pr-finalization

## Propósito

Dejar el PR listo para revisión humana con todas las puertas superadas.

## Cuándo activarse

Cuando todas las tareas de la feature están `DONE` y revisadas.

## Archivos que debe leer

`spec.md`, `plan.md`, `tasks.md`, handoffs, `.github/pull_request_template.md`.

## Pasos obligatorios

1. Ejecutar validación final: `pnpm verify`.
2. Ejecutar Spec Kit `$speckit-analyze` (sin bloqueos).
3. Ejecutar Spec Kit `$speckit-converge` (convergido).
4. Preparar el PR con la plantilla obligatoria.
5. Añadir evidencias (comandos, resultados, capturas).
6. Confirmar Preview de Vercel.
7. Confirmar ausencia de secretos.
8. Elaborar el resumen de trazabilidad (Requisito → Implementación → Test).

## Resultados esperados

PR completo, CI verde, Preview enlazada, trazabilidad rellena.

## Comprobaciones

- CI verde en `quality`, `e2e`, `spec-compliance`, `security`.
- El implementador no aprueba su propio trabajo.

## Condiciones de bloqueo

CI fallido, analyze/converge con bloqueos, o secretos detectados.

## Formato de salida

PR conforme a `.github/pull_request_template.md`.
