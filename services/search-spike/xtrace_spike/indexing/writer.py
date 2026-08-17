"""Atomic per-video index replacement (TASK-005-001 / FR-010).

The writer is the single boundary at which a prepared embedding batch becomes
visible.  Extraction, deduplication and embedding happen before this boundary;
therefore a failure in any of those phases cannot publish a partial replacement.
Concrete stores implement the transaction appropriate to their backend.
"""

from __future__ import annotations

from collections.abc import Sequence

from xtrace_spike.indexing.state import VideoStateSnapshot, VideoStateStore
from xtrace_spike.vectorstore.base import FrameRecord, VectorStore


class AtomicReplacementError(RuntimeError):
    """The writer could not guarantee rollback of a failed replacement."""


class VideoIndexWriter:
    """Coordinate complete frame replacement with the video's final state."""

    def __init__(self, *, store: VectorStore, video_states: VideoStateStore | None = None) -> None:
        self._store = store
        self._video_states = video_states

    async def replace_video_index(
        self,
        video_id: str,
        records: Sequence[FrameRecord],
        *,
        duration_ms: int | None,
    ) -> None:
        """Publish exactly ``records`` or leave the prior complete index intact."""
        if not records:
            raise ValueError("el índice de vídeo no puede quedar vacío")

        handles_video_state = bool(getattr(self._store, "handles_video_state", False))
        state_snapshot: VideoStateSnapshot | None = None
        store_snapshot: object | None = None
        if self._video_states is not None and not handles_video_state:
            state_snapshot, store_snapshot = await self._prepare_rollback(video_id)

        try:
            await self._store.replace_video_index(video_id, records, duration_ms=duration_ms)

            # PgVectorStore commits the video row and frames in one SQL
            # transaction; its state store reads that same row.  The in-memory
            # backend has a separate state double, synchronized only after the
            # replacement has been published successfully.
            if self._video_states is not None and not getattr(
                self._store, "handles_video_state", False
            ):
                await self._video_states.mark_indexed(
                    video_id, frame_count=len(records), duration_ms=duration_ms
                )
        except Exception:
            rollback_errors: list[Exception] = []
            if self._video_states is not None and not handles_video_state:
                try:
                    await self._restore_store(store_snapshot)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                try:
                    await self._video_states.restore(video_id, state_snapshot)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise AtomicReplacementError(
                    "falló el reemplazo y no se pudo garantizar el rollback"
                ) from rollback_errors[0]
            raise

    async def _prepare_rollback(self, video_id: str) -> tuple[VideoStateSnapshot | None, object]:
        """Require and capture the public rollback contracts before mutation."""
        snapshot_state = getattr(self._video_states, "snapshot", None)
        restore_state = getattr(self._video_states, "restore", None)
        snapshot_store = getattr(self._store, "snapshot_video_index", None)
        restore_store = getattr(self._store, "restore_video_index", None)
        if not all(callable(method) for method in (snapshot_state, restore_state)):
            raise AtomicReplacementError("VideoStateStore no ofrece snapshot/restore público")
        if not all(callable(method) for method in (snapshot_store, restore_store)):
            raise AtomicReplacementError(
                "VectorStore no ofrece snapshot/restore público para rollback"
            )
        assert callable(snapshot_state)
        assert callable(snapshot_store)
        try:
            state_snapshot = await snapshot_state(video_id)
            store_snapshot = await snapshot_store()
        except Exception as exc:
            raise AtomicReplacementError(
                "no se pudo preparar el rollback antes del reemplazo"
            ) from exc
        return state_snapshot, store_snapshot

    async def _restore_store(self, snapshot: object | None) -> None:
        restore_store = getattr(self._store, "restore_video_index", None)
        if not callable(restore_store) or snapshot is None:
            raise AtomicReplacementError("VectorStore no puede restaurar el snapshot")
        await restore_store(snapshot)
