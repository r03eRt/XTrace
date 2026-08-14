---
name: "task-review"
description: "Revisión independiente por un agente distinto (idealmente otro proveedor). Compara código contra spec, plan, tarea, tests y seguridad."
---

# Skill: task-review

## Propósito

Verificar de forma adversarial que una tarea cumple spec, plan, alcance y calidad.

## Cuándo activarse

Cuando una tarea está en `READY_FOR_REVIEW`. **La ejecuta un agente diferente al
implementador**, preferiblemente con otro modelo/proveedor.

## Archivos que debe leer

`spec.md`, `plan.md`, `tasks.md`, el contrato de tarea, el handoff, el diff de la rama.

## Pasos obligatorios

Comparar: código vs spec · código vs plan · código vs tarea · tests vs criterios de
aceptación · migraciones vs modelo de datos · comportamiento vs seguridad (RLS incluida)
· cambios vs alcance permitido. Revisar además: lógica, accesibilidad, rendimiento,
mantenibilidad, documentación.

## Prohibido

Limitarse a estilo. El revisor no implementa.

## Resultados esperados

Veredicto explícito:

```text
APPROVED
```

o

```text
CHANGES_REQUESTED
```

con hallazgos concretos, prioridad y evidencia.

## Comprobaciones

- Cada criterio de aceptación tiene un test que lo valida.
- No hay ampliación de alcance no documentada.

## Condiciones de bloqueo

Hallazgos que requieran una decisión funcional o de seguridad del humano.

## Formato de salida

Comentario/handoff de revisión con veredicto + lista priorizada de hallazgos.
