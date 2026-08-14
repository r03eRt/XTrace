---
name: "wdio-e2e"
description: "Crea y mantiene tests E2E con WebdriverIO (TypeScript, Chrome, headless en CI). Prohibido Playwright/Cypress."
---

# Skill: wdio-e2e

## Propósito

Cubrir flujos críticos con tests E2E fiables usando WebdriverIO.

## Cuándo activarse

Cuando una tarea afecta un flujo crítico de usuario o lo exige el plan.

## Archivos que debe leer

`wdio.conf.ts`, `plan.md`, la spec, `tests/e2e/`.

## Normas obligatorias

- TypeScript. Archivos `*.e2e.ts`. Chrome. Headless en CI.
- Page Objects **solo** cuando reduzcan duplicación real.
- Selectores estables: `data-testid` o semántica accesible. Nunca clases visuales frágiles.
- Esperas explícitas por estados observables. **Prohibido** `browser.pause()` para
  sincronización (solo depuración temporal que no llega al commit).
- Captura de pantalla y logs del navegador en fallo.
- Datos aislados por test (cada test prepara y limpia). Sin datos de producción.
- Suite `smoke` para PR; suite completa para `main` o ejecución programada.

## Estructura

```text
tests/e2e/
├── specs/         # *.e2e.ts
├── pages/         # Page Objects (solo si aportan)
├── components/
├── fixtures/
└── helpers/
```

## Scripts

```json
"test:e2e": "wdio run ./wdio.conf.ts",
"test:e2e:smoke": "wdio run ./wdio.conf.ts --suite smoke",
"test:e2e:headed": "WDIO_HEADLESS=false wdio run ./wdio.conf.ts"
```

## Resultados esperados

Tests deterministas, con evidencias en fallo, integrados en `e2e.yml`.

## Condiciones de bloqueo

El entorno no puede levantar app + Supabase local, o faltan `data-testid` estables.

## Formato de salida

Nuevos `*.e2e.ts` + Page Objects/helpers cuando aporten.
