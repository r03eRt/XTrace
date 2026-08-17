# Data Model: Muestreo adaptativo de frames

No se requieren tablas ni migraciones nuevas.

## SamplingPolicy

| Campo | Tipo | Regla |
| --- | --- | --- |
| `mode` | `legacy_fixed \| adaptive` | Conserva referencia o activa esta feature. |
| `target_interval_ms` | entero | `120000`, mayor que cero. |
| `max_frames` | entero | `8`, rango `1..8`. |
| `fixed_frames` | entero | `30` para referencia histórica. |

## SamplingTarget

| Campo | Tipo | Regla |
| --- | --- | --- |
| `requested_count` | entero | Entre 1 y 8 en adaptativo. |
| `ideal_timestamps_ms` | lista de enteros | Ordenada, única y dentro de duración. |
| `duration_reliable` | booleano | Indica precisión temporal. |

## RepresentativeFrame

Reutiliza `frames`: `video_id`, `timestamp_ms`, `frame_seq`, `phash`, `embedding` y
`source_kind`. El timestamp solo se conserva cuando procede de evidencia permitida.

## ReindexRun y ReindexResult

`ReindexRun` se identifica por `run_id` en cada payload. Incluye pendientes,
`videos_indexed`, `videos_failed`, `videos_skipped`, `frames` y resultados por vídeo.

## State Transitions

```text
indexed(previous complete set)
  → replacement prepared
  → indexed(new frames + state/count committed atomically)
  ↘ job failed(previous complete index preserved)

no previous index
  → replacement prepared
  → indexed(new complete index)
  ↘ failed(no frames)
```

## Persistence Rules

- `replace_video_index` elimina/inserta frames y finaliza estado/conteo/duración en la
  misma unidad atómica.
- Repetir entrada y política produce las mismas claves y timestamps.
- `delete_video` queda reservado a exclusión/retirada.
- `run_id` y el perfil viven en payload; la dedupe key añade un hash estable del perfil.
