"""Modelos del contrato REST de la API de búsqueda (PR-055 · FR-004/011 · UX-001
· contracts §1/§5).

`SearchResponse` reutiliza el JSON de la CLI `search` del spike (spec 001
contracts §1, FR-004): `search_id`, `processing_ms` y `results[]` con
`video_id`, `local_ref`, `match_score`, `matching_frames`,
`match_timestamp_ms` (nullable) y `evidence.visual`/`evidence.phash`.
La extensión **MAY** del contrato 003 §1 añade `title`/`page_url` (metadatos
de visualización, nullables) sin alterar los campos existentes.

`ApiError` es el cuerpo de error estructurado del contrato §5 (FR-011):
`{"error": "<mensaje en español>", "error_type": "<tipo-máquina>"}`.
"""

from __future__ import annotations

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


class ApiError(BaseModel):
    """Cuerpo de error estructurado (contracts §5 · FR-011 · UX-001).

    `error_type` es estable para consumo programático; `error` es el mensaje
    en español (idioma del frontend).
    """

    error: str
    error_type: str
