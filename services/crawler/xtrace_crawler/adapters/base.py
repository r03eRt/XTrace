"""Contrato `SourceAdapter` + `AdapterManifest` (PR-020 · FR-001 · ADR-0009 · contracts §1).

El protocolo async `SourceAdapter` es la única frontera entre fuentes web y el
core de indexación/búsqueda: el core nunca ve HTML/JSON de la fuente, solo
`VideoSource`/`VisualAsset` (ADR-0009). `AdapterManifest` documenta el compliance
de cada fuente (SEC-002): el registry (PR-028) no habilita un adapter real sin
`robots_reviewed`/`terms_reviewed` en `true` y `review_date`.

Nota PR-020: el manifest es un modelo pydantic **frozen** (no un TypedDict como
en contracts §1) para exigir los campos de compliance en runtime; la firma de
campos y métodos es idéntica al contrato.

**Método opcional (PR-034 · FR-003 · SC-001 · contracts §1)**: un adapter PUEDE
implementar `fetch_asset_bytes(url) -> bytes | None` para servir los bytes de
sus visual assets **in-process, sin red** (p. ej. el `MockAdapter` con imágenes
sintéticas deterministas del catálogo). Semántica del contrato: `bytes` → el
pipeline (PR-030) los usa directamente; `None` —o adapter que no implementa el
método— → el pipeline descarga por HTTP (`AssetFetcher`/`SafeHTTPClient`, la
ruta actual de las fuentes reales, p. ej. xvideos: su contrato funcional NO
cambia). No se declara como miembro del cuerpo del protocolo a propósito: los
protocolos estructurales exigen **todos** los miembros declarados (mypy) y el
`runtime_checkable` exige su presencia en `isinstance`, lo que rompería la
compatibilidad de los adapters que no lo implementan; el pipeline lo descubre
con `getattr(adapter, "fetch_asset_bytes", None)` (ver docstring de la clase).
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
    """Defaults de rate limit declarados por adapter (FR-009 · D5 · contracts §1/§4).

    **Definición canónica ÚNICA** (alineación exigida por la revisión de la Ola A a
    PR-030 · contracts §1): `crawling/ratelimit.py` (PR-022) la **importa** y no
    redefine; el pipeline consume `manifest.rate_limit`. Overrides por entorno sin
    tocar código (se consumen en PR-022): `XTRACE_CRAWLER_RATE_<SOURCE>_MIN_INTERVAL_MS`
    y `XTRACE_CRAWLER_RATE_<SOURCE>_MAX_RPS`.

    `min_interval_ms` es el espaciado mínimo entre requests; `max_rps` el ritmo
    sostenido (token bucket con ráfaga inicial de 1 segundo de tokens), **estrictamente
    > 0** (`gt=0`, contracts §1: evita divisiones por cero en el limiter).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    min_interval_ms: int = Field(
        default=1000, ge=0, description="Intervalo mínimo entre requests (ms)"
    )
    max_rps: float = Field(
        default=1.0, gt=0, description="Ritmo sostenido máximo (requests/segundo)"
    )


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

    **Método opcional (PR-034 · FR-003 · SC-001)**: un adapter PUEDE implementar

        async def fetch_asset_bytes(self, url: str) -> bytes | None

    para servir los bytes de un visual asset **in-process, sin red** (el
    `MockAdapter` devuelve imágenes Pillow sintéticas y deterministas del
    catálogo). Semántica del contrato: `bytes` → el pipeline los usa
    directamente; `None` (o adapter sin el método) → descarga por HTTP con
    `AssetFetcher`/`SafeHTTPClient` (ruta de las fuentes reales; el contrato
    funcional de xvideos NO cambia). No se declara como miembro del cuerpo del
    protocolo: los protocolos estructurales (mypy strict) y el
    `runtime_checkable` (isinstance) exigirían el miembro en TODOS los
    adapters, rompiendo la compatibilidad de los que no lo implementan; el
    pipeline lo descubre con `getattr(adapter, "fetch_asset_bytes", None)`.
    """

    manifest: AdapterManifest

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage: ...

    async def get_video(self, external_id: str) -> VideoSource | None: ...

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]: ...

    async def check_availability(self, video: VideoSource) -> VideoAvailability: ...
