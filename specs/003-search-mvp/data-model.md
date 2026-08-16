# Data Model — MVP de Búsqueda (spec 003)

> **No hay modelo de datos nuevo.** La fase 3 reutiliza el esquema existente de las fases
> 1-2 (DATA-001) sin migraciones ni tablas nuevas. Este documento registra qué tablas se
> leen/escriben y por quién.

## Tablas reutilizadas (sin cambios)

| Tabla | Uso en esta fase | Quién |
| --- | --- | --- |
| `videos` | **Ficha** `GET /videos/{id}` (FR-008: `local_ref`, `title`, `page_url`, `status`, `duration_ms`, `frame_count`, `tags`, `published_at`, `thumbnail_url`, `excluded`) y **enriquecimiento** de resultados de `/search` (`title`, `page_url`, FR-004 MAY); join a `sources` para el nombre de la fuente | `xtrace_api` (lectura) |
| `frames` | ANN del índice (HNSW `vector(768)` coseno) y evidencia pHash (`PgRepo.get_frame_phashes`) | `xtrace_spike` vía `PgVectorStore`/`PgRepo` (reutilizado) |
| `searches` | **Analítica sin media** (FR-012): la API inserta una fila por búsqueda aceptada: `id = search_id` (uuid), `search_type='image'`, `processing_ms`, `results_count`, `created_at` | `xtrace_api` (escritura) |
| `sources` | Solo lectura indirecta (nombre de la fuente en la ficha vía FK `videos.source_id`) | `xtrace_api` (lectura) |

## TTL configurable sin cambio de esquema (FR-012)

La tabla `searches` no tiene columna de expiración y **no se añade ninguna** (DATA-001). El
TTL se implementa como **cleanup periódico por `created_at`** ejecutado en el lifespan del
servicio FastAPI:

- `XTRACE_API_SEARCHES_TTL_DAYS` (default `30`): antigüedad máxima de las filas.
- `XTRACE_API_SEARCHES_TTL_CLEANUP_MIN` (default `60`): intervalo del cleanup (más un purge
  inicial al arrancar).

```sql
delete from public.searches
 where created_at < now() - make_interval(days => <ttl_days>);
```

Limitación aceptada: no es expiración real de filas, sino limpieza por antigüedad;
suficiente para la analítica temporal del MVP y compatible con "preferiblemente ninguna
tabla nueva" (DATA-001).

## RLS y acceso

- RLS **deny-by-default intacta** en `videos`, `frames`, `searches` (y `sources`/`jobs`):
  sin políticas nuevas ni grants a `anon`/`authenticated` (SEC-004).
- La API accede con credenciales de servidor (`SUPABASE_DB_URL`, service-side) vía
  `PgRepo`/`PgVectorStore` del spike (paridad con la CLI).
- La media de consulta **nunca** se persiste en ninguna tabla (SEC-005, ASSUMPTION-6);
  `searches` solo contiene el registro analítico sin media.

## Corpus (sin reindexar, DATA-003)

El índice consumido es el **real actual** (D5): 104 vídeos `indexed` del tag `buttfucking`
(web, embeddings SigLIP reales) + 43 vídeos del dataset local del spike. La API lo lee tal
cual (FR-013). `halfvec`/escalado: **diferido** (ADR-0004) — no es de esta fase.
