# 0003. Servicio Python para indexación y búsqueda visual junto al skeleton Next.js

- **Estado**: Aceptada
- **Fecha**: 2026-08-14
- **Spec/Requisitos relacionados**: 001-visual-search-spike · FR-001..FR-018, NFR coste

## Contexto

El skeleton es Next.js/TypeScript. El pipeline visual del spike (FFmpeg, perceptual
hashing, embeddings SigLIP, pgvector) tiene un ecosistema maduro en **Python** y el prompt
maestro fija Python/FastAPI para búsqueda y Python para crawler. El spike necesita una CLI
de indexación/búsqueda/benchmark, no una UI.

## Decisión

Añadir un **servicio Python** en `services/search-spike/` (paquete `xtrace_spike`, CLI
Typer), aislado del app Next.js del skeleton, que **no** se modifica en esta feature. La
base de datos se comparte mediante `supabase/migrations`. Herramientas Python: `ruff`
(lint/format), `mypy` (typecheck), `pytest` (tests). Se añade un job de CI dedicado sin
alterar la pipeline JS.

## Alternativas consideradas

- **Implementar en TypeScript/Node** — ecosistema pobre para embeddings/FFmpeg/pgvector;
  mayor fricción y coste de desarrollo. Rechazada.
- **Repositorio separado** — rompe trazabilidad, monorepo y flujo multiagente del skeleton.
  Rechazada.

## Consecuencias

- (+) Ecosistema idóneo, alineado con crawler/embedding workers futuros.
- (+) Aislamiento: la feature no toca el frontend ni su CI.
- (−) Dos toolchains (JS + Python) y un job de CI adicional; se documenta y automatiza.
