# 0011. Reutilización del pipeline del spike como dependencia editable

- **Estado**: Aceptada
- **Fecha**: 2026-08-15
- **Spec/Requisitos relacionados**: 002-source-sdk-crawler · FR-011, NFR-001 · relacionada
  con ADR-0003, ADR-0007

## Contexto

El crawler (fase 2) necesita exactamente el pipeline ya construido y validado en el spike:
pHash (ADR-0005), `EmbeddingProvider`/SigLIP y `VectorStore`/pgvector (ADR-0007), ranking y
exclusión. Duplicar ese código violaría trazabilidad y mantenibilidad; reescribirlo
desperdiciaría lo validado con el dataset real del operador.

## Decisión

`services/crawler/` depende de **`xtrace_spike` como dependencia de camino editable**
(`[tool.uv.sources] xtrace-spike = { path = "../search-spike", editable = true }`) y
**reutiliza** sus módulos de hashing, embeddings, vector store y ranking **sin modificar
el paquete del spike**. El crawler añade solo lo nuevo: adapters, jobs, rate limits,
descarga de assets y su propio `repo` (sources/jobs/videos-web).

Si en el futuro un tercer servicio necesita lo mismo, se reevalúa extraer un paquete
compartido (p. ej. `packages/xtrace-core`); no se hace ahora para no reestructurar el
spike cerrado y mergeado.

## Alternativas consideradas

- **Copiar el código al crawler** — duplicación, divergencia de comportamiento y doble
  mantenimiento de pHash/embeddings/ANN. Rechazada.
- **Extraer ya un paquete compartido `packages/`** — más puro a largo plazo, pero toca el
  servicio del spike (cerrado, con 19 ramas de PR preservadas) sin necesidad inmediata y
  añade fricción de empaquetado en el MVP. Diferida con criterio claro.
- **Reimplementar el pipeline en el crawler** — coste y riesgo innecesarios; pierde la
  validación del spike. Rechazada.

## Consecuencias

- (+) Un solo pipeline visual (mismas firmas pHash/embeddings → mismo índice), FR-011
  cumplido por construcción.
- (+) El spike permanece intocado; sus PRs cerrados no se reabren.
- (−) Acoplamiento de paquete entre servicios: cualquier cambio necesario en el spike debe
  ser un PR propio trazado a esta spec (frontera documentada en `contracts/README.md` §6).
