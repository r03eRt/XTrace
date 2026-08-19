# Data Model: Refinamiento temporal bajo demanda

## Entidades de ejecución

### `RefinementPolicy`

Objeto inmutable de configuración, no persistido como JSON completo.

| Campo | Tipo | Regla |
| --- | --- | --- |
| `enabled` | boolean | `false` conserva el primer pase sin acceder a fuentes |
| `candidate_limit` | entero | `1..5`, default `3` |
| `max_assets_per_candidate` | entero | `1..30`, default `30` |
| `search_timeout_ms` | entero | `>0`, default `10000` |
| `candidate_timeout_ms` | entero | `>0`, `<= search_timeout_ms`, default `3000` |
| `max_asset_bytes` | entero | `>0`, default 10 MiB |
| `policy_version` | texto | versión estable para métricas y benchmark |

### `RefinementCandidate`

Vista efímera construida desde el primer pase y `public.videos`:

| Campo | Tipo | Regla |
| --- | --- | --- |
| `video_id` | UUID | FK lógica a `videos.id` |
| `source` | texto nullable | nombre de `sources`; `null` para vídeos locales |
| `adapter` | texto nullable | nombre de adapter registrado |
| `external_id` | texto nullable | necesario para `VideoSource` web |
| `page_url` | URL nullable | URL canónica ya validada |
| `duration_ms` | entero nullable | duración declarada por la fuente |
| `base_timestamp_ms` | entero nullable | timestamp del mejor frame indexado |
| `base_visual_similarity` | float | `1 - best_distance` del ranking |

Un vídeo local, excluido, retirado o con fuente no habilitada no es refinable y produce
fallback `unavailable`/`disabled` sin acceso de red.

### `RefinementEvidence`

Registro efímero y, solo si se selecciona o se necesita auditar el descarte, resumido en
la tabla de evidencia. Incluye `source`, `video_id`, `candidate_rank`, `asset_kind`, URL
pública sanitizada, `position`, `timestamp_ms`, similitud, `selected` y
`discarded_reason`. No contiene bytes ni una ruta al temporal local.

Invariantes:

- `asset_kind` solo puede ser `thumbnail` o `storyboard`;
- `timestamp_ms` es `null` únicamente para evidencia descartada; nunca se presenta;
- timestamp no negativo y, si hay duración, dentro de `[0, duration_ms]`;
- URL pertenece a la allowlist del adapter y no contiene credenciales;
- una URL/timestamp duplicada se cuenta una sola vez.

## Persistencia de métricas

La migración `20260818000000_temporal_refinement.sql` añade:

### `public.search_refinements`

Una fila por búsqueda (PK/FK `search_id` → `searches.id`, `ON DELETE CASCADE`):

| Campo | Tipo | Regla |
| --- | --- | --- |
| `search_id` | uuid | misma identidad que la respuesta REST |
| `status` | text | `completed`, `disabled`, `unavailable`, `limited` o `failed` |
| `policy_version` | text | no nulo |
| `candidates_requested` / `candidates_processed` | int | no negativos |
| `assets_requested` / `assets_evaluated` / `assets_discarded` | int | no negativos |
| `bytes_downloaded` | bigint | no negativo; solo bytes de assets visuales |
| `embedding_count` | int | no negativo; incluye solo imágenes evaluadas |
| `embedding_elapsed_ms` | int | no negativo; tiempo del provider, sin media persistida |
| `errors_count` | int | no negativo |
| `improved_count` / `unchanged_count` | int | no negativos |
| `elapsed_ms` | int | no negativo |
| `limit_reason` | text nullable | código estable, no detalle de excepción |
| `created_at` / `finished_at` | timestamptz | auditoría |

Checks SQL restringen estados y contadores. Se indexa `status`, `policy_version` y
`created_at` para métricas y TTL.

### `public.search_refinement_evidence`

Detalle acotado para la trazabilidad de assets:

| Campo | Tipo | Regla |
| --- | --- | --- |
| `search_id` | uuid | FK a `search_refinements`, cascade |
| `video_id` | uuid | FK a `videos`, cascade |
| `source` | text | nombre canónico del adapter |
| `candidate_rank` | int | rango del primer pase, `>=1` |
| `asset_kind` | text | `thumbnail` o `storyboard` |
| `asset_url` | text | URL pública sanitizada, sin query/fragmento/credenciales |
| `asset_url_hash` | text | SHA-256 del URL sanitizado |
| `position` | int nullable | posición declarada por el adapter |
| `timestamp_ms` | int nullable | timestamp declarado y validado |
| `similarity` | double precision | `[0,1]` |
| `selected` | boolean | true solo para evidencia que fijó el timestamp |
| `discarded_reason` | text nullable | código estable si se descartó |
| `created_at` | timestamptz | auditoría |

Un índice único por expresión sobre `(search_id, video_id, asset_url_hash,
coalesce(timestamp_ms,-1))` evita duplicados (la expresión se implementa como índice,
no como constraint de tabla). No se almacena la imagen de consulta ni el contenido
descargado.

## Resultado REST efímero

Cada `SearchResultItem` conserva sus campos actuales y añade:

```text
timestamp_provenance:
  origin: base_index | refined_asset
  status: improved | unchanged | unavailable | limited | disabled
  source: string | null
  asset_kind: thumbnail | storyboard | null
  asset_url: string | null
  asset_position: integer | null

SearchResponse.refinement:
  status: completed | disabled | unavailable | limited | failed
  candidates_requested, candidates_processed,
  assets_evaluated, assets_discarded, errors_count,
  bytes_downloaded, embedding_count, embedding_elapsed_ms,
  improved_results, elapsed_ms
```

`match_timestamp_ms` sigue siendo la propiedad compatible que se muestra; la
procedencia evita interpretarlo como exacto. La URL solo aparece cuando el timestamp
refinado se puede trazar a un asset público.

## Relaciones y retención

```text
searches 1 ─── 0..1 search_refinements 1 ─── N search_refinement_evidence N ─── 1 videos
videos N ─── 0..1 sources
videos 1 ─── N frames                 (índice base, nunca escrito por el refinamiento)
```

RLS queda habilitado y sin políticas/grants para `anon` o `authenticated`, igual que
`videos`, `frames`, `searches`, `sources` y `jobs`. El servicio Python usa exclusivamente
la conexión server-side existente. El purge de `searches` hereda por cascade y no deja
telemetría fuera del TTL vigente.
