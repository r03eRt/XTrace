---
name: "task-planning"
description: "Genera tareas pequeñas, verificables y ordenadas por dependencias, trazables a la spec."
---

# Skill: task-planning

## Propósito

Descomponer un plan en tareas ejecutables, ordenadas por dependencias y trazables.

## Cuándo activarse

Tras `technical-planning`, con spec `APPROVED` y `plan.md` existente.

## Entradas necesarias

- `spec.md` y `plan.md`.

## Archivos que debe leer

- `spec.md`, `plan.md`, `.specify/templates/tasks-template.md`, `AGENTS.md`.

## Pasos obligatorios

Generar `tasks.md`. Cada tarea incluye: ID · objetivo · spec y requisitos relacionados ·
dependencias · archivos previstos (`allowed_paths`) · tests requeridos · criterios de
finalización · posibilidad de paralelización · rol recomendado · nivel de riesgo.

## Resultados esperados

`tasks.md` con tareas pequeñas y verificables; grafo de dependencias explícito.

## Comprobaciones

- Ninguna tarea mezcla features no relacionadas.
- Las tareas paralelas no comparten archivos ni dependencias pendientes.

## Condiciones de bloqueo

Dependencia sobre una decisión aún abierta.

## Formato de salida

`tasks.md` + contratos de tarea en `specs/NNN-*/contracts/` (ver `task-execution`).
