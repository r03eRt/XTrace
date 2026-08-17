# Tasks: Configuración de subagentes Codex

**Input**: Design documents from `specs/004-codex-subagents/`

**Prerequisites**: `spec.md` APPROVED, `plan.md`, `research.md`, `data-model.md`,
`quickstart.md`

**Organization**: Una única tarea atómica cubre las dos historias porque ambas dependen
del mismo archivo y deben verificarse juntas.

## Phase 1: Project-scoped orchestration policy

**Goal**: Versionar los defaults de subagente sin fijar el orquestador principal.

**Independent Test**: Parsear la configuración, comprobar los cuatro valores acordados
y verificar la ausencia de defaults del agente principal.

- [x] T001 [US1] Implementar y validar la política completa de US1 y US2 conforme al contrato `specs/004-codex-subagents/contracts/TASK-004-001.md`, creando `.codex/config.toml` y `docs/handoffs/TASK-004-001.md`

## Dependencies & Execution Order

- T001 no tiene dependencias pendientes: la spec está aprobada, el plan existe, la rama
  está creada y los archivos permitidos están identificados.
- No hay oportunidades de paralelización: implementación, validación y handoff forman
  una única unidad y comparten el mismo contrato.

## Requirement Coverage

| Task | Requirements | Success criteria |
| --- | --- | --- |
| T001 | FR-001..FR-007, NFR-001..NFR-003 | SC-001..SC-004 |

## Implementation Strategy

1. Ejecutar el preflight de `task-execution`.
2. Validar que la configuración todavía no existe o no satisface el contrato.
3. Crear el cambio mínimo.
4. Ejecutar validación TOML, formato, diff y `pnpm verify`.
5. Crear handoff y solicitar revisión independiente.
6. Resolver hallazgos, revalidar y marcar T001 `DONE` desde el orquestador.

## Task Summary

- **Total tasks**: 1
- **US1**: 1 tarea, cubre también US2 porque comparte el mismo artefacto
- **Parallel tasks**: 0
- **Suggested MVP**: T001 completa
