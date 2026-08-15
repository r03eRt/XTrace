"""Implementación `PgVectorStore` del contrato VectorStore (PR-007 · FR-006/008/014
· SC-003/005 · ADR-0004/0007 · contracts §2).

Índice vectorial real del spike sobre la tabla `frames` (migración PR-006):
embedding `vector(768)` con índice HNSW `vector_cosine_ops` (ADR-0004) y
distancia coseno `<=>` (menor = más similar; invariante contracts §5).

Paridad de comportamiento con `InMemoryVectorStore` (PR-003), documentada en la
docstring de esta clase: misma semántica de contrato (upsert idempotente,
`delete_video`, filtro `exclude_videos`, `stats`).

El pHash real de cada frame (FR-004/FR-006, FIX-phash) se persiste en la
columna `frames.phash` usando `phash_to_db` (bigint con signo; ver el
codec al pie de este módulo). Quien lea el pHash almacenado debe decodificar
con `phash_from_db` antes de compararlo con el pHash de una consulta.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from xtrace_spike.repo import PgRepo, parse_uuid
from xtrace_spike.vectorstore.base import FrameHit, FrameRecord, VectorStoreStats

#: Dimensión del embedding fijada por PR-005 (SigLIP2) y usada por el esquema
#: PR-006 (`vector(768)`). Los embeddings del contrato son L2-normalizados.
EMBEDDING_DIMENSION = 768


def phash_to_db(phash: int) -> int:
    """Codifica un pHash de 64 bits sin signo para la columna frames.phash.

    La columna es bigint (con signo, migración PR-006) pero el pHash del
    contrato (FR-004) es un entero sin signo de 64 bits [0, 2^64): el bit 63
    suele estar a 1 (coeficiente DC del DCT de imagehash), por lo que el valor
    crudo desborda bigint ("bigint out of range"). Se persiste la
    reinterpretación con signo (complemento a dos, biyección determinista):
    phash_to_db(0) = 0 y phash_to_db(2^64 - 1) = -1.

    Raises:
        ValueError: si phash no está en [0, 2^64) (no es un pHash 64-bit).
    """
    if not 0 <= phash < 1 << 64:
        raise ValueError(f"pHash fuera de rango [0, 2^64): {phash}")
    return phash if phash < 1 << 63 else phash - (1 << 64)


def phash_from_db(value: int) -> int:
    """Decodifica el frames.phash almacenado (bigint con signo) a pHash real.

    Inversa de phash_to_db: devuelve el entero sin signo de 64 bits (salida
    de compute_phash). Los consumidores del pHash persistido (p. ej. la
    evidencia pHash del ranking de PR-013) DEBEN aplicar esta función antes
    de comparar con el pHash de una consulta.
    """
    return value if value >= 0 else value + (1 << 64)


#: Offset del `frame_seq` para frames sin timestamp: sus ordinales de lote viven
#: en [1e9, 2^31) mientras que los frames con timestamp usan
#: `frame_seq = timestamp_ms` (< 1e9 ms ≈ 11,6 días para cualquier vídeo real).
#: Así ambas familias de claves no colisionan dentro de UNIQUE(video_id,
#: frame_seq) y la idempotencia es estable (FR-008/SC-005).
_NULL_TS_SEQ_OFFSET = 1_000_000_000


def _embedding_literal(embedding: Sequence[float]) -> str:
    """Literal pgvector `[x,y,…]` (texto; la DB castea con `::vector`).

    El paquete Python `pgvector` no está en pyproject.toml (no editable aquí),
    así que se usa el formato de entrada nativo de `vector`. Aprovecha la
    validación de dimensión para dar paridad de error con `InMemoryVectorStore`.
    """
    if len(embedding) != EMBEDDING_DIMENSION:
        raise ValueError(
            f"Dimensiones de embedding distintas: {len(embedding)} != {EMBEDDING_DIMENSION}"
        )
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class PgVectorStore:
    """VectorStore sobre pgvector/HNSW (coseno) en Supabase (PR-007).

    Semántica documentada (paridad de contrato con `InMemoryVectorStore`):
    - upsert_frames es idempotente (FR-008/SC-005): la clave de conflicto es
      UNIQUE(video_id, frame_seq) — frame_seq = timestamp_ms cuando existe o
      un ordinal estable del lote (offset 1e9) si no — y el re-upsert reemplaza,
      no duplica. Los vídeos referenciados se crean si faltan (FK; FR-007 vía
      PgRepo.ensure_video). Devuelve el nº de registros procesados. Persiste el
      pHash real del frame (FIX-phash · FR-004/FR-006) codificado con signo
      (phash_to_db): la columna frames.phash queda con el pHash real del frame
      (no 0), salvo que el registro lo transporte como 0 explícitamente.
    - `delete_video` elimina los frames del vídeo y marca el vídeo como excluido
      (`videos.excluded`, FR-014): un re-upsert posterior no lo devuelve a los
      resultados con `exclude_videos=True`.
    - `ann_search` ordena por distancia coseno ascendente (HNSW, `<=>`) y
      devuelve los `k` mejores, filtrando vídeos `excluded` por defecto.
    - `stats` cuenta filas del índice con el mismo criterio que el doble
      in-memory (videos = vídeos con ≥ 1 frame indexado).
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._repo = PgRepo(dsn)

    async def upsert_frames(self, frames: Sequence[FrameRecord]) -> int:
        if not frames:
            return 0
        rows: list[tuple[Any, ...]] = []
        ordinals: dict[str, int] = {}
        for record in frames:
            frame_uuid = parse_uuid(record["frame_id"], "frame_id")
            video_uuid = parse_uuid(record["video_id"], "video_id")
            timestamp_ms = record["timestamp_ms"]
            if timestamp_ms is not None:
                frame_seq = timestamp_ms
            else:
                ordinal = ordinals.get(record["video_id"], 0)
                ordinals[record["video_id"]] = ordinal + 1
                frame_seq = _NULL_TS_SEQ_OFFSET + ordinal
            rows.append(
                (
                    frame_uuid,
                    video_uuid,
                    timestamp_ms,
                    frame_seq,
                    phash_to_db(record["phash"]),
                    _embedding_literal(record["embedding"]),
                )
            )
        video_ids = sorted({str(video_uuid) for _, video_uuid, *_ in rows})
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "insert into public.videos (id, local_ref) values (%s, %s) "
                    "on conflict (id) do nothing",
                    [(vid, vid) for vid in video_ids],
                )
                await cur.executemany(
                    "insert into public.frames "
                    "(id, video_id, timestamp_ms, frame_seq, phash, embedding) "
                    "values (%s, %s, %s, %s, %s, %s::vector) "
                    "on conflict (video_id, frame_seq) do update set "
                    "id = excluded.id, timestamp_ms = excluded.timestamp_ms, "
                    "phash = excluded.phash, embedding = excluded.embedding",
                    rows,
                )
        return len(frames)

    async def ann_search(
        self,
        embedding: Sequence[float],
        k: int,
        exclude_videos: bool = True,
    ) -> list[FrameHit]:
        if k <= 0:
            return []
        query = _embedding_literal(embedding)
        # `%s = false` se pliega a constante en plan time: sin filtro cuando
        # exclude_videos=False, `not v.excluded` cuando True (FR-014).
        sql = (
            "select f.id::text, f.video_id::text, f.timestamp_ms, "
            "(f.embedding <=> %s::vector) as distance "
            "from public.frames f "
            "join public.videos v on v.id = f.video_id "
            "where %s = false or not v.excluded "
            "order by distance asc "
            "limit %s"
        )
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (query, exclude_videos, k))
                rows = await cur.fetchall()
        return [
            FrameHit(
                frame_id=str(row[0]),
                video_id=str(row[1]),
                timestamp_ms=row[2],
                distance=float(row[3]),
            )
            for row in rows
        ]

    async def delete_video(self, video_id: str) -> None:
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.videos set excluded = true where id = %s",
                    (video_uuid,),
                )
                await cur.execute(
                    "delete from public.frames where video_id = %s",
                    (video_uuid,),
                )

    async def stats(self) -> VectorStoreStats:
        async with await self._repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select count(distinct video_id), count(*), count(*) from public.frames"
                )
                row = await cur.fetchone()
        assert row is not None, "count(*) sobre frames siempre devuelve una fila"
        videos, frames, vectors = row
        return VectorStoreStats(videos=videos, frames=frames, vectors=vectors)
