# 0008. CLI como interfaz de validación del spike; FastAPI y frontend diferidos

- **Estado**: Aceptada
- **Fecha**: 2026-08-14
- **Spec/Requisitos relacionados**: 001-visual-search-spike · FR-017 (Decisión D2)

## Contexto

El objetivo del spike es **validar la búsqueda** al menor coste, no construir producto. Una
API HTTP o un frontend añaden superficie y trabajo sin aportar a la validación de la
hipótesis. El humano decidió (D2) una CLI interna.

## Decisión

La interfaz del spike es una **CLI** (`typer`) con comandos `index`, `search`, `benchmark`,
`exclude` y `stats`. **No** se implementa FastAPI ni frontend en esta feature. FastAPI
(endpoints `/search/*`), frontend Next.js y admin quedan **diferidos** a features
posteriores del MVP.

## Alternativas consideradas

- **FastAPI desde ya** — superficie/seguridad (SSRF, rate limit) y trabajo no necesarios
  para validar. Diferida.
- **Frontend mínimo** — fuera del objetivo del spike (spec §fuera de alcance). Diferido.

## Consecuencias

- (+) Máxima simplicidad y foco; benchmarks reproducibles vía CLI.
- (−) Sin interacción de usuario final aún; aceptable: el spike valida el motor, no la UX.
