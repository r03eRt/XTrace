# Feature Specification: Platform Foundation (Esqueleto técnico)

**Feature Branch**: `chore/000-platform-foundation`

**Created**: 2026-08-05

**Status**: IMPLEMENTED

**Input**: Estandarizar el esqueleto técnico reutilizable para desarrollo Spec-Driven
multiagente. **No** incluye funcionalidades de producto.

> ✅ **Aprobada por el humano responsable el 2026-08-05** (aprobación explícita: "lo apruebo").
> Estado: `APPROVED` → `IMPLEMENTING` → `IMPLEMENTED`.
> Todas las puertas verificadas en local: `format:check`, `lint`, `typecheck`, `test`
> (3/3), `build`, `test:db` (pgTAP 4/4) y `test:e2e:smoke` (Chrome 150, 1/1).

## Objetivo

Disponer de una base reutilizable que permita desarrollar cualquier aplicación mediante
Spec-Driven Development con Spec Kit, agentes de distintos proveedores y skills
compartidas, con puertas de calidad automatizadas.

## Alcance

- Aplicación Next.js (App Router) con TypeScript estricto.
- Gestor de paquetes pnpm.
- Supabase (BD, auth, almacenamiento) con desarrollo local vía Supabase CLI + Docker.
- Tests: Vitest (unit), Testing Library (componentes), WebdriverIO (E2E, Chrome).
- CI: GitHub Actions (quality, e2e, spec-compliance, security).
- Despliegue: Vercel (Preview por rama/PR, producción desde `main`).
- Gestión de variables de entorno con validación explícita.
- Sistema de skills (`.agents/skills`) y contrato multiagente (`AGENTS.md`).
- Trazabilidad y plantilla de PR.

## Fuera de alcance

- Cualquier funcionalidad de negocio o dominio del producto.
- Elección del modelo de datos del producto (se define por feature).
- Integraciones concretas de terceros no requeridas por el esqueleto.

## Actores

- **Humano responsable**: aprueba specs y merges.
- **Orquestador**: asigna tareas y mantiene el estado.
- **Agentes trabajadores**: implementan tareas (cualquier proveedor).
- **Revisor**: agente distinto al implementador.

## Historias de usuario

### US1 — Arranque reproducible (P1)

Como desarrollador, quiero clonar el repo y levantar el entorno con comandos
estándar para empezar a trabajar sin configuración manual frágil.

**Acceptance Scenarios**

1. **Given** el repo clonado, **When** ejecuto `pnpm install && pnpm dev`, **Then** la
   app arranca en `http://localhost:3000`.
2. **Given** Docker disponible, **When** ejecuto `docker compose up --build`, **Then**
   obtengo un entorno de desarrollo equivalente.

### US2 — Puertas de calidad (P1)

Como responsable, quiero que ningún PR se fusione con CI fallido.

**Acceptance Scenarios**

1. **Given** un PR, **When** falla lint/typecheck/test/build, **Then** el merge queda bloqueado.
2. **Given** un PR, **When** falta la spec asociada o su estado no es válido, **Then**
   `spec-compliance` falla.

### US3 — Contrato multiagente (P1)

Como agente de cualquier proveedor, quiero una fuente de verdad única para actuar igual.

**Acceptance Scenarios**

1. **Given** un agente nuevo, **When** lee `AGENTS.md` y la constitución, **Then**
   conoce flujo, estados, comandos y condiciones de bloqueo.

### US4 — Descubrimiento al clonar (P2)

Como usuario, quiero que la entrevista de descubrimiento se inicie al empezar a trabajar
sobre un repo sin idea definida.

**Acceptance Scenarios**

1. **Given** `docs/PRODUCT_IDEA.md` en `PENDIENTE_DE_DESCUBRIMIENTO`, **When** un agente
   empieza, **Then** carga `project-discovery` y hace la primera ronda de preguntas.

## Flujos principales

`constitution → specify → clarify → checklist → aprobación → plan → tasks → analyze →
implementación → converge → revisión → PR`.

