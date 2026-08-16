"""Contrato `SourceAdapter` + `AdapterManifest` (PR-020 · FR-001 · ADR-0009 · contracts §1).

El protocolo async `SourceAdapter` es la única frontera entre fuentes web y el
core de indexación/búsqueda: el core nunca ve HTML/JSON de la fuente, solo
`VideoSource`/`VisualAsset` (ADR-0009). `AdapterManifest` documenta el compliance
de cada fuente (SEC-002): el registry (PR-028) no habilita un adapter real sin
`robots_reviewed`/`terms_reviewed` en `true` y `review_date`.

Nota PR-020: el manifest es un modelo pydantic **frozen** (no un TypedDict como
en contracts §1) para exigir los campos de compliance en runtime; la firma de
campos y métodos es idéntica al contrato.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from xtrace_crawler.adapters.models import (
    AssetKind,
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)

# Jerarquía documentada de acceso a la fuente (FR-004): API/feed oficial → sitemap
# → JSON → HTML → navegador (último recurso). El manifest declara cuál usa el adapter.
AccessMethod = Literal["api", "sitemap", "json", "html", "browser"]


class RateLimitSpec(BaseModel):
    """Defaults de rate limit declarados por adapter (FR-009 · D5 · contracts §4).

    Overrides por entorno sin tocar código (se consumen en PR-022):
    `XTRACE_CRAWLER_RATE_<SOURCE>_MIN_INTERVAL_MS` y `XTRACE_CRAWLER_RATE_<SOURCE>_MAX_RPS`.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    min_interval_ms: int = Field(ge=0)
    max_rps: float = Field(ge=0)


class AdapterManifest(BaseModel):
    """Manifest de compliance por adapter (FR-001 · SEC-002 · ADR-0009 · contracts §1).

    Modelo frozen con los campos de compliance obligatorios: `robots_reviewed`,
    `terms_reviewed` y `rate_limit` son requeridos; el gate de habilitación vive
    en `adapters/registry.py` (PR-028).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    source: str
    access_method: AccessMethod
    assets_accessed: list[AssetKind]
    robots_reviewed: bool
    terms_reviewed: bool
    rate_limit: RateLimitSpec
    review_date: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocolo async único de acceso a una fuente (FR-001 · ADR-0009 · contracts §1).

    Regla de oro: el core nunca ve HTML/JSON de la web; solo
    `VideoSource`/`VisualAsset` (SC-007: añadir una fuente no toca el core).
    """

    manifest: AdapterManifest

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage: ...

    async def get_video(self, external_id: str) -> VideoSource | None: ...

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]: ...

    async def check_availability(self, video: VideoSource) -> VideoAvailability: ...
