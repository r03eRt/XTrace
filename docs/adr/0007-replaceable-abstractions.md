# 0007. Abstracciones reemplazables: VectorStore y EmbeddingProvider

- **Estado**: Aceptada
- **Fecha**: 2026-08-14
- **Spec/Requisitos relacionados**: 001-visual-search-spike · FR-005, FR-006, NFR mantenibilidad

## Contexto

Es probable cambiar de proveedor de vectores (pgvector → Qdrant) y de cómputo de embeddings
(CPU local → GPU local → Modal/otro) según coste y escala. Acoplar el dominio a una
implementación concreta encarecería esa migración.

## Decisión

Definir dos interfaces (ABC) estables desde el inicio:
- **`VectorStore`**: `upsert_frames`, `ann_search(embedding, k)`, `delete_video`, `stats`.
  Implementación del spike: `PgVectorStore` (pgvector/HNSW).
- **`EmbeddingProvider`**: `embed_images(batch) -> ndarray`, `dimension`, `model_id`.
  Implementación del spike: `SiglipLocalProvider`. Un `FakeEmbeddingProvider`
  determinista se usa en tests/CI para evitar cargar Torch.

El dominio (indexación, búsqueda, ranking) depende **solo** de las interfaces. Cambiar de
proveedor no debe requerir tocar el modelo de dominio (paridad con `ObjectStorage`/
`SourceAdapter` futuros).

## Alternativas consideradas

- **Acceso directo a pgvector/Torch desde el dominio** — simple hoy, caro de migrar y
  difícil de testear sin GPU. Rechazada.

## Consecuencias

- (+) Migración de proveedor contenida; tests rápidos y deterministas con dobles.
- (−) Una capa de indirección adicional; justificada por el cambio previsto de proveedor.