## Reglas de negocio (del esqueleto)

- Spec-first, aprobación humana, trazabilidad, un PR por feature, sin push a `main`.
- E2E solo con WebdriverIO; RLS habilitado por defecto; sin secretos en el repo.

## Requisitos

### Funcionales

- **FR-001**: El repo DEBE proveer `AGENTS.md` como contrato universal para todos los agentes.
- **FR-002**: El repo DEBE incluir la constitución en `.specify/memory/constitution.md`.
- **FR-003**: El repo DEBE incluir las 11 skills de dominio en `.agents/skills/`.
- **FR-004**: El proyecto DEBE exponer los scripts `format:check, lint, typecheck, test,
test:db, test:e2e, build, verify` y `verify` DEBE ejecutarlos en orden.
- **FR-005**: CI DEBE bloquear el merge ante fallo en `quality`, `e2e`, `spec-compliance` o `security`.
- **FR-006**: E2E DEBE ejecutarse con WebdriverIO en Chrome y headless en CI, con sufijo `.e2e.ts`.
- **FR-007**: Supabase local DEBE gestionarse por CLI con `supabase:start/stop/reset/types`.
- **FR-008**: La app DEBE fallar de forma explícita si falta una variable de entorno obligatoria.
- **FR-009**: DEBE existir plantilla de PR con trazabilidad Requisito→Implementación→Test.
- **FR-010**: DEBE existir un mecanismo de arranque que dispare `project-discovery` al clonar.

### No funcionales

- **NFR-001**: Arranque reproducible (`pnpm dev` y `docker compose up --build`).
- **NFR-002**: Instalación reproducible con lockfile (`--frozen-lockfile`).
- **NFR-003**: Usar siempre la última versión estable compatible; sin beta/canary/RC sin aprobación.

### Seguridad

- **SEC-001**: RLS habilitado por defecto en tablas accesibles desde cliente, con tests +/-.
- **SEC-002**: Sin secretos en repo, imágenes ni Compose; `service_role` solo en servidor.
- **SEC-003**: Entornos local/preview/producción separados; nunca exponer Supabase local.

### Datos

- **DATA-001**: Migraciones versionadas en Git; sin migraciones destructivas sin plan de recuperación.
- **DATA-002**: Tipos TypeScript generados desde el esquema.

### UX

- **UX-001**: Selectores E2E estables (`data-testid` o semántica accesible).

## Casos límite

- Falta Docker o Supabase CLI → mensajes de error claros y documentación de arranque.
- Variables de entorno ausentes → fallo explícito, no comportamiento silencioso.
- Migración destructiva sin `RECOVERY-PLAN` → CI de seguridad falla.

## Criterios Given/When/Then

Ver historias de usuario (US1–US4).

## Dependencias

- Cuentas/CLI: GitHub, Vercel, Supabase CLI, Docker, Node LTS, pnpm.

## Riesgos

- Deriva de versiones de dependencias → mitigar con lockfile y renovaciones controladas.
- Fricción del entorno local (Docker/Supabase) → runbooks y health checks.

## Preguntas pendientes

- **Q1 (RESUELTA)**: Node LTS fijado a **22** (`.nvmrc`, `engines`, Dockerfile), acorde
  al entorno actual.
- **Q2**: ¿`gitleaks` como scanner de secretos o alternativa preferida?
- **Q3 (RESUELTA)**: Smoke E2E contra Preview de Vercel — **NO** se habilita. El smoke se
  ejecuta solo contra la app local en CI (`e2e.yml`).

## Estado de aprobación

`IMPLEMENTING` — aprobada por el humano responsable el 2026-08-05.

## Historial de decisiones

- 2026-08-05: Node LTS fijado a **22**.
- 2026-08-05: Smoke E2E **solo** contra app local en CI (no contra Preview).
- 2026-08-05: Secret scanner = **gitleaks**.
- 2026-08-05: **Aprobación humana explícita** ("lo apruebo") → `APPROVED` → `IMPLEMENTING`.
