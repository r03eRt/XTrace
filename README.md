# Proyect-skeleton

> ## 👉 Primer paso al abrir este repo: **descubrimiento**
>
> Este esqueleto **arranca preguntándote qué quieres construir**. No se escribe
> código hasta tenerlo claro.
>
> **Si eres un agente** (Copilot, Codex, Claude, …): lee `AGENTS.md` §0, carga la
> skill `project-discovery` e **inicia la entrevista** (máx. 5 preguntas por ronda),
> porque `docs/PRODUCT_IDEA.md` está en `PENDIENTE_DE_DESCUBRIMIENTO`.
>
> **Si eres una persona:** pega este prompt para empezar:
>
> ```text
> Inicia el descubrimiento de una nueva aplicación utilizando la skill project-discovery.
>
> Idea inicial:
> [DESCRIBE AQUÍ TU APLICACIÓN]
>
> No implementes código. Hazme un máximo de cinco preguntas por ronda y construye
> progresivamente la especificación. Carga AGENTS.md, la constitución y las skills aplicables.
> ```
>
> Más prompts (nueva feature, etc.) en [`docs/USAGE.md`](docs/USAGE.md).

Esqueleto **reutilizable** para desarrollar aplicaciones mediante **Spec-Driven
Development** (GitHub Spec Kit), con **desarrollo multiagente** (Codex, Claude, ChatGPT,
DeepSeek, Qwen y otros agentes de VS Code) y **skills compartidas**.

> Este repositorio es una base. Aún **no** tiene funcionalidad de producto: la idea se
> define mediante una entrevista de descubrimiento al empezar a trabajar.

## 🚀 Al clonar y empezar

> 📌 **Prompts listos para copiar** en [`docs/USAGE.md`](docs/USAGE.md) (nueva aplicación
> y nueva feature).

1. Lee `AGENTS.md` (contrato universal) y `.specify/memory/constitution.md`.
2. El primer agente carga la skill [`project-discovery`](.agents/skills/project-discovery/SKILL.md)
   e inicia la **entrevista de descubrimiento** (máx. 5 preguntas por ronda), porque
   `docs/PRODUCT_IDEA.md` está en `PENDIENTE_DE_DESCUBRIMIENTO`.
3. No se implementa funcionalidad de negocio hasta que exista una spec `APPROVED`
   (frase exacta: **`Especificación aprobada`**).

## 🧱 Stack

Next.js (App Router) · TypeScript estricto · pnpm · Supabase (BD/auth/almacenamiento) ·
Docker + Supabase CLI (local) · Vercel (deploy) · Vitest · Testing Library ·
WebdriverIO (E2E, Chrome) · GitHub Actions.

## 📁 Estructura

```text
AGENTS.md                     Contrato universal para agentes
CLAUDE.md / GEMINI.md         Adaptadores finos por proveedor
.github/                      Workflows CI, plantillas de PR/issue, copilot-instructions
.agents/skills/               Skills Spec Kit + skills de dominio
.specify/                     Constitución, plantillas, scripts Spec Kit
specs/000-platform-foundation Spec del esqueleto técnico (READY_FOR_REVIEW)
src/                          app / components / features / lib / server / types
tests/                        unit / e2e (WebdriverIO) / fixtures / helpers
supabase/                     config, migraciones, seeds, tests
docs/                         adr / architecture / handoffs / runbooks
scripts/                      verificaciones de workflow, specs, contratos, migraciones
```

## ✅ Comandos de calidad

```bash
pnpm format:check   # formato
pnpm lint           # lint
pnpm typecheck      # tipos
pnpm test           # unit + componentes
pnpm test:db        # tests de base de datos
pnpm test:e2e       # E2E WebdriverIO (Chrome)
pnpm build          # build de producción
pnpm verify         # todo lo anterior en orden
```

> ⚠️ Las dependencias se instalan durante la **implementación** de
> `000-platform-foundation`, tras su aprobación. Hasta entonces `package.json`
> declara los scripts pero no fija dependencias.

## 🔁 Flujo por feature

```text
specify → clarify → checklist → aprobación → plan → tasks → analyze
→ git (issue/rama/PR draft) → implementación → converge → revisión → PR
```

Cada feature: su spec, su rama (`feature/NNN-*`) y su PR. Sin push directo a `main`.

## 🤖 Multiagente

- Fuente de verdad: `AGENTS.md` + constitución + skills + specs.
- La revisión la hace un agente **distinto** al implementador (idealmente otro proveedor).
- Cada tarea deja un handoff en `docs/handoffs/`.

Consulta `AGENTS.md` para el detalle de preflight, estados, bloqueos, Ready y Done.
