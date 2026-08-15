# 0010. Cola de jobs en Postgres con FOR UPDATE SKIP LOCKED (sin Redis)

- **Estado**: Aceptada
- **Fecha**: 2026-08-15
- **Spec/Requisitos relacionados**: 002-source-sdk-crawler · FR-006, FR-007, FR-008,
  DATA-002 · decisión de producto en `PRODUCT_IDEA.md` (colas = tabla `jobs`, no Redis en v1)

## Contexto

El crawler necesita una cola durable de trabajos (DISCOVER, FETCH_METADATA, INDEX_VIDEO,
EXTRACT_FRAMES, GENERATE_EMBEDDINGS, CHECK_AVAILABILITY, REINDEX) con reintentos, para uno o
varios workers locales. `PRODUCT_IDEA.md` ya fija la dirección: colas sobre la tabla `jobs`
en Postgres con `FOR UPDATE SKIP LOCKED`, **sin Redis** en v1 (principio *cheap first*:
menos infraestructura, una sola fuente de verdad de datos y operación más simple).

## Decisión

Persistir los jobs en una tabla **`jobs`** en la misma Supabase Postgres (local en dev), y
despacharlos con **`FOR UPDATE SKIP LOCKED`**: cada worker toma el siguiente job elegible
(`status='pending' AND not_before<=now()` ordenado por `created_at`) en una transacción que
lo marca `running` con lease (`locked_by`, `locked_at`).

Retries con **backoff exponencial + jitter completo** (base 1 s, factor 2, cap 1 h,
`max_attempts` configurable) planificados vía `not_before`; errores terminales
(404/removed, violación de robots/ToS) van a `unavailable`/`failed` definitivo sin
reintentos. Un **lease reset** devuelve a `pending` los `running` con lease vencido
(crash de worker). Sin broker externo; el único requisito es el Postgres ya existente.

## Alternativas consideradas

- **Redis/RQ/Celery** — reintentos y prioridades ricos, pero añade infraestructura, coste y
  dos fuentes de verdad en v1; el patrón SKIP LOCKED cubre el volumen previsto (una fuente,
  backfill acotado). Rechazada por ahora; revisable con métricas de escala (constitución §
  "cheap first, scale when proven").
- **Cola en proceso (asyncio.Queue)** — trivial, pero pierde durabilidad ante crash y
  dificulta varios workers y la observabilidad exigida (FR-014). Rechazada.
- **SQLite local** — simple pero duplica el motor de datos y no valida el patrón de
  producción decidido. Rechazada.

## Consecuencias

- (+) Una sola infraestructura (Postgres), durabilidad y observabilidad SQL de los jobs.
- (+) Sin servicio nuevo que operar; coste dev ~0 €.
- (−) Límites conocidos del patrón (polling, sin push): irrelevantes a la escala de esta
  fase; el `lease reset` añade complejidad mínima para robustez ante crashes.
