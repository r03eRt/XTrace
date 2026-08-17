# Feature Specification: Configuración de subagentes Codex

**Feature Branch**: `chore/000-codex-subagents`

**Created**: 2026-08-17

**Status**: IMPLEMENTED

**Input**: User description: "Versionar en XTrace la configuración de proyecto que
mantiene GPT-5.6 Sol Medium como orquestador y usa GPT-5.6 Luna Max para subagentes."

## Objetivo

Hacer reproducible para cualquier colaborador de XTrace la política de orquestación
multiagente acordada, sin depender de la configuración personal de una máquina.

## Alcance

- Activar los flujos multiagente en la configuración propia del repositorio.
- Limitar a tres los subagentes concurrentes, además del agente principal.
- Establecer GPT-5.6 Luna con razonamiento Max como valor por defecto de los subagentes.
- Mantener sin cambios la selección del modelo y razonamiento del agente principal.
- Documentar la configuración como parte versionada del proyecto.

## Fuera de alcance

- Cambios en la lógica de negocio, frontend, API, base de datos o crawler de XTrace.
- Creación de agentes personalizados por rol en esta tarea.
- Modificación de la configuración global o personal de Codex.
- Cambios en specs de producto, corpus, indexación o política de ocho frames.
- Ejecución de reindexados, descargas o servicios locales.

## Actores

- **Orquestador principal**: conserva el modelo y nivel de razonamiento elegidos para la
  sesión principal y delega trabajo acotado.
- **Subagente**: ejecuta tareas delegadas con los valores predeterminados del proyecto.
- **Colaborador**: abre XTrace en Codex y recibe la misma política multiagente versionada.

## User Scenarios & Testing

### User Story 1 - Delegación reproducible (Priority: P1)

Como colaborador de XTrace, quiero que los subagentes usen Luna Max por defecto para
obtener una política de coste y capacidad consistente sin configurar cada máquina.

**Why this priority**: Es el objetivo principal del cambio y evita divergencias entre
sesiones y colaboradores.

**Independent Test**: Abrir una sesión nueva y lanzar un subagente sin indicar modelo ni
razonamiento; la sesión delegada debe utilizar los valores predeterminados del proyecto.

**Acceptance Scenarios**:

1. **Given** una sesión nueva abierta en XTrace, **When** el orquestador lanza un
   subagente sin overrides, **Then** el subagente usa GPT-5.6 Luna con razonamiento Max.
2. **Given** una tarea paralelizable, **When** se lanzan tres subagentes concurrentes,
   **Then** los tres pueden ejecutarse junto al orquestador principal.
3. **Given** tres subagentes ya activos, **When** se intenta superar el límite del
   proyecto, **Then** Codex respeta el máximo configurado.

### User Story 2 - Preservar el orquestador (Priority: P1)

Como responsable del proyecto, quiero que la política de subagentes no cambie el modelo
ni el razonamiento elegidos para el agente principal.

**Why this priority**: La separación Sol Medium/Luna Max es una decisión explícita del
usuario y evita elevar innecesariamente el coste del orquestador.

**Independent Test**: Revisar la configuración del proyecto y abrir una sesión nueva;
la configuración de subagentes no debe establecer ni sustituir los valores del agente
principal.

**Acceptance Scenarios**:

1. **Given** una sesión principal configurada como GPT-5.6 Sol Medium, **When** se carga
   la configuración del proyecto, **Then** la sesión principal conserva esos valores.
2. **Given** un lanzamiento con overrides explícitos permitidos, **When** el orquestador
   los proporciona, **Then** los overrides prevalecen sobre los defaults del proyecto.

### Edge Cases

- Una sesión ya abierta puede requerir reinicio o una sesión nueva para recargar la
  configuración versionada.
- Un colaborador sin acceso a GPT-5.6 Luna debe recibir el error de disponibilidad de
  Codex; no se sustituirá silenciosamente por otro modelo.
- La ausencia de agentes personalizados no debe impedir el uso de los agentes integrados.
- Los cambios personales de Codex fuera del repositorio quedan fuera de esta política.

## Requirements

### Functional Requirements

- **FR-001**: El proyecto MUST habilitar las herramientas multiagente.
- **FR-002**: El proyecto MUST limitar a tres los subagentes concurrentes por sesión,
  sin contar el agente principal.
- **FR-003**: Los subagentes lanzados sin override MUST usar GPT-5.6 Luna por defecto.
- **FR-004**: Los subagentes lanzados sin override MUST usar razonamiento Max por defecto.
- **FR-005**: La configuración del proyecto MUST NOT fijar ni sustituir el modelo o el
  razonamiento del agente principal.
- **FR-006**: El proyecto MUST NOT sustituir silenciosamente Luna por Terra u otro modelo
  cuando Luna no esté disponible.
- **FR-007**: Los overrides explícitos de una delegación MUST conservar la precedencia
  definida por Codex sobre los valores predeterminados del proyecto.

### Non-Functional Requirements

- **NFR-001**: La configuración MUST ser válida para una versión actual de Codex que
  soporte los campos de subagentes acordados.
- **NFR-002**: El cambio MUST quedar limitado a configuración y documentación de proceso;
  no puede alterar el comportamiento del producto XTrace.
- **NFR-003**: La configuración MUST ser revisable y reproducible desde el repositorio,
  sin secretos ni valores específicos de una máquina.

## Success Criteria

### Measurable Outcomes

- **SC-001**: El 100 % de los subagentes lanzados sin overrides en una sesión nueva de
  XTrace seleccionan GPT-5.6 Luna y razonamiento Max cuando el modelo está disponible.
- **SC-002**: Pueden coexistir un orquestador principal y hasta tres subagentes activos.
- **SC-003**: Cero archivos de producto, base de datos, tests funcionales o corpus cambian
  como consecuencia de esta tarea.
- **SC-004**: Una validación sintáctica de la configuración termina sin errores.

## Dependencies

- Una versión de Codex compatible con configuración de subagentes a nivel de proyecto.
- Acceso de la cuenta o workspace a GPT-5.6 Luna y al nivel de razonamiento Max.
- Reapertura de la sesión cuando Codex necesite recargar configuración.

## Risks

- La disponibilidad de modelos puede variar por cuenta o política de workspace.
- Una sesión iniciada antes del cambio puede conservar configuración anterior.
- Niveles altos de razonamiento pueden aumentar tiempo y consumo en tareas sencillas.

## Assumptions

- El agente principal seguirá configurándose fuera de esta spec como GPT-5.6 Sol Medium.
- El límite total disponible es de cuatro hilos: un orquestador y tres subagentes.
- Los agentes integrados de Codex son suficientes; los agentes personalizados se
  evaluarán en una tarea separada si aportan valor.

## Approval

- **Current state**: `IMPLEMENTED`
- **Approved by**: humano responsable
- **Approval date**: 2026-08-17
- **Approval evidence**: frase exacta `Especificación aprobada`
