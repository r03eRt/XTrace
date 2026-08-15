# 0004. pgvector + HNSW como VectorStore del spike/MVP

- **Estado**: Aceptada
- **Fecha**: 2026-08-14
- **Spec/Requisitos relacionados**: 001-visual-search-spike · FR-006, SC-001, SC-003, NFR coste

## Contexto

El spike necesita búsqueda por similitud (ANN) sobre ~90k vectores con coste ~0 €.
Supabase (Postgres) ya forma parte del skeleton. Introducir una vector DB dedicada
(Qdrant) añadiría un servicio, coste y complejidad no justificados a esta escala.

## Decisión

Usar **PostgreSQL de Supabase con la extensión `pgvector`** e índice **HNSW**
(`vector_cosine_ops`) como almacén vectorial del spike y MVP. Evaluar **`halfvec`** para
reducir ~½ el almacenamiento de embeddings si la pérdida de precisión es aceptable
(medido en benchmark). El acceso se realiza siempre detrás de la abstracción `VectorStore`
(ver ADR-0007) para permitir migrar a Qdrant en el futuro sin tocar el dominio.

## Alternativas consideradas

- **Qdrant / vector DB dedicada** — mejor a gran escala (millones), pero servicio extra y
  coste; prematuro para 90k vectores. Diferida hasta que benchmarks lo justifiquen.
- **FAISS local en memoria** — no persistente ni compartible con Supabase; peor operativa.
  Rechazada para el MVP.

## Consecuencias

- (+) Coste 0 € (Supabase Free), un solo almacén (metadatos + vectores), operativa simple.
- (+) Migración futura contenida por `VectorStore`.
- (−) pgvector puede degradarse a mayor escala; se mide y se documenta como hallazgo.
- Nota: la **dimensión** del `vector(D)` depende del modelo (ADR-0005); fijarla antes del
  índice HNSW definitivo o migrar la columna.
