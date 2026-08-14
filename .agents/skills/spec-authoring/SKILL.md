---
name: "spec-authoring"
description: "Genera y actualiza specs mediante Spec Kit. Define el qué y el porqué, no el cómo."
---

# Skill: spec-authoring

## Propósito

Producir y mantener `specs/NNN-feature-name/spec.md` a partir de los requisitos del
descubrimiento, usando el flujo Spec Kit (`$speckit-specify`).

## Cuándo activarse

Tras `project-discovery`, o cuando un cambio funcional requiere actualizar la spec.

## Entradas necesarias

- Requisitos y decisiones de `docs/PRODUCT_IDEA.md` o del descubrimiento de la feature.

## Archivos que debe leer

- `AGENTS.md`, `.specify/memory/constitution.md`, `.specify/templates/spec-template.md`.

## Pasos obligatorios

Producir una spec que contenga: objetivo · alcance · fuera de alcance · actores ·
historias de usuario · flujos · reglas de negocio · requisitos numerados
(`FR/NFR/SEC/DATA/UX`) · requisitos no funcionales · casos límite · criterios
Given/When/Then · dependencias · riesgos · preguntas pendientes · estado de aprobación.

## Resultados esperados

`spec.md` centrado en **qué** y **por qué**, sin decisiones técnicas, con estado
`DRAFT` o `READY_FOR_REVIEW`.

## Comprobaciones

- Cada requisito tiene ID estable. Cada criterio es verificable.
- No mezcla decisiones de implementación.

## Condiciones de bloqueo

Requisitos contradictorios o ambiguos → derivar a `spec-clarification`.

## Formato de salida

Una spec conforme a `.specify/templates/spec-template.md` con encabezado de **Estado**.
