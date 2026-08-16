"""Entidades normalizadas del contrato `SourceAdapter` (PR-020 · FR-002 · contracts §2).

Única frontera de datos entre adapters y core (ADR-0009): el core nunca ve el
HTML/JSON de la web, solo estos modelos. Validación pydantic estricta: modelos
frozen, sin campos extra, URLs http(s) con host y sin credenciales embebidas
(constitución §7), y campos opcionales `None` cuando la fuente no los expone.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Tipos de visual asset permitidos (FR-005 · SC-006): nunca el vídeo completo.
AssetKind = Literal["storyboard", "thumbnail", "preview"]


def _validate_http_url(value: str) -> str:
    """Valida una URL pública http(s) con host y sin credenciales embebidas."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL must use http(s), got: {value!r}")
    if not parsed.netloc:
        raise ValueError(f"URL must include a host, got: {value!r}")
    if parsed.username is not None:
        raise ValueError(f"URL must not embed credentials, got: {value!r}")
    return value


class VideoAvailability(StrEnum):
    """Estado de disponibilidad de un vídeo (FR-001 · contracts §1).

    La razón opcional del resultado se registra en la capa de jobs (error del
    job, FR-008 / edge cases) cuando `check_availability` no es `available`.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    REMOVED = "removed"


class DiscoverPage(BaseModel):
    """Página de `discover()`: IDs externos descubiertos + cursor de paginación (FR-001)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    external_ids: list[str]
    next_cursor: str | None = None


class VideoSource(BaseModel):
    """Entidad normalizada de vídeo web (FR-002 · contracts §2).

    Los campos opcionales son `None` cuando la fuente no los expone (spec edge
    cases: metadatos incompletos no bloquean el procesado del vídeo).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    source: str
    external_id: str
    title: str | None = None
    page_url: str
    duration_ms: int | None = Field(default=None, ge=0)
    thumbnail_url: str | None = None
    preview_url: str | None = None
    storyboard_urls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    published_at: datetime | None = None

    @field_validator("page_url", "thumbnail_url", "preview_url")
    @classmethod
    def _validate_http_url_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_http_url(value)

    @field_validator("storyboard_urls")
    @classmethod
    def _validate_storyboard_urls(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_http_url(value)
        return values


class VisualAsset(BaseModel):
    """Referencia a un asset visual de la fuente (FR-002 · contracts §2).

    Solo storyboard/thumbnail/preview; nunca un vídeo completo (SC-006).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: AssetKind
    url: str
    position: int | None = Field(default=None, ge=0)
    timestamp_ms: int | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _validate_http_url(value)
