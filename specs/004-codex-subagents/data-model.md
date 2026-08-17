# Data Model: Configuración de subagentes Codex

No hay entidades de negocio ni almacenamiento persistente.

## Configuration State

| Property | Type | Required value | Constraint |
| --- | --- | --- | --- |
| Multi-agent enabled | Boolean | `true` | Debe permitir herramientas multiagente |
| Concurrent subagents | Integer | `3` | Excluye al orquestador principal |
| Default subagent model | String | `gpt-5.6-luna` | No se sustituye silenciosamente |
| Default subagent reasoning | String | `max` | Se aplica cuando no hay override |

## Invariants

- La configuración no declara un modelo principal.
- La configuración no declara razonamiento para el agente principal.
- No contiene secretos ni valores específicos de una máquina.
- Los overrides explícitos conservan su precedencia.

## State Transitions

1. **No cargada**: la sesión todavía no ha leído la configuración del proyecto.
2. **Cargada**: una sesión nueva incorpora los defaults del proyecto.
3. **Override por delegación**: una llamada explícita sustituye uno o ambos defaults
   únicamente para el subagente lanzado.
