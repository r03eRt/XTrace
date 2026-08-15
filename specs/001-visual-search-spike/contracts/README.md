# Contracts — Visual Search Spike

Contratos estables que los implementadores deben respetar. Cambios a estos contratos
requieren actualizar la spec/plan primero.

## 1. CLI (`xtrace-spike`, Typer) — FR-017 / Decisión D2

Salida siempre en **JSON** por stdout (para tests y benchmark reproducibles); logs por
stderr.

### `index`
```
xtrace-spike index --dataset <path>
                   [--frames-per-video N=30]
                   [--dedupe-threshold T=<hamming>]
                   [--batch-size B=64]
```
- Ingiere el dataset local, extrae/deduplica frames, calcula pHash+embedding y persiste.
- Idempotente (FR-008). Cleanup garantizado (FR-009).
- Salida: `{ "videos_indexed": n, "videos_failed": m, "frames": f, "vectors": v }`.

### `search`
```
xtrace-spike search --image <path> [--top-k K=10]
```
- Salida:
```json
{
  "search_id": "uuid",
  "processing_ms": 1234,
  "results": [
    {
      "video_id": "uuid",
      "local_ref": "…",
      "match_score": 0.0,
      "matching_frames": 0,
      "match_timestamp_ms": 94000,
      "evidence": { "visual": 0.0, "phash": 0.0 }
    }
  ]
}
```
- `match_timestamp_ms` puede ser `null` si el frame coincidente no tiene timestamp (FR-012).
- Excluye vídeos con `excluded=true` (FR-014).

### `benchmark`
```
xtrace-spike benchmark --cases <path> [--top-k K=10]
```
- Salida (informe reproducible, FR-016 / SC-001..007):
```json
{
  "cases": 210,
  "top1": 0.0, "top5": 0.0, "top10": 0.0,
  "false_positive_rate_negatives": 0.0,
  "latency_ms": { "p50": 0, "p95": 0 },
  "frames_per_video_avg": 0.0,
  "index_size_bytes": 0,
  "embedding_throughput_fps": 0.0
}
```

### `exclude` / `stats`
```
xtrace-spike exclude --video <id>        # FR-014
xtrace-spike stats                       # métricas del índice (observabilidad)
```

## 2. `VectorStore` (ABC) — FR-006 / ADR-0007

```python
class FrameHit(TypedDict):
    frame_id: str
    video_id: str
    timestamp_ms: int | None
    distance: float          # menor = más similar (coseno)

class VectorStore(Protocol):
    async def upsert_frames(self, frames: Sequence[FrameRecord]) -> int: ...
    async def ann_search(self, embedding: Sequence[float], k: int,
                         exclude_videos: bool = True) -> list[FrameHit]: ...
    async def delete_video(self, video_id: str) -> None: ...
    async def stats(self) -> VectorStoreStats: ...
```
- Implementación del spike: `PgVectorStore` (pgvector/HNSW, coseno).

## 3. `EmbeddingProvider` (ABC) — FR-005 / ADR-0007

```python
class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int
    def embed_images(self, images: Sequence["PIL.Image"]) -> "np.ndarray": ...  # (N, D), L2-normalized
```
- Implementaciones: `SiglipLocalProvider` (real) y `FakeEmbeddingProvider` (determinista,
  para tests/CI sin Torch).

## 4. Ranking — FR-013

`rank(candidate_frames) -> [SearchResult]` agrupa por `video_id` y combina, con **pesos
configurables**: similitud visual (1 − distancia), nº de frames coincidentes y evidencia
pHash. Devuelve `match_score` normalizado y `match_timestamp_ms` del mejor frame.
La consistencia temporal (clips) queda diferida (D1) pero el diseño no debe impedir añadirla.

## 5. Invariantes

- Ningún contrato expone/almacena la media de consulta (FR-018, ADR-0006).
- `service_role` solo en el servicio Python (nunca cliente).
- Embeddings L2-normalizados; distancia coseno en el ANN.
