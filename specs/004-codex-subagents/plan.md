# Implementation Plan: Configuración de subagentes Codex

**Branch**: `chore/000-codex-subagents` | **Date**: 2026-08-17 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-codex-subagents/spec.md`

## Summary

Versionar una configuración de proyecto mínima para que Codex habilite flujos
multiagente, permita hasta tres subagentes concurrentes y use GPT-5.6 Luna con
razonamiento Max cuando una delegación no incluya overrides. La configuración no fijará
el modelo ni el razonamiento del agente principal.

## Technical Context

**Language/Version**: TOML 1.0 para configuración; Markdown para trazabilidad

**Primary Dependencies**: Codex con soporte para configuración de proyecto y subagentes

**Storage**: Archivo versionado en el repositorio; sin datos persistentes de producto

**Testing**: Parser TOML de Python 3.11+ (`tomllib`), Prettier para Markdown y prueba de
delegación en una sesión nueva

**Target Platform**: Clientes locales de Codex que confían en este repositorio

**Project Type**: Configuración interna de herramientas de desarrollo

**Performance Goals**: Sin impacto en runtime de XTrace; máximo de tres subagentes
concurrentes además del orquestador

**Constraints**: No modificar la configuración del agente principal, no incluir secretos,
no tocar aplicación, base de datos, corpus ni servicios locales

**Scale/Scope**: Un archivo de configuración y artefactos Spec Kit asociados

## Architecture

La solución añade una única capa de configuración local al repositorio. Codex combina
esta capa con la configuración personal y los overrides de cada lanzamiento según su
precedencia documentada. La capa del proyecto contiene exclusivamente la tabla de
subagentes y no declara campos globales de modelo o razonamiento, por lo que el
orquestador conserva los valores seleccionados para su sesión.

No se crean agentes personalizados: los agentes integrados heredan el modelo y el nivel
de razonamiento predeterminados de la tabla de subagentes.

## Components

- **Configuración del proyecto**: declara activación, concurrencia y defaults de los
  subagentes.
- **Codex local**: carga la configuración al abrir una sesión nueva y aplica su
  precedencia.
- **Validación estática**: parsea TOML, verifica valores exactos y confirma que no se
  declaran defaults del agente principal.
- **Validación funcional**: una sesión nueva puede lanzar un subagente sin overrides y
  comprobar la selección Luna Max.

## Data Model

No existen entidades de dominio ni persistencia. El único estado es una configuración
de cuatro propiedades escalares descrita en [data-model.md](data-model.md).

## Contracts and Interfaces

No se añaden APIs, endpoints, UI, comandos públicos ni contratos de datos. El contrato
externo es el esquema de configuración publicado por Codex; no se crea un directorio
`contracts/` porque el cambio es interno.

## Security

- No se almacenan secretos, tokens, rutas personales ni variables de entorno.
- Los subagentes heredan el sandbox y permisos de la sesión principal.
- La configuración no amplía permisos ni habilita servicios de red.
- RLS, autenticación y Supabase no aplican porque no existe acceso a datos.

## Test Strategy

1. Parsear `.codex/config.toml` con un parser TOML estándar.
2. Verificar los cuatro valores requeridos y sus tipos.
3. Verificar la ausencia de campos raíz `model` y `model_reasoning_effort`.
4. Ejecutar Prettier sobre los artefactos Markdown/JSON de la feature.
5. Ejecutar `git diff --check` para detectar errores de whitespace.
6. Tras recargar Codex, lanzar un smoke test de subagente sin overrides.

No se requieren tests unitarios, de componentes, base de datos, E2E ni build porque la
feature no modifica ningún runtime de XTrace. `pnpm verify` se ejecutará y cualquier
fallo preexistente se distinguirá de este cambio.

## Deployment and Rollback

No hay despliegue de aplicación ni Preview de Vercel. La configuración se activa cuando
un cliente Codex abre o recarga una sesión en el repositorio. El rollback consiste en
revertir el archivo de configuración versionado.

## Observability

La actividad de los subagentes se observa mediante el panel de subagentes de Codex. No
se añaden logs ni telemetría a XTrace.

## Risks

- La disponibilidad de GPT-5.6 Luna o Max depende de la cuenta y el workspace.
- Una sesión abierta antes del cambio puede mantener la configuración previa.
- Razonamiento Max aumenta consumo y latencia frente a niveles inferiores.
- Un override explícito puede seleccionar otro modelo por diseño.

## Constitution Check

### Pre-design

- **Spec-first**: PASS; la spec 004 está `APPROVED` con evidencia literal.
- **Trazabilidad**: PASS; FR-001..FR-007, NFR-001..NFR-003 y SC-001..SC-004 se mapearán
  a una tarea y validaciones explícitas.
- **Rama independiente**: PASS; `chore/000-codex-subagents`.
- **Multiagente**: PASS; un único implementador evitará conflictos de archivos.
- **Testing**: PASS; se usa validación automatizada equivalente para configuración.
- **Seguridad**: PASS; sin secretos, datos, Supabase ni cambios de permisos.
- **Calidad**: PASS condicionado a ejecutar las validaciones definidas y documentar
  cualquier fallo preexistente de `pnpm verify`.

### Post-design

Todos los gates continúan en PASS. El diseño no altera requisitos ni introduce
excepciones constitucionales.

## Project Structure

### Documentation

```text
specs/004-codex-subagents/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── tasks.md
├── checklists/requirements.md
└── contracts/TASK-004-001.md
```

### Implementation

```text
.codex/
└── config.toml

docs/handoffs/
└── TASK-004-001.md
```

**Structure Decision**: Mantener la configuración en el scope del proyecto y toda la
trazabilidad dentro de la feature Spec Kit y el handoff obligatorio.

## Complexity Tracking

No existen violaciones constitucionales ni complejidad adicional que justificar.
