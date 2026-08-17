# 0013. Índice global multi-proveedor con 8 frames por vídeo

- **Estado**: Aceptada
- **Fecha**: 2026-08-17
- **Spec/Requisitos relacionados**: 001-visual-search-spike · 002-source-sdk-crawler · FR-002, FR-006, NFR coste
- **Decisión solicitada por**: operador del producto

## Contexto

XTrace debe poder incorporar vídeos de muchos proveedores y crecer desde miles hasta
millones de vídeos. Mantener 30–60 embeddings por vídeo en el índice global hace crecer
linealmente el almacenamiento, la indexación y el coste de búsqueda. Además, algunos
proveedores solo ofrecen un conjunto reducido de thumbnails o storyboards, por lo que el
número configurado no siempre coincide con el número de imágenes realmente disponibles.

El spike ya validó la búsqueda con una configuración histórica de 30 frames por vídeo.
Esa validación no debe confundirse con la política de muestreo del catálogo global.

## Decisión

El catálogo global multi-proveedor usará **8 frames representativos por vídeo como índice
base**.

- El muestreo será uniforme cuando exista duración fiable; si la fuente proporciona
  storyboards o thumbnails con posiciones, se conservarán esas posiciones y timestamps.
- Ocho es un **objetivo de la capa global**, no una garantía: si una fuente solo permite
  acceder legalmente a menos imágenes, se indexarán las disponibles y se conservará el
  conteo real.
- La primera búsqueda devolverá candidatos usando esos 8 frames. Un refinamiento
  posterior podrá analizar más frames únicamente para los candidatos principales, para
  mejorar el timestamp y resolver escenas cercanas.
- La estrategia mantiene `VectorStore` como frontera. No obliga todavía a migrar de
  pgvector; esa decisión se tomará con benchmarks cuando el volumen real lo justifique.
- La decisión aplica al catálogo global futuro. El benchmark del spike y el índice ya
  existente no se reindexan automáticamente por este ADR.

## Alternativas consideradas

- **30 frames globales** — mejor densidad temporal, pero mayor coste lineal sin aportar
  suficiente valor en la primera fase de identificación a gran escala. Se mantiene como
  referencia de validación del spike y como posible densidad de refinamiento.
- **60 frames globales** — coste y almacenamiento aún mayores; no es adecuado como valor
  por defecto para millones de vídeos.
- **4 frames globales** — menor coste, pero más riesgo de perder escenas distintivas;
  ocho ofrece un margen inicial más equilibrado.
- **Descargar y analizar el vídeo completo** — fuera de la estrategia del producto:
  aumenta coste y superficie de cumplimiento; se priorizan los assets visuales públicos
  permitidos por cada adapter.

## Consecuencias

- Para 1 millón de vídeos, el índice base tendrá aproximadamente 8 millones de vectores,
  antes del refinamiento bajo demanda.
- La marca de tiempo del primer pase será aproximada; la precisión temporal se obtiene
  mediante refinamiento de candidatos, no inflando el índice global.
- La ingesta futura debe soportar dos fases: índice global económico y refinamiento
  dirigido, con límites por fuente, duración y presupuesto.
- Antes de cambiar defaults o reindexar, debe ejecutarse un benchmark multi-fuente que
  compare recall, error temporal, coste de embeddings y número real de assets disponibles
  entre 8 y configuraciones más densas.

