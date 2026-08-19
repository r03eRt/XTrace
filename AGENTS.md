# AGENTS.md — Contrato universal para agentes

> **Léeme primero.** Este archivo es el contrato que **todo** agente (Codex, Claude,
> ChatGPT, DeepSeek, Qwen y cualquier agente de VS Code) debe leer antes de tocar el
> repositorio. El nombre del proveedor no cambia el proceso.

## 0. Al clonar este repositorio (bootstrap) — PRIMER PASO OBLIGATORIO

> **Este es el primer paso al abrir el repo: iniciar el descubrimiento.**

La foundation técnica (`specs/000-platform-foundation`) ya está `IMPLEMENTED`, así que
**el disparador del arranque es `docs/PRODUCT_IDEA.md`**:

- Si `docs/PRODUCT_IDEA.md` está en `PENDIENTE_DE_DESCUBRIMIENTO` (o no existe una spec
  de producto aprobada), el **primer agente** que empiece a trabajar **debe iniciar la
  entrevista de descubrimiento** cargando la skill
  [`project-discovery`](.agents/skills/project-discovery/SKILL.md) **antes de cualquier
  otra cosa**: máximo 5 preguntas por ronda, tratando cualquier descripción como idea
  incompleta y sin convertir supuestos en requisitos sin marcarlos.
- No implementes funcionalidad de negocio hasta que exista una spec de producto
  `APPROVED`.

Prompts listos para copiar (nueva aplicación / nueva feature): ver
[`docs/USAGE.md`](docs/USAGE.md).

## 1. Fuente de verdad (orden canónico)

```text
AGENTS.md                          ← este contrato
.specify/memory/constitution.md    ← reglas no negociables
.agents/skills/                    ← skills compartidas
specs/                             ← specs por feature
```

Ante conflicto, gana la constitución. Ante duda funcional, gana la spec aprobada.

## 2. Flujo Spec Kit

```text
constitution → specify → clarify → checklist → APROBACIÓN HUMANA
→ plan → tasks → analyze → implementación → converge → revisión → PR
```

Comandos por agente:

| Agente         | Mecanismo                                   |
| -------------- | ------------------------------------------- |
| Codex          | Skills `$speckit-*` en `.agents/skills`     |
| Claude / otros | Comandos `/speckit.*` o skills equivalentes |

## 3. Estados de las specs

`DRAFT` → `CLARIFICATION_REQUIRED` → `READY_FOR_REVIEW` → `APPROVED` → `IMPLEMENTING`
→ `IMPLEMENTED` → `DEPRECATED`. Solo el humano aprueba, con la frase exacta
**`Especificación aprobada`**.

## 4. Reglas para tomar una tarea (preflight)

Antes de modificar archivos, un agente **debe**:

1. Leer `AGENTS.md`. 2. Leer la constitución. 3. Cargar las skills aplicables.
2. Leer `spec.md`, `plan.md`, `tasks.md`. 5. Identificar requisitos y criterios.
3. Confirmar spec `APPROVED`. 7. Confirmar tarea en estado `READY`.
4. Confirmar dependencias completadas. 9. Confirmar que nadie edita los mismos archivos.
5. Confirmar rama/worktree asignados.

Si algo falla → detente y marca la tarea como `BLOCKED`.

## 5. Convenciones Git

```text
feature/NNN-feature-name
fix/NNN-description
chore/000-platform-description
```

- Rama y PR independientes por feature. Sin push directo a `main`.
- Commits trazables al requisito/tarea. PR draft desde el inicio.

## 6. Comandos de validación

```bash
pnpm format:check && pnpm lint && pnpm typecheck \
  && pnpm test && pnpm test:db && pnpm test:e2e && pnpm build
pnpm verify   # todo lo anterior en orden
```

## 7. Skills disponibles (`.agents/skills/`)

| Skill                | Responsabilidad                                |
| -------------------- | ---------------------------------------------- |
| `project-discovery`  | Entrevistar y convertir la idea en requisitos  |
| `spec-authoring`     | Generar/actualizar specs (Spec Kit)            |
| `spec-clarification` | Rondas de aclaración (máx. 5 preguntas/ronda)  |
| `technical-planning` | Plan técnico, arquitectura, ADR                |
| `task-planning`      | Tareas pequeñas, ordenadas y trazables         |
| `task-execution`     | Ejecutar el flujo obligatorio de cada tarea    |
| `task-review`        | Revisión independiente (otro agente/proveedor) |
| `wdio-e2e`           | Tests E2E WebdriverIO                          |
| `supabase-database`  | Migraciones, RLS, seeds, tipos, tests de BD    |
| `security-review`    | Revisión de seguridad                          |
| `pr-finalization`    | Validación final, evidencias, trazabilidad, PR |

Las skills `$speckit-*` (specify, plan, tasks, analyze, clarify, checklist,
implement, converge…) están en `.agents/skills` y ejecutan el flujo Spec Kit.

## 8. Archivos que cada rol puede modificar

| Rol                   | Puede modificar                               |
| --------------------- | --------------------------------------------- |
| Orquestador           | `tasks.md`, contratos de tarea, estado global |
| Analista de producto  | `spec.md`, `docs/` de requisitos              |
| Arquitecto            | `plan.md`, `docs/adr/`, `docs/architecture/`  |
| Implementador         | Solo `allowed_paths` de su contrato de tarea  |
| Especialista de datos | `supabase/`, tests de BD                      |
| QA                    | `tests/`                                      |
| Revisor               | Solo comentarios/handoff (no implementa)      |
| Finalizador de PR     | PR, evidencias, trazabilidad                  |

Dos agentes **nunca** editan los mismos archivos a la vez. Solo el orquestador toca
`tasks.md`.

## 9. Condiciones de bloqueo

Detente y solicita resolución si: la spec no está aprobada · hay requisitos
contradictorios · falta una decisión funcional · el plan contradice la spec · una
migración puede destruir datos · faltan autorización o secretos · otra tarea edita
los mismos archivos · los tests no pueden ejecutarse · el cambio excede el alcance ·
la constitución entra en conflicto con la petición. No improvises en silencio.

## 10. Formato de handoff

Cada tarea produce `docs/handoffs/TASK-ID.md` con: resumen · requisitos implementados ·
archivos modificados · decisiones · tests añadidos · comandos ejecutados · resultados ·
limitaciones · riesgos · trabajo pendiente · instrucciones para el revisor.

## 11. Definición de Ready

Una tarea es `READY` cuando: spec aprobada · plan existe · criterios verificables ·
dependencias resueltas · archivos previstos identificados · tests definidos · sin
preguntas funcionales abiertas · rama/worktree existe · agente asignado.

## 12. Definición de Done

Una tarea es `DONE` cuando: cumple spec y criterios · tests y build pasan ·
migraciones verificadas · seguridad revisada · handoff existe · revisión independiente
realizada · comentarios resueltos · trazabilidad actualizada · nada sin documentar.

Una feature se completa cuando: todas sus tareas `DONE` · Spec Kit _analyze_ sin
bloqueos · _converge_ convergido · CI verde · Preview revisada · PR aprobado.

<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
