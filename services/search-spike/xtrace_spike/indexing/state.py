"""Estado del vídeo en la indexación (PR-010 · FR-007 · ADR-0007).

La spec exige asociar cada frame a su vídeo y mantener el estado del vídeo
(`discovered`/`pending`/`indexing`/`indexed`/`failed`, FR-007). Como el
pipeline depende solo de interfaces (ADR-0007), el estado es una abstracción
inyectable (`VideoStateStore`) con dos implementaciones:

- `InMemoryVideoStateStore`: estado en memoria (dict), para tests del dominio
  sin DB (determinista, sin Postgres).
- `PgVideoStateStore`: estado real en la tabla `videos` (migración PR-006),
  reutilizando `PgRepo` (repo.py, PR-007) para las conexiones; es el camino
  de producción junto a `PgVectorStore` + `SiglipLocalProvider`.

Transiciones que implementa el pipeline (FR-007):
`discovered -> indexing -> indexed | failed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from xtrace_spike.repo import PgRepo, parse_uuid

#: Estados de `videos.status` (migración PR-006 · data-model.md · FR-007).
STATUS_DISCOVERED = "discovered"
STATUS_INDEXING = "indexing"
STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"


class VideoStateStore(Protocol):
    """Estado por vídeo (FR-007): transiciones y consulta, idempotente.

    Paridad de contrato con `VectorStore`/domain (ADR-0007): métodos async y
    claves estables (`video_id` derivado del `local_ref`, FR-008).
    """

    async def mark_discovered(self, video_id: str, local_ref: str) -> None: ...
    async def mark_indexing(self, video_id: str) -> None: ...
    async def mark_indexed(
        self, video_id: str, *, frame_count: int, duration_ms: int | None
    ) -> None: ...
    async def mark_failed(self, video_id: str, error: str) -> None: ...
    async def status(self, video_id: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class _VideoStateRecord:
    """Estado persistido por vídeo en memoria (InMemoryVideoStateStore)."""

    local_ref: str
    status: str
    frame_count: int = 0
    duration_ms: int | None = None
    error: str | None = None


class InMemoryVideoStateStore:
    """Estado del vídeo en memoria (dict) para tests sin DB (PR-010 · FR-007).

    Semántica documentada (paridad con `PgVideoStateStore`):
    - `mark_discovered` crea/resetea la entrada (estado `discovered`,
      error a None): es el inicio de un ciclo de indexación.
    - `mark_indexing`/`mark_indexed`/`mark_failed` transicionan el estado;
      `mark_indexed` registra `frame_count` y `duration_ms`; `mark_failed`
      registra el error del último intento.
    - `status` devuelve el estado actual o None si el vídeo no existe.
    """

    def __init__(self) -> None:
        self._videos: dict[str, _VideoStateRecord] = {}

    async def mark_discovered(self, video_id: str, local_ref: str) -> None:
        self._videos[video_id] = _VideoStateRecord(local_ref=local_ref, status=STATUS_DISCOVERED)

    async def mark_indexing(self, video_id: str) -> None:
        self._videos[video_id] = _VideoStateRecord(
            local_ref=self._local_ref_of(video_id), status=STATUS_INDEXING
        )

    async def mark_indexed(
        self, video_id: str, *, frame_count: int, duration_ms: int | None
    ) -> None:
        self._videos[video_id] = _VideoStateRecord(
            local_ref=self._local_ref_of(video_id),
            status=STATUS_INDEXED,
            frame_count=frame_count,
            duration_ms=duration_ms,
        )

    async def mark_failed(self, video_id: str, error: str) -> None:
        self._videos[video_id] = _VideoStateRecord(
            local_ref=self._local_ref_of(video_id),
            status=STATUS_FAILED,
            error=error,
        )

    async def status(self, video_id: str) -> str | None:
        record = self._videos.get(video_id)
        return record.status if record is not None else None

    def _local_ref_of(self, video_id: str) -> str:
        """local_ref conocido o respaldo (mismo criterio que repo.py)."""
        record = self._videos.get(video_id)
        return record.local_ref if record is not None else video_id


class PgVideoStateStore:
    """Estado real del vídeo en `public.videos` (PR-010 · FR-007 · PR-006).

    Reutiliza `PgRepo` (repo.py, PR-007) para las conexiones async
    (autocommit). Paridad con `InMemoryVideoStateStore`: mismas transiciones
    y consulta; `mark_indexed` registra además `indexed_at`. Idempotente
    (FR-008): los UPDATE por `id` no duplican filas y `mark_discovered` es
    un upsert por PK.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._repo = PgRepo(dsn)

    async def mark_discovered(self, video_id: str, local_ref: str) -> None:
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "insert into public.videos (id, local_ref, status) "
                    "values (%s, %s, 'discovered') "
                    "on conflict (id) do update set "
                    "local_ref = excluded.local_ref, status = 'discovered', "
                    "error = null",
                    (video_uuid, local_ref),
                )

    async def mark_indexing(self, video_id: str) -> None:
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.videos set status = 'indexing', error = null where id = %s",
                    (video_uuid,),
                )

    async def mark_indexed(
        self, video_id: str, *, frame_count: int, duration_ms: int | None
    ) -> None:
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.videos set status = 'indexed', "
                    "frame_count = %s, duration_ms = %s, error = null, "
                    "indexed_at = now() where id = %s",
                    (frame_count, duration_ms, video_uuid),
                )

    async def mark_failed(self, video_id: str, error: str) -> None:
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.videos set status = 'failed', error = %s where id = %s",
                    (error, video_uuid),
                )

    async def status(self, video_id: str) -> str | None:
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select status from public.videos where id = %s", (video_uuid,))
                row = await cur.fetchone()
        return row[0] if row is not None else None
