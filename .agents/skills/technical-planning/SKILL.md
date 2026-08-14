---
name: "technical-planning"
description: "Transforma una spec APPROVED en arquitectura, modelo de datos, contratos y ADR. No altera requisitos funcionales."
---

# Skill: technical-planning

## Propósito

Definir **cómo** se implementará una spec ya aprobada.

## Cuándo activarse

Solo cuando la spec está en estado `APPROVED`.

## Entradas necesarias

- Spec aprobada y su lista de requisitos.

## Archivos que debe leer

- `spec.md`, `AGENTS.md`, `.specify/memory/constitution.md`,
  `.specify/templates/plan-template.md`, `docs/adr/`, `docs/architecture/`.

## Pasos obligatorios

Producir `plan.md` con: arquitectura · modelo de datos · contratos/APIs · componentes ·
estrategia de seguridad (RLS incluida) · estrategia de tests (unit, componentes, BD, E2E)
· estrategia de despliegue (Vercel/Preview) · observabilidad · riesgos · ADR necesarios
en `docs/adr/`.

## Prohibido

Alterar requisitos funcionales. Si el plan contradice la spec → bloquear.

## Resultados esperados

`plan.md` completo + ADRs relevantes. Stack fijado: Next.js (App Router), TS estricto,
pnpm, Supabase, Docker local, Vitest, Testing Library, WebdriverIO, GitHub Actions, Vercel.

## Comprobaciones

- Cada requisito de la spec tiene cobertura en el plan.
- Respeta las fronteras servidor/cliente y la separación de entornos.

## Condiciones de bloqueo

Requisito ambiguo, contradicción con la spec, o decisión de negocio pendiente.

## Formato de salida

`plan.md` conforme a la plantilla + entradas en `docs/adr/NNNN-*.md`.
