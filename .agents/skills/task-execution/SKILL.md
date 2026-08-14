---
name: "task-execution"
description: "Skill obligatoria para cualquier agente que implemente una tarea. Ejecuta el flujo completo de 10 pasos."
---

# Skill: task-execution

## Propósito

Ejecutar el flujo obligatorio de cada tarea de forma idéntica para cualquier proveedor.

## Cuándo activarse

Siempre que un agente vaya a implementar una tarea.

## Archivos que debe leer

`AGENTS.md`, `.specify/memory/constitution.md`, `spec.md`, `plan.md`, `tasks.md`,
el contrato de tarea correspondiente.

## Pasos obligatorios

1. **Preflight** (AGENTS.md §4). Si algo falla → `BLOCKED`.
2. **Contrato de tarea**: crear/actualizar con el YAML de abajo.
3. **Análisis**: localizar código relacionado, riesgos, archivos previstos, estrategia
   de tests; confirmar que cabe en el alcance. No ampliar alcance en silencio.
4. **Tests iniciales**: test-first cuando aplique; comprobar que falla por la razón
   correcta; relacionar con el requisito. Para tareas estructurales/config → validación
   automatizada equivalente.
5. **Implementación**: cambio mínimo necesario; sin refactors ajenos; sin tocar la spec
   para justificar el código; sin dependencias innecesarias; mantener Docker/Supabase
   local/Vercel, fronteras servidor/cliente, seguridad y accesibilidad.
6. **Autovalidación**: comprobaciones de la tarea + `pnpm verify`. No ocultar fallos
   preexistentes; diferenciarlos de los introducidos.
7. **Handoff**: crear `docs/handoffs/TASK-ID.md`.
8. **Revisión independiente**: otro agente con `task-review`.
9. **Correcciones**: resolver hallazgos con evidencia; re-validar.
10. **Finalización**: el orquestador verifica y marca `DONE`.

El trabajador **no** marca la tarea como `DONE` directamente.

## Contrato de tarea

```yaml
task_id:
spec_id:
title:
status: # PROPOSED|READY|IN_PROGRESS|BLOCKED|READY_FOR_REVIEW|CHANGES_REQUESTED|APPROVED|DONE
assigned_agent:
provider:
branch:
worktree:
requirements:
acceptance_criteria:
dependencies:
allowed_paths:
forbidden_paths:
required_tests:
reviewer:
started_at:
completed_at:
```

## Resultados esperados

Cambio implementado, validado, con handoff y contrato actualizado.

## Condiciones de bloqueo

Ver AGENTS.md §9 y la sección 20 del estándar (spec no aprobada, migración destructiva,
archivos en conflicto, cambio fuera de alcance, etc.).

## Formato de salida

Contrato de tarea + `docs/handoffs/TASK-ID.md`.
