"""GET /videos/{id} (PR-056 · FR-008/011 · SEC-004 · contracts §4/§5).

Ficha del vídeo con metadatos, fuente y enlace original: `local_ref`, `title`,
`page_url`, `source` (`sources.name` vía el join existente `videos.source_id`),
`status`, `duration_ms`, `frame_count`, `tags`, `published_at`,
`thumbnail_url`, `excluded` (contracts §4; los campos del contrato marcados
nullable quedan `null` para vídeos locales sin fuente/metadatos).

Errores (contracts §5): **400** `invalid_uuid` si el id no es un UUID válido
(validación sin tocar la BD, paridad SC-006), **404** `video_not_found` si el
vídeo no existe y **503** `index_unavailable` si la BD no está disponible
(backend in-memory sin `SUPABASE_DB_URL` → gate sin conexión al DSN por
defecto; con postgres, el handler de psycopg de main.py).

La lectura de `public.videos` reutiliza el repo del spike (`PgRepo`,
credenciales de servidor — SEC-004, RLS deny-by-default intacta): misma capa
de datos que la CLI, sin duplicar. `_fetch_video_card` es inyectable en tests
(monkeypatch) para cubrir 200/404 sin BD.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC

from fastapi import APIRouter
from xtrace_spike.cli import build_backend  # type: ignore[import-untyped]
from xtrace_spike.repo import PgRepo  # type: ignore[import-untyped]

from xtrace_api.schemas import VideoCard

logger = logging.getLogger(__name__)

router = APIRouter(tags=["videos"])


class VideoCardError(Exception):
    """Error de la ficha con el cuerpo del contracts §5 (400/404).

    El handler registrado en `main.py` lo traduce a `{"error", "error_type"}`
    en español (FR-011 · UX-001). Es distinto de `MediaValidationError` (media
    de búsqueda): aquí el recurso es el vídeo (UUID inválido / inexistente).
    """

    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.message = message


@router.get("/videos/{video_id}", response_model=VideoCard)
def video_card(video_id: str) -> VideoCard:
    """Ficha del vídeo (FR-008 · contracts §4): metadatos, fuente y enlace.

    El 400 por UUID inválido ocurre **antes** de tocar la BD (paridad SC-006:
    los 4xx de validación no ejecutan la consulta). El 503 de BD caída llega
    vía el handler global de `psycopg.Error` (contracts §5).
    """
    try:
        video_uuid = uuid.UUID(video_id)
    except ValueError:
        raise VideoCardError(
            400, "invalid_uuid", "el id del vídeo debe ser un UUID válido"
        ) from None

    card = _fetch_video_card(video_uuid)
    if card is None:
        raise VideoCardError(404, "video_not_found", "el vídeo no existe")
    return card


def _fetch_video_card(video_id: uuid.UUID) -> VideoCard | None:
    """Ficha desde `public.videos` vía `PgRepo` (patrón de la CLI del spike).

    Devuelve `None` si el vídeo no existe (→ 404 en el handler). Con el
    backend **in-memory** (tests/dev sin `SUPABASE_DB_URL`) la ficha no
    tiene fuente de datos: responde 503 `index_unavailable` **sin intentar
    conectar al DSN por defecto** (mismo criterio que `record_search` en
    PR-055: no tocar la BD cuando no está configurada). Un fallo de conexión
    con backend postgres propaga `psycopg.Error` (→ 503 del handler global).
    """
    if build_backend().label != "postgres":
        raise VideoCardError(
            503,
            "index_unavailable",
            "la ficha de vídeo requiere la BD del índice (backend in-memory)",
        )
    return asyncio.run(_fetch_video_card_async(video_id))


async def _fetch_video_card_async(video_id: uuid.UUID) -> VideoCard | None:
    """SELECT de la ficha con el join existente a `sources` (contracts §4)."""
    async with await PgRepo().connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "select v.id::text, v.local_ref, v.title, v.page_url, v.status, "
                "v.duration_ms, v.frame_count, v.tags, v.published_at, "
                "v.thumbnail_url, v.excluded, s.name "
                "from public.videos v "
                "left join public.sources s on s.id = v.source_id "
                "where v.id = %s",
                (video_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    (
        row_id,
        local_ref,
        title,
        page_url,
        status,
        duration_ms,
        frame_count,
        tags,
        published_at,
        thumbnail_url,
        excluded,
        source,
    ) = row
    return VideoCard(
        video_id=row_id,
        local_ref=local_ref,
        title=title,
        page_url=page_url,
        source=source,
        status=status,
        duration_ms=duration_ms,
        frame_count=frame_count,
        tags=tags,
        # `published_at` se normaliza a UTC: el contrato §4 muestra el
        # timestamp en Z y la serialización es estable entre entornos.
        published_at=published_at.astimezone(UTC) if published_at else None,
        thumbnail_url=thumbnail_url,
        excluded=excluded,
    )
