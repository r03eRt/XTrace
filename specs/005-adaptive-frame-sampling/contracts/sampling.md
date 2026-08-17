# Contracts: Muestreo adaptativo de frames

## Política compartida

```python
@dataclass(frozen=True)
class AdaptiveSamplingPolicy:
    target_interval_ms: int = 120_000
    max_frames: int = 8

    def target_count(self, duration_ms: int | None, available_count: int | None = None) -> int: ...
    def ideal_timestamps(self, duration_ms: int) -> tuple[int, ...]: ...

def select_representative_frames(
    frames: Sequence[T],
    *,
    duration_ms: int | None,
    timestamp: Callable[[T], int | None],
    policy: AdaptiveSamplingPolicy,
) -> list[T]: ...
```

Garantías: resultado estable, 1–8 con entrada válida, sin reutilización, orden temporal y
`None` conservado sin posición fiable. Duraciones no positivas son desconocidas;
timestamps negativos o fuera de duración se degradan a `None`. El llamador deduplica
primero posición y pHash exacto.

## VideoIndexWriter

```python
async def replace_video_index(
    video_id: str,
    records: Sequence[FrameRecord],
    *,
    duration_ms: int | None,
) -> None: ...
```

Garantías: éxito deja exactamente `records` y actualiza estado/conteo/duración en la misma
unidad atómica; fallo conserva el índice anterior; no cambia `excluded`; vacío rechazado.

## CLI local

```text
xtrace-spike index --dataset <path> --sampling adaptive \
  --max-frames 8 --target-interval-seconds 120 --provider siglip
```

El modo omitido conserva `legacy_fixed` y `--frames-per-video 30`.

## CLI crawler

```text
xtrace-crawler reindex --source <source> --limit <N> --sampling adaptive \
  --max-frames 8 --target-interval-seconds 120
```

Salida JSON estable:

```json
{
  "source": "xvideos",
  "selected": 104,
  "enqueued": 104,
  "sampling": {"mode": "adaptive", "max_frames": 8, "target_interval_seconds": 120},
  "job_ids": ["..."]
}
```

Cada job `REINDEX` incluye fuente, `external_id` y política validada.

Solo son elegibles vídeos no excluidos en estado `indexed` o `failed` recuperable. El
handler repite la comprobación y la dedupe key incluye vídeo y hash del perfil.

## Estado de reindexación

```text
xtrace-crawler reindex-status --run-id <uuid>
```

Devuelve `pending`, `completed`, `skipped`, `failed`, `frames` y resultados por vídeo.

## Benchmark report

Cada caso aporta fuente, duración, vídeo y timestamp de verdad. Las políticas usan los
mismos casos e informan recall, error mediano/p95 y normalizado, frames, reducción y
segmentos. Menos de 30 casos, ausencia de local/web o cobertura insuficiente produce
`accepted=false`.
