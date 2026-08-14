---
name: "spec-clarification"
description: "Ejecuta rondas de aclaración (máx. 5 preguntas/ronda) y actualiza la spec con historial de decisiones."
---

# Skill: spec-clarification

## Propósito

Eliminar ambigüedades relevantes de una spec antes de planificar.

## Cuándo activarse

Cuando la spec contiene `[NEEDS CLARIFICATION]`, preguntas abiertas o contradicciones;
antes de `technical-planning`.

## Entradas necesarias

- La spec objetivo y las preguntas abiertas detectadas.

## Archivos que debe leer

- La `spec.md` correspondiente, `AGENTS.md`, `.specify/memory/constitution.md`.

## Pasos obligatorios

1. Máximo **cinco preguntas por ronda**.
2. Actualizar la spec con las respuestas.
3. Mantener un **historial de decisiones** dentro de la spec.
4. Repetir hasta que no queden ambigüedades capaces de cambiar la implementación.

## Resultados esperados

Spec sin ambigüedades relevantes y con historial de decisiones.

## Comprobaciones

- Ninguna pregunta abierta crítica sin respuesta.
- El silencio no se interpreta como respuesta.

## Condiciones de bloqueo

Falta una decisión funcional que solo el humano puede tomar.

## Formato de salida

Actualización de `spec.md` + bloque `## Historial de decisiones` con fecha y respuesta.
