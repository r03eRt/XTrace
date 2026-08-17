"""Modelos del contrato REST de la API de búsqueda (PR-055/056 · FR-004/007/008/011
· UX-001 · contracts §1/§3/§4/§5).

`SearchResponse` reutiliza el JSON de la CLI `search` del spike (spec 001
contracts §1, FR-004): `search_id`, `processing_ms` y `results[]` con
`video_id`, `local_ref`, `match_score`, `matching_frames`,
`match_timestamp_ms` (nullable) y `evidence.visual`/`evidence.phash`.
La extensión **MAY** del contrato 003 §1 añade `title`/`page_url` (metadatos
de visualización, nullables) sin alterar los campos existentes.

`Stats` (contracts §3, FR-007) replica los campos de la CLI `stats` del
spike; `VideoCard` (contracts §4, FR-008) es la ficha del vídeo con sus
metadatos, fuente y enlace original.

`ApiError` es el cuerpo de error estructurado del contrato §5 (FR-011):
`{"error": "<mensaje en español>", "error_type": "<tipo-máquina>"}`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """Evidencia de coincidencia de un resultado (paridad CLI, contracts §1)."""

    visual: float
    phash: float


class SearchResultItem(BaseModel):
    """Un vídeo rankeado de `POST /search` (contracts §1).

    `local_ref` puede ser `null` (backend in-memory, paridad CLI);
    `match_timestamp_ms` puede ser `null` (frame sin timestamp fiable,
    paridad FR-012 del spike); `title`/`page_url` pueden ser `null` (vídeos
    locales solo con `local_ref`; extensión MAY, FR-004).
    """

    video_id: str
    local_ref: str | None = None
    title: str | None = None
    page_url: str | None = None
    match_score: float
    matching_frames: int
    match_timestamp_ms: int | None = None
    evidence: Evidence


class SearchResponse(BaseModel):
    """Respuesta 200 de `POST /search` (contracts §1, FR-004)."""

    search_id: str
    processing_ms: int = Field(ge=0)
    results: list[SearchResultItem]


class Stats(BaseModel):
    """Métricas del índice (contracts §3 · FR-007): mismos campos que la CLI `stats`.

    `videos` = vídeos con ≥ 1 frame indexado (criterio de `VectorStore.stats()`
    del spike); `backend` es la etiqueta estable (`postgres` | `in-memory`) y
    `embedding_provider` el `model_id` del proveedor activo.
    """

    videos: int
    frames: int
    vectors: int
    backend: str
    embedding_provider: str


class VideoCard(BaseModel):
    """Ficha del vídeo (contracts §4 · FR-008): metadatos, fuente y enlace original.

    Nullables: `title`, `page_url`, `source` (vídeos locales sin fuente),
    `duration_ms`, `tags`, `published_at`, `thumbnail_url` (contracts §4).
    `source` es el nombre de la fuente (`sources.name`, join existente).
    """

    video_id: str
    local_ref: str
    title: str | None = None
    page_url: str | None = None
    source: str | None = None
    status: str
    duration_ms: int | None = None
    frame_count: int
    tags: list[str] | None = None
    published_at: datetime | None = None
    thumbnail_url: str | None = None
    excluded: bool


class ApiError(BaseModel):
    """Cuerpo de error estructurado (contracts §5 · FR-011 · UX-001).

    `error_type` es estable para consumo programático; `error` es el mensaje
    en español (idioma del frontend).
    """

    error: str
    error_type: str
