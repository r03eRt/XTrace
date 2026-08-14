# Skills compartidas

Skills del proyecto (contrato multiagente). Cada agente debe cargar las aplicables
antes de modificar archivos. Ver `AGENTS.md` §7.

## Skills de proceso Spec Kit (`$speckit-*`)

Instaladas por Spec Kit: `speckit-constitution`, `speckit-specify`, `speckit-clarify`,
`speckit-checklist`, `speckit-plan`, `speckit-tasks`, `speckit-analyze`,
`speckit-implement`, `speckit-converge`, `speckit-taskstoissues`.

## Skills de dominio del proyecto

| Skill                | Rol                        |
| -------------------- | -------------------------- |
| `project-discovery`  | Entrevista y requisitos    |
| `spec-authoring`     | Autoría de specs           |
| `spec-clarification` | Rondas de aclaración       |
| `technical-planning` | Plan técnico y ADR         |
| `task-planning`      | Tareas trazables           |
| `task-execution`     | Flujo obligatorio de tarea |
| `task-review`        | Revisión independiente     |
| `wdio-e2e`           | E2E WebdriverIO            |
| `supabase-database`  | BD, RLS, migraciones       |
| `security-review`    | Seguridad                  |
| `pr-finalization`    | Cierre de PR               |

> Seguridad: esta carpeta puede contener credenciales/tokens de algunos agentes.
> Revisa `.gitignore` para no filtrar secretos.
