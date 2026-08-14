# Cómo usar este esqueleto (prompts listos)

Este repositorio es una base para desarrollo **Spec-Driven multiagente**. Cualquier agente
(Codex, Claude, ChatGPT, DeepSeek, Qwen u otro de VS Code) debe, antes de nada:

1. Leer [`AGENTS.md`](../AGENTS.md).
2. Leer [`.specify/memory/constitution.md`](../.specify/memory/constitution.md).
3. Cargar las skills aplicables de [`.agents/skills/`](../.agents/skills/).

> Regla de oro: **no se implementa código** hasta que la spec esté en estado `APPROVED`.
> Solo el humano aprueba, con la frase exacta: **`Especificación aprobada`**.

---

## 1. Nueva aplicación (descubrimiento)

Al clonar el repo por primera vez, para arrancar una aplicación nueva, pega este prompt:

```text
Inicia el descubrimiento de una nueva aplicación utilizando la skill project-discovery.

Idea inicial:

[DESCRIBIR LA APLICACIÓN]

No implementes código. Hazme un máximo de cinco preguntas por ronda y construye
progresivamente la especificación.
```

Qué ocurre después:

- El agente carga la skill [`project-discovery`](../.agents/skills/project-discovery/SKILL.md)
  y hace **máximo 5 preguntas por ronda**.
- Rellena `docs/PRODUCT_IDEA.md` (sale de `PENDIENTE_DE_DESCUBRIMIENTO`).
- Tras cada ronda muestra: Decisiones confirmadas · Supuestos · Preguntas abiertas ·
  Contradicciones · Recomendación.
- No cierra el descubrimiento mientras haya ambigüedades relevantes.

---

## 2. Nueva feature (flujo Spec Kit)

Para cada nueva funcionalidad, pega este prompt:

```text
Inicia una nueva feature mediante Spec Kit.

Feature:

[DESCRIBIR LA FEATURE]

Sigue obligatoriamente este flujo:

specify → clarify → checklist → aprobación humana → plan → tasks → analyze →
implementación multiagente → revisión independiente → converge → PR.

No implementes nada hasta que la spec esté en estado APPROVED.
Carga AGENTS.md, la constitución y todas las skills aplicables.
```

Qué ocurre después:

- Se crea `specs/NNN-feature-name/spec.md` (qué y por qué, sin decisiones técnicas).
- `clarify` elimina ambigüedades (máx. 5 preguntas/ronda).
- `checklist` valida la calidad de los requisitos.
- **Esperas tú**: escribe `Especificación aprobada` para pasar la spec a `APPROVED`.
- `plan` → `tasks` → `analyze` (sin bloqueos) → issue/rama/PR draft.
- Implementación multiagente (skill `task-execution`), **revisión independiente** por otro
  agente/proveedor (`task-review`), `converge` y cierre de PR (`pr-finalization`).

---

## 3. Recordatorios del flujo

| Fase           | Skill / Comando                           |
| -------------- | ----------------------------------------- |
| Descubrimiento | `project-discovery`                       |
| Especificar    | `spec-authoring` · `$speckit-specify`     |
| Aclarar        | `spec-clarification` · `$speckit-clarify` |
| Checklist      | `$speckit-checklist`                      |
| Planificar     | `technical-planning` · `$speckit-plan`    |
| Tareas         | `task-planning` · `$speckit-tasks`        |
| Analizar       | `$speckit-analyze`                        |
| Implementar    | `task-execution` · `$speckit-implement`   |
| Revisar        | `task-review`                             |
| Converger      | `$speckit-converge`                       |
| Cerrar PR      | `pr-finalization`                         |

Convención de ramas: `feature/NNN-feature-name`, `fix/NNN-description`,
`chore/000-platform-description`. Sin push directo a `main`. Validación previa a PR:
`pnpm verify`.
