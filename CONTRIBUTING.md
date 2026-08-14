# Contribuir

Este proyecto sigue **Spec-Driven Development** y un **contrato multiagente**. Antes de
tocar nada, lee `AGENTS.md` y `.specify/memory/constitution.md`.

## Reglas de oro

- **Spec-first**: nada se implementa sin una spec `APPROVED`. Solo el humano aprueba, con
  la frase exacta `Especificación aprobada`.
- **Una feature = una spec + una rama + un PR.** Sin push directo a `main`.
- **Revisión independiente**: el implementador no aprueba su propio trabajo; idealmente
  el revisor usa otro proveedor/modelo.
- **Trazabilidad**: todo cambio se relaciona con requisito(s) `FR/NFR/SEC/DATA/UX`, tests
  y PR.

## Flujo por feature

```text
specify → clarify → checklist → aprobación → plan → tasks → analyze
→ git (issue/rama/PR draft) → implementación → converge → revisión → PR
```

## Ramas

```text
feature/NNN-feature-name
fix/NNN-description
chore/000-platform-description
```

## Antes de abrir/actualizar un PR

```bash
pnpm verify   # format:check, lint, typecheck, test, test:db, test:e2e, build
```

Completa la plantilla de PR (incluida trazabilidad, seguridad y evidencias). El PR no se
fusiona con CI en rojo. Deja un handoff en `docs/handoffs/`.

## Tests

- E2E **solo** con WebdriverIO (`*.e2e.ts`, Chrome, headless en CI).
- Todo bug corregido incluye test de regresión.
- No debilitar ni omitir tests para conseguir CI verde.

## Seguridad

- Sin secretos en el repo. `service_role` solo en servidor. RLS por defecto con tests +/-.
