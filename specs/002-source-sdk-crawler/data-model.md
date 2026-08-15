# Data Model — Source SDK + Primer Crawler

> Detalle del modelo de la spec 002. Amplía el esquema del spike (001) de forma **no
> destructiva**: las tablas y datos existentes (`videos` con `local_ref`, `frames`,
> `searches`) permanecen intactos.

## Tabla `sources`

| Columna | Tipo | Notas |
| --- | --- | --- |
| `id` | `uuid` PK | `default gen_random_uuid()` |
| `name` | `text` NOT NULL | nombre canónico (p. ej. `xvideos`) · **UNIQUE** |
| `adapter` | `text` NOT NULL | clase/registro del adapter (p. ej. `xvideos`) |
| `manifest` | `jsonb` NOT NULL | compliance: `access_method`, `assets_accessed`, `robots_reviewed` (bool), `terms_reviewed` (bool), `rate_limit` (defaults), `review_date` (date) |
| `enabled` | `boolean` NOT NULL default false | gate SEC-002: solo `true` con manifest revisado y aprobación humana |
| `created_at` | `timestamptz` NOT NULL default now() | |
| `updated_at` | `timestamptz` NOT NULL default now() | trigger `set_updated_at` (reutilizado) |

## Tabla `videos` (ampliación del spike)

Columnas nuevas (todas NULL por defecto para no afectar a las filas locales del spike):

| Columna | Tipo | Notas |
| --- | --- | --- |
| `source_id` | `uuid` NULL | FK → `sources(id)` `ON DELETE SET NULL` |
| `external_id` | `text` NULL | id en la fuente (URL-slug o numérico) |
| `page_url` | `text` NULL | URL canónica del vídeo en la fuente |
| `title` | `text` NULL | |
| `tags` | `jsonb` NULL | lista de etiquetas de la fuente |
| `published_at` | `timestamptz` NULL | |
| `thumbnail_url` | `text` NULL | |
| `preview_url` | `text` NULL | |
| `storyboard_urls` | `jsonb` NULL | lista de URLs de storyboard/sprite |

Cambios sobre columnas existentes:

- `status`: se amplía el CHECK con `unavailable` y `removed`
  (`discovered`/`pending`/`indexing`/`indexed`/`failed`/`unavailable`/`removed`) — FR-012.
- Unicidad: índice único **parcial** `UNIQUE(source_id, external_id) WHERE source_id IS NOT
  NULL AND external_id IS NOT NULL` (idempotencia web, DATA-001). `UNIQUE(local_ref)` se
  mantiene para vídeos locales (DATA-003: no colisionan).
- Índice: `idx_videos_source_external` sobre `(source_id, external_id)`; el existente
  `idx_videos_status` se mantiene.

## Tabla `jobs`

| Columna | Tipo | Notas |
| --- | --- | --- |
| `id` | `uuid` PK | |
| `job_type` | `text` NOT NULL | `DISCOVER`/`FETCH_METADATA`/`INDEX_VIDEO`/`EXTRACT_FRAMES`/`GENERATE_EMBEDDINGS`/`CHECK_AVAILABILITY`/`REINDEX` (DATA-002) |
| `status` | `text` NOT NULL default `pending` | `pending`/`running`/`done`/`failed`/`unavailable` |
| `source_id` | `uuid` NULL | FK → `sources(id)` `ON DELETE SET NULL` |
| `video_id` | `uuid` NULL | FK → `videos(id)` `ON DELETE CASCADE` |
| `payload` | `jsonb` NOT NULL default `'{}'` | parámetros del job (cursor, limit, …) |
| `attempts` | `int` NOT NULL default 0 | |
| `max_attempts` | `int` NOT NULL default 3 | configurable por job/tipo |
| `not_before` | `timestamptz` NOT NULL default now() | planificación por backoff (FR-008) |
| `locked_by` | `text` NULL | identificador del worker (observabilidad + lease) |
| `locked_at` | `timestamptz` NULL | lease: los `running` con lease vencido vuelven a `pending` (edge crash) |
| `error` | `text` NULL | último error |
| `created_at` | `timestamptz` NOT NULL default now() | |
| `updated_at` | `timestamptz` NOT NULL default now() | trigger `set_updated_at` |

- Índices: `idx_jobs_dispatch` sobre `(status, not_before)` (despacho con
  `FOR UPDATE SKIP LOCKED`, ADR-0010); `idx_jobs_source` sobre `source_id`; `idx_jobs_type`
  sobre `job_type`.

## Relaciones

```text
sources (1) ──< videos (N)          [videos.source_id → sources.id]
sources (1) ──< jobs (N)            [jobs.source_id → sources.id]
videos  (1) ──< jobs (N)            [jobs.video_id → videos.id, ON DELETE CASCADE]
videos  (1) ──< frames (N)          [spike; frames.source_kind ya admite storyboard/thumbnail]
```

Los vídeos locales del spike (`source_id IS NULL`) conviven con los web sin colisión de
unicidad (DATA-003).

## Semántica de despacho (`FOR UPDATE SKIP LOCKED` — ADR-0010)

```sql
select * from jobs
where status = 'pending' and not_before <= now()
order by created_at
for update skip locked
limit 1;
```

- El worker marca `status='running'`, `locked_by=<worker>`, `locked_at=now()` en la misma
  transacción.
- Al terminar: `done` (éxito) · `failed` (error transitorio: `attempts+1`,
  `not_before = now() + backoff_exponencial(attempts) * jitter`, si `attempts <
  max_attempts`) · `failed` definitivo o `unavailable` (error terminal: 404/removed,
  violación de robots/ToS → sin más reintentos, FR-008).
- Lease reset: jobs `running` con `locked_at` anterior a `now() - lease_timeout` vuelven a
  `pending` (crash del worker, edge case de la spec).

## RLS y seguridad

- RLS **habilitada** en `sources` y `jobs` con política **deny-by-default** (paridad con el
  spike). Sin grants a `anon`/`authenticated`.
- El servicio Python accede con `service_role` (servidor). Ningún acceso desde cliente.
- pgTAP verifica: tablas, constraints únicos (parcial incluido), CHECK de estados, índices de
  despacho y RLS activa.

## Extensiones

Sin extensiones nuevas: `vector` y `pgcrypto` ya instaladas por la migración del spike.
