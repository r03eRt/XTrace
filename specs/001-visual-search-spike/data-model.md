# Data Model — Visual Search Spike

> Detalle del modelo de la spec 001. La dimensión `D` del embedding se fija al elegir el
> modelo SigLIP (ADR-0005). Se evaluará `halfvec(D)` (ADR-0004).

## Tabla `videos`

| Columna | Tipo | Notas |
| --- | --- | --- |
| `id` | `uuid` PK | `default gen_random_uuid()` |
| `local_ref` | `text` NOT NULL | ruta/identificador local del dataset · **UNIQUE** |
| `duration_ms` | `int` NULL | de FFprobe |
| `status` | `text` NOT NULL | enum: `discovered`/`pending`/`indexing`/`indexed`/`failed` |
| `frame_count` | `int` NOT NULL default 0 | nº de frames representativos |
| `excluded` | `boolean` NOT NULL default false | FR-014 (excluir de resultados) |
| `error` | `text` NULL | último error de indexación |
| `indexed_at` | `timestamptz` NULL | |
| `created_at` | `timestamptz` NOT NULL default now() | |
| `updated_at` | `timestamptz` NOT NULL default now() | trigger de actualización |

- **UNIQUE(local_ref)** → idempotencia a nivel de vídeo (FR-008).
- Índice: `idx_videos_status`.

## Tabla `frames`

| Columna | Tipo | Notas |
| --- | --- | --- |
| `id` | `uuid` PK | |
| `video_id` | `uuid` NOT NULL | FK → `videos(id)` ON DELETE CASCADE |
| `timestamp_ms` | `int` NULL | instante aproximado del frame |
| `frame_seq` | `int` NOT NULL | orden dentro del vídeo (para idempotencia si no hay ts) |
| `phash` | `bigint` NOT NULL | pHash 64-bit (o `bit(64)`) — near-exact (FR-004) |
| `embedding` | `vector(D)` NOT NULL | (o `halfvec(D)`) — semántico (FR-005) |
| `width` | `int` NULL | |
| `height` | `int` NULL | |
| `source_kind` | `text` NOT NULL | `video_frame`/`storyboard`/`thumbnail` (spike: `video_frame`) |
| `created_at` | `timestamptz` NOT NULL default now() | |

- **UNIQUE(video_id, frame_seq)** y, cuando hay timestamp, `UNIQUE(video_id, timestamp_ms)`
  → reindexar no duplica (FR-008).
- Índices:
  - **HNSW** sobre `embedding` con `vector_cosine_ops` (ANN, FR-006).
  - `idx_frames_phash` sobre `phash` (búsqueda near-exact / rango de Hamming por prefiltro).
  - `idx_frames_video_id` sobre `video_id` (agrupación por vídeo).

## Tabla `searches`

| Columna | Tipo | Notas |
| --- | --- | --- |
| `id` | `uuid` PK | |
| `search_type` | `text` NOT NULL | `image` (spike) |
| `processing_ms` | `int` NOT NULL | latencia total |
| `results_count` | `int` NOT NULL | nº de vídeos devueltos |
| `created_at` | `timestamptz` NOT NULL default now() | |

- **No** almacena la media de consulta (FR-018, privacidad, ADR-0006).

## Relaciones

```text
videos (1) ──< frames (N)
searches  (registro analítico, sin FK a resultados en el spike)
```

## RLS y seguridad

- RLS **habilitada** en las tres tablas con política **deny-by-default**.
- El servicio Python accede con `service_role` (servidor). Ningún acceso desde cliente.
- Verificado con pgTAP: existencia de tablas, constraints únicos, índices y RLS activa.

## Extensiones

- `create extension if not exists vector;` (pgvector) — ADR-0004.
- `pgcrypto`/`gen_random_uuid` para PKs (según disponibilidad en Supabase).

## Notas de rendimiento

- Parámetros HNSW (`m`, `ef_construction`, `ef_search`) configurables; valores iniciales por
  defecto de pgvector, ajustados con benchmark (SC-001/SC-003).
- Evaluar `halfvec(D)` para ~½ almacenamiento; validar precisión en benchmark.
