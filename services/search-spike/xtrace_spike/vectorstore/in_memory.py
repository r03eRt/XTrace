"""Implementación en memoria del contrato VectorStore (PR-003 · FR-006 · ADR-0007).

Doble para tests del dominio sin DB: distancia coseno sobre embeddings en memoria.
Paridad de comportamiento con `PgVectorStore` (PR-007) definida en la docstring de
`InMemoryVectorStore`.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from xtrace_spike.vectorstore.base import (
    FrameHit,
    FrameRecord,
    VectorStoreStats,
    VideoIndexMetadata,
)


@dataclass(frozen=True, slots=True)
class _StoredFrame:
    """Frame indexado: identidad + phash + embedding inmutable (tuple).

    El pHash (FIX-phash · FR-004/FR-006) se conserva tal cual lo entrega el
    contrato (entero sin signo de 64 bits, salida de compute_phash): el doble
    in-memory no tiene columna física, así que no aplica la codificación con
    signo de PgVectorStore.
    """

    frame_id: str
    video_id: str
    timestamp_ms: int | None
    phash: int
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _VideoIndexMetadata:
    """Metadata committed together with a video's frame representation."""

    status: str
    frame_count: int
    duration_ms: int | None


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Distancia coseno en [0, 1]; menor = más similar (contracts §5, ADR-0004)."""
    if len(a) != len(b):
        raise ValueError(f"Dimensiones de embedding distintas: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0  # vector nulo: máxima distancia (defensivo)
    return 1.0 - dot / (norm_a * norm_b)


class InMemoryVectorStore:
    """VectorStore en memoria (coseno) para tests del dominio sin DB (PR-003).

    Semántica documentada (paridad de contrato con `PgVectorStore`, PR-007):
    - `upsert_frames` es idempotente por `frame_id` (FR-008): re-upsert reemplaza,
      no duplica. Devuelve el nº de registros procesados. Conserva el pHash del
      frame (FIX-phash · FR-004/FR-006): `get_frame` lo devuelve tal cual.
    - `delete_video` elimina los frames del vídeo y marca el vídeo como excluido
      (equivalente en memoria a la columna `videos.excluded`, FR-014): un re-upsert
      posterior no lo devuelve a los resultados con `exclude_videos=True`.
    - `ann_search` ordena por distancia coseno ascendente y devuelve los `k` mejores.
    - `stats` cuenta filas almacenadas (mismo criterio que contar filas en la DB).
    """

    def __init__(self) -> None:
        self._frames: dict[str, _StoredFrame] = {}
        self._excluded_videos: set[str] = set()
        self._video_metadata: dict[str, _VideoIndexMetadata] = {}

    async def upsert_frames(self, frames: Sequence[FrameRecord]) -> int:
        for record in frames:
            self._frames[record["frame_id"]] = _StoredFrame(
                frame_id=record["frame_id"],
                video_id=record["video_id"],
                timestamp_ms=record["timestamp_ms"],
                phash=record["phash"],
                embedding=tuple(record["embedding"]),
            )
        for video_id in {record["video_id"] for record in frames}:
            frame_count = sum(frame.video_id == video_id for frame in self._frames.values())
            previous = self._video_metadata.get(video_id)
            self._video_metadata[video_id] = _VideoIndexMetadata(
                status="indexed",
                frame_count=frame_count,
                duration_ms=previous.duration_ms if previous is not None else None,
            )
        return len(frames)

    async def replace_video_index(
        self,
        video_id: str,
        frames: Sequence[FrameRecord],
        *,
        duration_ms: int | None,
    ) -> None:
        """Atomically replace one video's frames and committed metadata.

        All validation and frame materialization happen before publishing either
        dictionary, so an invalid or failed replacement leaves the old complete
        representation untouched.  The exclusion set is intentionally never
        modified: exclusion is an operator decision, not an indexing side effect.
        """
        if not frames:
            raise ValueError("el índice de vídeo no puede quedar vacío")
        if any(record["video_id"] != video_id for record in frames):
            raise ValueError("todos los frames deben pertenecer al video_id indicado")
        frame_ids = [record["frame_id"] for record in frames]
        if len(frame_ids) != len(set(frame_ids)):
            raise ValueError("el índice de vídeo contiene frame_id duplicados")
        timestamps = [record["timestamp_ms"] for record in frames]
        positioned = [timestamp for timestamp in timestamps if timestamp is not None]
        if len(positioned) != len(set(positioned)):
            raise ValueError("el índice de vídeo contiene posiciones duplicadas")

        replacement = {
            record["frame_id"]: _StoredFrame(
                frame_id=record["frame_id"],
                video_id=record["video_id"],
                timestamp_ms=record["timestamp_ms"],
                phash=record["phash"],
                embedding=tuple(record["embedding"]),
            )
            for record in frames
        }
        next_frames = {
            frame_id: frame
            for frame_id, frame in self._frames.items()
            if frame.video_id != video_id
        }
        next_frames.update(replacement)
        next_metadata = _VideoIndexMetadata(
            status="indexed",
            frame_count=len(replacement),
            duration_ms=duration_ms,
        )

        self._frames = next_frames
        self._video_metadata[video_id] = next_metadata

    async def get_video_index(self, video_id: str) -> dict[str, object]:
        """Return committed metadata for deterministic replacement assertions."""
        metadata = self._video_metadata.get(video_id)
        return {
            "status": metadata.status if metadata is not None else None,
            "frame_count": metadata.frame_count if metadata is not None else 0,
            "duration_ms": metadata.duration_ms if metadata is not None else None,
            "excluded": video_id in self._excluded_videos,
        }

    async def get_video_index_metadata(self, video_id: str) -> VideoIndexMetadata | None:
        """Expose committed count/duration for fresh state-store hydration."""
        metadata = self._video_metadata.get(video_id)
        if metadata is None:
            return None
        return {
            "frame_count": metadata.frame_count,
            "duration_ms": metadata.duration_ms,
        }

    async def mark_video_failed(self, video_id: str) -> None:
        """Keep the prior frame set while exposing a failed indexing state."""
        previous = self._video_metadata.get(video_id)
        self._video_metadata[video_id] = _VideoIndexMetadata(
            status="failed",
            frame_count=previous.frame_count if previous is not None else 0,
            duration_ms=previous.duration_ms if previous is not None else None,
        )

    async def snapshot_video_index(self) -> object:
        """Capture the complete in-memory index for a contractual rollback."""
        return dict(self._frames), dict(self._video_metadata), set(self._excluded_videos)

    async def restore_video_index(self, snapshot: object) -> None:
        """Restore a prior complete index after a state-publication failure."""
        if not isinstance(snapshot, tuple) or len(snapshot) != 3:
            raise ValueError("snapshot de índice en memoria inválido")
        frames, metadata, excluded = snapshot
        if (
            not isinstance(frames, dict)
            or not isinstance(metadata, dict)
            or not isinstance(excluded, set)
        ):
            raise ValueError("snapshot de índice en memoria inválido")
        self._frames = frames
        self._video_metadata = metadata
        self._excluded_videos = excluded

    async def get_frame(self, frame_id: str) -> FrameRecord | None:
        """Devuelve el registro almacenado (incluye el pHash real, FIX-phash).

        Acceso de inspeccion para tests y consumidores del indice (p. ej. el
        ranking de PR-013): None si el frame no esta indexado. Sin impacto en
        el contrato VectorStore (metodo propio de la implementacion).
        """
        frame = self._frames.get(frame_id)
        if frame is None:
            return None
        return FrameRecord(
            frame_id=frame.frame_id,
            video_id=frame.video_id,
            timestamp_ms=frame.timestamp_ms,
            phash=frame.phash,
            embedding=frame.embedding,
        )

    async def ann_search(
        self,
        embedding: Sequence[float],
        k: int,
        exclude_videos: bool = True,
    ) -> list[FrameHit]:
        if k <= 0:
            return []
        hits: list[FrameHit] = []
        for frame in self._frames.values():
            if exclude_videos and frame.video_id in self._excluded_videos:
                continue
            hits.append(
                FrameHit(
                    frame_id=frame.frame_id,
                    video_id=frame.video_id,
                    timestamp_ms=frame.timestamp_ms,
                    distance=_cosine_distance(embedding, frame.embedding),
                )
            )
        hits.sort(key=lambda hit: hit["distance"])
        return hits[:k]

    async def delete_video(self, video_id: str) -> None:
        for frame_id in [fid for fid, frame in self._frames.items() if frame.video_id == video_id]:
            del self._frames[frame_id]
        self._excluded_videos.add(video_id)
        previous = self._video_metadata.get(video_id)
        self._video_metadata[video_id] = _VideoIndexMetadata(
            status=previous.status if previous is not None else "excluded",
            frame_count=0,
            duration_ms=previous.duration_ms if previous is not None else None,
        )

    async def stats(self) -> VectorStoreStats:
        video_ids = {frame.video_id for frame in self._frames.values()}
        return VectorStoreStats(
            videos=len(video_ids),
            frames=len(self._frames),
            vectors=len(self._frames),
        )
