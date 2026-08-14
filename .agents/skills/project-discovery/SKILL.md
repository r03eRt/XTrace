---
name: "project-discovery"
description: "Entrevista al usuario y convierte una idea incompleta en requisitos. No elige arquitectura ni escribe código."
---

# Skill: project-discovery

## Propósito

Transformar una idea de producto (posiblemente incompleta) en requisitos claros mediante
una entrevista estructurada, antes de cualquier decisión técnica.

## Cuándo activarse

- Al clonar el repositorio y empezar a trabajar si `docs/PRODUCT_IDEA.md` está en
  `PENDIENTE_DE_DESCUBRIMIENTO` o si no existe una spec de producto aprobada.
- Cuando el usuario propone una idea o una nueva feature sin requisitos.

## Entradas necesarias

- La idea inicial del usuario (aunque sea incompleta).
- Contexto de negocio disponible.

## Archivos que debe leer

- `AGENTS.md`, `.specify/memory/constitution.md`, `docs/PRODUCT_IDEA.md`.

## Pasos obligatorios

1. Máximo **cinco preguntas por ronda**.
2. Primera ronda: (1) problema que resuelve, (2) tipos de usuario, (3) flujo principal,
   (4) datos a almacenar, (5) cómo sabremos que la v1 funciona.
3. Rondas posteriores: roles/permisos, autenticación, flujos secundarios, estados y
   transiciones, reglas de negocio, errores/casos límite, dispositivos/navegadores,
   accesibilidad, idiomas, notificaciones, integraciones, privacidad/seguridad,
   conservación de datos, rendimiento, administración, informes, exclusiones, MVP vs futuro.
4. Tras cada ronda mostrar las secciones de salida (abajo).
5. Registrar decisiones y supuestos en `docs/PRODUCT_IDEA.md`.

## Prohibido

Elegir arquitectura · instalar dependencias · escribir código · dar por aprobada una spec.

## Resultados esperados

`docs/PRODUCT_IDEA.md` actualizado y una base suficiente para `spec-authoring`.

## Comprobaciones

- No cerrar el descubrimiento mientras existan ambigüedades capaces de producir
  implementaciones diferentes.
- Ningún supuesto convertido en requisito sin marcarlo.

## Condiciones de bloqueo

Contradicciones sin resolver o falta de una decisión funcional clave.

## Formato de salida

```markdown
## Decisiones confirmadas

## Supuestos

## Preguntas abiertas

## Contradicciones

## Recomendación
```
