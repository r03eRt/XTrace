# 0005. Matching doble: pHash (near-exact) + embeddings visuales SigLIP (semántico)

- **Estado**: Aceptada
- **Fecha**: 2026-08-14
- **Spec/Requisitos relacionados**: 001-visual-search-spike · FR-003, FR-004, FR-005, FR-013, SC-001, SC-002

## Contexto

Las consultas incluyen variantes: recompresión y resize leve (near-exact) y también crop,
watermark, cambios de color o encuadres parecidos (semántico). Un único método no cubre
bien ambos extremos. No se entrena modelo propio en v1.

## Decisión

Cada frame representativo participa en **dos sistemas complementarios**:
- **pHash** (perceptual hash) para **near-exact** (recompresión, resize menor) y para la
  **deduplicación** de frames en indexación (umbral configurable).
- **Embedding visual con SigLIP2** (alternativa OpenCLIP) para **similitud semántica**
  (crop, watermark, color, encuadre). Se usa como señal principal del ANN.

El **ranking** combina, con pesos **configurables**: similitud visual (embedding) + nº de
frames coincidentes + evidencia de pHash. La elección exacta modelo/dimensión se fija en el
PR de embeddings tras un mini-benchmark (precision/recall, frames/s, VRAM, dimensión).

## Alternativas consideradas

- **Solo pHash** — falla ante crop/watermark/color. Rechazada como único método.
- **Solo embeddings** — peor en near-exact y sin señal barata de dedupe. Rechazada como
  único método.
- **Entrenar modelo propio** — coste/tiempo injustificados para un spike. Diferida.

## Consecuencias

- (+) Cobertura amplia de transformaciones; señal barata (pHash) + señal robusta (embedding).
- (+) Dedupe reutiliza pHash, reduciendo embeddings innecesarios (coste).
- (−) Dos representaciones por frame (almacenamiento y cómputo); mitigado por dedupe y batch.

## Anexo PR-005 — dimensión fijada

**D = 768 con ViT-B-16-SigLIP** (open_clip, pretrained "webli", SigLIP **v1**), CPU local
Intel-Mac con torch 2.2.2 — elección fijada por el mini-benchmark de PR-005. La usará el
esquema DB (PR-006).
