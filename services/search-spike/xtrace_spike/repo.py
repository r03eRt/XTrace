"""Acceso a PostgreSQL (Supabase local) del spike — FR-007 · plan.md.

Capa de datos sobre psycopg (async) que `PgVectorStore` (PR-007) usa para
conectarse y garantizar la existencia de las filas `videos` referenciadas
(FR-007: `frames.video_id` es FK → `videos.id` ON DELETE CASCADE, migración
PR-006). La gestión de estado del vídeo (`status`, `frame_count`,
`indexed_at`) y la exclusión (`excluded`, FR-014) se ampliarán en PR-010 y
PR-013 respectivamente (tasks.md).

El DSN es configurable por entorno (`SUPABASE_DB_URL`, ver quickstart.md) o
por constructor; el valor por defecto apunta a Supabase local (config.toml,
puerto 55322).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg

#: Variable de entorno documentada en quickstart.md para el DSN del servicio.
DATABASE_URL_ENV = "SUPABASE_DB_URL"

#: Supabase local (config.toml): postgres superuser, RLS bypassed (service-side).
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:55322/postgres"


def resolve_dsn() -> str:
    """DSN efectivo: variable de entorno o Supabase local (default)."""
    return os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL


def parse_uuid(value: str, field: str) -> uuid.UUID:
    """Valida un id UUID del contrato VectorStore (frame_id/video_id).

    El esquema PR-006 usa `uuid` como PK/FK mientras el contrato (contracts §2)
    transporta los ids como `str`. El doble in-memory acepta cualquier cadena;
    aquí la DB lo exige, así que el error es explícito y temprano.
    """
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} no es un UUID válido: {value!r}") from exc


class PgRepo:
    """Acceso a la DB del spike (psycopg async, una conexión por operación).

    Las conexiones se abren con `autocommit=True`: el spike es una CLI local y
    la validación de contrato ocurre en Python antes de ejecutar SQL, por lo que
    no se necesita transaccionalidad explícita.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or resolve_dsn()

    async def connect(self) -> psycopg.AsyncConnection[Any]:
        """Nueva conexión async (autocommit) contra el DSN del repo."""
        return await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)

    async def ensure_video(self, video_id: str, local_ref: str | None = None) -> bool:
        """Garantiza la fila `videos(id)` para satisfacer la FK de `frames`.

        Idempotente (FR-008): si el vídeo ya existe no se modifica (DO NOTHING)
        — el `local_ref` real lo fija el pipeline de indexación (PR-010) con su
        propio flujo; aquí solo se asegura la integridad referencial. Cuando no
        se aporta `local_ref` se usa `video_id` como referencia de respaldo
        (la columna es NOT NULL). Devuelve True si la fila se creó.
        """
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "insert into public.videos (id, local_ref) values (%s, %s) "
                    "on conflict (id) do nothing",
                    (video_uuid, local_ref or video_id),
                )
                return cur.rowcount == 1
