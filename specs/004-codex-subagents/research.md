# Research: Configuración de subagentes Codex

## Decision 1: Configuración a nivel de proyecto

- **Decision**: Versionar la política en la configuración propia del repositorio.
- **Rationale**: Hace reproducible la política para colaboradores sin modificar sus
  preferencias personales.
- **Alternatives considered**: Mantener solo la configuración global; descartado porque
  no viaja con XTrace. Incluir instrucciones únicamente en `AGENTS.md`; descartado
  porque las instrucciones no fijan defaults del runtime.

## Decision 2: Defaults de subagente sin fijar el orquestador

- **Decision**: Declarar únicamente campos bajo la sección de subagentes.
- **Rationale**: Codex permite defaults específicos para sesiones delegadas y mantiene
  la selección principal fuera de esa sección.
- **Alternatives considered**: Declarar modelo principal en el proyecto; descartado
  porque contradice FR-005 y reduciría la elección del usuario por sesión.

## Decision 3: Sin custom agents en esta tarea

- **Decision**: Usar los agentes integrados con defaults comunes.
- **Rationale**: Satisface el objetivo con el cambio mínimo y evita mantener roles que
  todavía no tienen requisitos propios.
- **Alternatives considered**: Crear agentes Explorer, Implementer, Tester y Reviewer;
  diferido a otra tarea si se demuestra valor adicional.

## Decision 4: Validación proporcional

- **Decision**: Parser TOML, aserciones de valores, Prettier, `git diff --check` y smoke
  test tras recargar sesión.
- **Rationale**: Son comprobaciones directas y automatizables del artefacto modificado.
- **Alternatives considered**: Añadir una suite de aplicación o E2E; descartado porque
  no existe comportamiento de runtime de XTrace afectado.

## Source

- OpenAI Docs: `https://learn.chatgpt.com/docs/agent-configuration/subagents`
