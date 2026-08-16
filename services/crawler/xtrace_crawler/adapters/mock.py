"""MockAdapter determinista sin red (PR-021 · FR-003 · SC-001 · contracts §1).

Adapter de prueba que implementa el protocolo `SourceAdapter` (FR-001) con un
**catálogo sintético generado por seed**: los `external_ids` son estables
(patrón `mock-vid-<índice>`), los metadatos son deterministas dado el seed y el
tamaño, y la paginación por cursor recorre el catálogo sin duplicados.

Permite ejecutar el **flujo completo offline** (discover → get_video →
get_visual_assets → check_availability) en CI, sin red y de forma repetible
(NFR-003, SC-001): las URLs sintéticas viven bajo `http://mock.local/` (dominio
de prueba que nunca se resuelve; ningún test abre sockets).

**Inyección de fallos** (tasks.md PR-021): `MockFaults` configura errores
**globales** (falla todo el método de la fuente, p. ej. para SC-008: una fuente
caída) o **por external_id** (solo ese vídeo). Los fallos se propagan como
**errores tipados del adapter** (jerarquía `MockAdapterError`), de modo que el
worker/pipeline (PR-027/PR-030) puede clasificarlos con la política de retries
(FR-008): transitorio → reintentar, timeout → reintentar, removed → terminal
(sin reintentos; el mensaje incluye "removed", alineado con
`jobs/backoff.classify_error`, contracts §3).

`check_availability` expone **estados configurables** por external_id (con
default global), soporte a FR-012 (estados available/unavailable/removed).

Regla de oro del contrato (ADR-0009): el mock **no hace red** y el core nunca
ve HTML/JSON; solo `VideoSource`/`VisualAsset` del contrato.

**Assets in-process (PR-034 · FR-003 · SC-001)**: el mock implementa el método
opcional del contrato `fetch_asset_bytes(url)` (ver `adapters/base.py`): sirve
los bytes JPEG de sus assets como **imágenes Pillow sintéticas y deterministas**
generadas desde la URL del catálogo (`synthetic_sprite`/`synthetic_thumbnail`,
las mismas que los tests de integración ya usaban en su router). Hallazgo de la
validación del quickstart (PR-033): con el cableado real (CLI + worker + repos +
`SafeHTTPClient`), las URLs `http://mock.local/...` pasaban por el cliente HTTP
real (allowlist/esquema lo bloquean) y los jobs `INDEX_VIDEO` degradaban TODOS
los assets → 0 vídeos indexados. Ahora el pipeline prefiere los bytes del
adapter (in-process, sin red) y solo cae a `AssetFetcher`/`SafeHTTPClient`
cuando `fetch_asset_bytes` devuelve `None` (el `preview.mp4`, que no se fabrica
sin ffmpeg, degrada con warning y el vídeo se indexa con storyboard+thumbnails).
"""

from __future__ import annotations

import io
import random
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, NoReturn
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)

#: Seed por defecto del catálogo sintético (determinista entre ejecuciones, NFR-003).
DEFAULT_SEED: int = 42
#: Tamaño de catálogo por defecto.
DEFAULT_CATALOG_SIZE: int = 50
#: Base de las URLs sintéticas del mock (dominio de prueba, nunca resuelto).
MOCK_BASE_URL: str = "http://mock.local"

#: Cada cuántos vídeos uno **sin storyboard** (edge case de la spec: degradación
#: a thumbnails + preview sin fallar todo el vídeo).
_NO_STORYBOARD_EVERY: int = 5
#: Número de tiles de storyboard por vídeo (cuando hay storyboard).
_STORYBOARD_TILES: int = 6
#: Número de thumbnails por vídeo.
_THUMBNAILS: int = 3

# -- Imágenes sintéticas de los assets (PR-034 · FR-003 · SC-001) --------------
#
# Los bytes de los assets del catálogo se generan **in-process** con Pillow, de
# forma determinista: mismo URL → mismos bytes entre llamadas, instancias y
# procesos (NFR-003). Los tamaños/colores son paridad exacta con el router que
# los tests de integración ya servían por `httpx.MockTransport` (PR-029): el
# pipeline indexa exactamente las mismas imágenes con o sin transporte fake.

#: Grid del sprite storyboard sintético (paridad con tests/fixtures/assets/sprite_factory.py).
_SPRITE_COLS: int = 2
_SPRITE_ROWS: int = 2
_SPRITE_TILE_W: int = 48
_SPRITE_TILE_H: int = 27
#: Sello de unicidad por vídeo pegado en el sprite (paridad con los tests de integración).
_SPRITE_STAMP_SIZE: tuple[int, int] = (24, 13)
_SPRITE_STAMP_POSITION: tuple[int, int] = (36, 20)


def _tile_color(row: int, col: int) -> tuple[int, int, int]:
    """Color único y estable de la tile (paridad con `sprite_factory.tile_color`)."""
    return (
        (row * 53 + col * 29) % 256,
        (row * 17 + col * 71) % 256,
        (row * 91 + col * 13) % 256,
    )


def _make_sprite() -> Image.Image:
    """Sprite storyboard 2×2 de tiles con borde blanco (paridad con `make_sprite`)."""
    sprite = Image.new("RGB", (_SPRITE_COLS * _SPRITE_TILE_W, _SPRITE_ROWS * _SPRITE_TILE_H))
    draw = ImageDraw.Draw(sprite)
    for row in range(_SPRITE_ROWS):
        for col in range(_SPRITE_COLS):
            x0, y0 = col * _SPRITE_TILE_W, row * _SPRITE_TILE_H
            draw.rectangle(
                [x0, y0, x0 + _SPRITE_TILE_W - 1, y0 + _SPRITE_TILE_H - 1],
                fill=_tile_color(row, col),
                outline=(255, 255, 255),
                width=1,
            )
    return sprite


def synthetic_sprite(external_id: str) -> Image.Image:
    """Sprite storyboard determinista del vídeo (PR-034 · FR-003 · SC-001).

    Imagen Pillow generada desde la URL del catálogo: mismo `external_id` →
    misma imagen entre llamadas, instancias y procesos (NFR-003); vídeos
    distintos → sprites distintos (sello de `zlib.crc32(external_id)`), la
    unicidad que las búsquedas SC-002 necesitan.
    """
    sprite = _make_sprite()
    seed = zlib.crc32(external_id.encode())
    stamp = Image.new(
        "RGB", _SPRITE_STAMP_SIZE, (seed % 256, (seed >> 8) % 256, (seed >> 16) % 256)
    )
    sprite.paste(stamp, _SPRITE_STAMP_POSITION)
    return sprite


def synthetic_thumbnail(position: int) -> Image.Image:
    """Miniatura determinista del catálogo: color de tile por posición (PR-034)."""
    return Image.new("RGB", (_SPRITE_TILE_W, _SPRITE_TILE_H), _tile_color(position, 0))


def _jpeg_bytes(image: Image.Image) -> bytes:
    """Codifica la imagen a JPEG (los tests de integración reutilizan los bytes)."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def synthetic_asset_bytes(url: str) -> bytes | None:
    """Bytes JPEG sintéticos de un asset del catálogo (PR-034); `None` si no hay
    representación in-process (semántica del contrato: descargar por HTTP).

    Solo las URLs del catálogo (`http://mock.local/assets/<external_id>/...`)
    se fabrican: `storyboard.jpg` → sprite del vídeo; `thumb-<n>.jpg` →
    miniatura por posición; `preview.mp4` → `None` (el mock no genera mp4 sin
    ffmpeg; el preview degrada con warning y el vídeo se indexa con
    storyboard+thumbnails — jerarquía de assets, FR-005). Cualquier otra URL
    (ajena al mock, malformada o fuera del patrón) → `None`.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "mock.local":
        return None
    prefix = "/assets/"
    path = parsed.path
    if not path.startswith(prefix):
        return None
    external_id, _, filename = path[len(prefix) :].partition("/")
    if filename == "storyboard.jpg":
        return _jpeg_bytes(synthetic_sprite(external_id))
    if filename.startswith("thumb-") and filename.endswith(".jpg"):
        try:
            position = int(filename.removeprefix("thumb-").removesuffix(".jpg"))
        except ValueError:
            return None
        return _jpeg_bytes(synthetic_thumbnail(position))
    return None  # p. ej. preview.mp4 → descarga HTTP (y degrada sin red)


#: Títulos sintéticos (anonimizados; sin contenido real — SEC-004).
_TITLES: tuple[str, ...] = (
    "Sundown Ride",
    "City Lights Walk",
    "Quiet Forest Stream",
    "Morning Market",
    "Coastal Drive",
    "Night Sky Timelapse",
    "Old Town Alley",
    "Garden In Bloom",
    "Harbor Sunset",
    "Desert Road",
    "Rain On Windows",
    "Neon District",
    "Countryside Train",
    "Mountain Trail",
    "Autumn Leaves",
    "Street Musicians",
)

#: Pool de tags sintéticos.
_TAGS: tuple[str, ...] = (
    "nature",
    "city",
    "travel",
    "food",
    "music",
    "art",
    "tech",
    "people",
    "animals",
    "scenery",
    "night",
    "weather",
)

#: Tipos de fallo inyectable (tasks.md PR-021): transitorio, timeout y
#: terminal/removed (sin reintentos, contracts §3).
FaultKind = Literal["transient", "timeout", "removed"]


class MockAdapterError(Exception):
    """Base de los errores tipados del MockAdapter (PR-021 · FR-003)."""


class MockAdapterTransientError(MockAdapterError):
    """Fallo transitorio inyectado (reintentable con backoff, FR-008)."""


class MockAdapterTimeoutError(MockAdapterTransientError):
    """Timeout inyectado (transitorio, paridad con los timeouts de red)."""


class MockAdapterRemovedError(MockAdapterError):
    """Contenido retirado/eliminado inyectado (terminal: sin reintentos, contracts §3)."""


@dataclass(frozen=True)
class MockFaults:
    """Configuración de fallos inyectables del MockAdapter (tasks.md PR-021).

    - Fallos **globales** por método (`discover`, `get_video`,
      `get_visual_assets`): falla toda la fuente en ese método (SC-008:
      una fuente caída no impide procesar otras).
    - Fallos **por external_id** (`by_external_id`): solo ese vídeo, tanto en
      `get_video` como en `get_visual_assets` (un "vídeo fallido").
    - `check_availability` no lanza errores: los **estados** son configurables
      por external_id en `availability`, con `availability_default` para el
      resto (FR-012).
    """

    discover: FaultKind | None = None
    get_video: FaultKind | None = None
    get_visual_assets: FaultKind | None = None
    by_external_id: dict[str, FaultKind] = field(default_factory=dict)
    availability: dict[str, VideoAvailability] = field(default_factory=dict)
    availability_default: VideoAvailability = VideoAvailability.AVAILABLE

    def __post_init__(self) -> None:
        for kind in (self.discover, self.get_video, self.get_visual_assets):
            if kind is not None and kind not in ("transient", "timeout", "removed"):
                raise ValueError(f"FaultKind desconocido: {kind!r}")
        for external_id, kind in self.by_external_id.items():
            if kind not in ("transient", "timeout", "removed"):
                raise ValueError(f"FaultKind desconocido para {external_id!r}: {kind!r}")


class MockAdapter:
    """Adapter determinista sin red que cumple el protocolo `SourceAdapter` (FR-003).

    El catálogo sintético se genera **una vez** en `__init__` con un
    `random.Random(seed)`: dado el mismo seed y tamaño, dos instancias producen
    exactamente el mismo catálogo (SC-001) y el resultado no depende del orden
    de llamadas (los metadatos no se derivan de un RNG por request).
    """

    manifest: AdapterManifest

    def __init__(
        self,
        *,
        seed: int = DEFAULT_SEED,
        catalog_size: int = DEFAULT_CATALOG_SIZE,
        faults: MockFaults | None = None,
    ) -> None:
        """Crea el adapter con catálogo sintético `catalog_size` y `seed` dados.

        `faults` se **copia** en la construcción: el comportamiento queda fijado
        aunque el llamador mute los dicts después (determinismo, SC-001).
        """
        if catalog_size < 0:
            raise ValueError(f"catalog_size debe ser >= 0; recibido {catalog_size}")
        self.seed = seed
        self.catalog_size = catalog_size
        source = MockFaults() if faults is None else faults
        self.faults = MockFaults(
            discover=source.discover,
            get_video=source.get_video,
            get_visual_assets=source.get_visual_assets,
            by_external_id=dict(source.by_external_id),
            availability=dict(source.availability),
            availability_default=source.availability_default,
        )
        self.manifest = AdapterManifest(
            source="mock",
            access_method="json",
            assets_accessed=["storyboard", "thumbnail", "preview"],
            # Contenido 100% sintético (SEC-004): no hay revisión legal pendiente
            # y el mock puede habilitarse para tests; el gate real de fuentes
            # (SEC-002) vive en `adapters/registry.py` (PR-028).
            robots_reviewed=True,
            terms_reviewed=True,
            rate_limit=RateLimitSpec(min_interval_ms=100, max_rps=10.0),
        )
        self._catalog: dict[str, VideoSource] = self._build_catalog(seed, catalog_size)
        self._ids: list[str] = list(self._catalog)
        self._index_by_id: dict[str, int] = {
            external_id: i for i, external_id in enumerate(self._ids)
        }

    # -- Contrato SourceAdapter (FR-001) -----------------------------------

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
        """Página de `external_ids` con paginación por cursor (FR-003).

        El cursor es opaco (índice del siguiente inicio como string); `None`
        cuando el catálogo está agotado.
        """
        self._raise_global_fault(self.faults.discover, "discover")
        if limit < 1:
            raise ValueError(f"limit debe ser >= 1; recibido {limit}")
        start = 0 if cursor is None else self._parse_cursor(cursor)
        if start >= len(self._ids):
            return DiscoverPage(external_ids=[], next_cursor=None)
        end = min(start + limit, len(self._ids))
        return DiscoverPage(
            external_ids=self._ids[start:end],
            next_cursor=str(end) if end < len(self._ids) else None,
        )

    async def get_video(self, external_id: str) -> VideoSource | None:
        """Metadatos del vídeo del catálogo; `None` si no existe (contracts §1)."""
        self._raise_video_fault(external_id, "get_video", self.faults.get_video)
        return self._catalog.get(external_id)

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        """Assets storyboard/thumbnail/preview del vídeo (FR-005; nunca `video`, SC-006).

        Un vídeo fuera del catálogo se considera retirado de la fuente: error
        terminal `MockAdapterRemovedError` (edge case 404/removed de la spec).
        """
        self._raise_video_fault(
            video.external_id, "get_visual_assets", self.faults.get_visual_assets
        )
        if video.external_id not in self._catalog:
            raise MockAdapterRemovedError(
                f"mock adapter: video removed (fuera del catálogo) en get_visual_assets "
                f"({video.external_id})"
            )
        return self._assets_for(video)

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        """Estado configurable del vídeo (FR-012): mapa por id > default global.

        Un vídeo fuera del catálogo → `removed` (no existe en la fuente).
        """
        if video.external_id not in self._catalog:
            return VideoAvailability.REMOVED
        return self.faults.availability.get(video.external_id, self.faults.availability_default)

    # -- Método opcional del contrato (PR-034 · FR-003 · SC-001) ---------------

    async def fetch_asset_bytes(self, url: str) -> bytes | None:
        """Bytes del asset servidos **in-process, sin red** (contracts §1, PR-034).

        Devuelve los bytes JPEG sintéticos y deterministas del catálogo
        (imágenes Pillow de `synthetic_asset_bytes`: las mismas que los tests
        de integración ya servían). Semántica del contrato: `None` → el
        pipeline descarga por HTTP (`AssetFetcher`/`SafeHTTPClient`); el mock
        solo devuelve `None` para `preview.mp4` (sin ffmpeg no fabrica mp4: el
        preview degrada con warning y el vídeo se indexa con
        storyboard+thumbnails — SC-001/SC-002 sin red).
        """
        return synthetic_asset_bytes(url)

    # -- Soporte a fixtures/harness (PR-021) --------------------------------

    def catalog_ids(self) -> list[str]:
        """External_ids del catálogo en orden de descubrimiento (harness/tests)."""
        return list(self._ids)

    def catalog_snapshot(self) -> dict[str, VideoSource]:
        """Copia del catálogo (id → VideoSource) para expectativas de tests."""
        return dict(self._catalog)

    def get_catalog_video(self, external_id: str) -> VideoSource | None:
        """Acceso síncrono al catálogo **sin** inyección de fallos (harness/tests).

        Permite construir los valores esperados de un caso aunque el adapter
        esté configurado con fallos (los fallos solo se disparan vía los
        métodos async del contrato).
        """
        return self._catalog.get(external_id)

    # -- Internos ------------------------------------------------------------

    def _build_catalog(self, seed: int, size: int) -> dict[str, VideoSource]:
        """Catálogo sintético determinista: mismo seed → mismo catálogo (SC-001).

        El RNG solo se consume aquí, en orden fijo por índice: el resultado no
        depende del orden de llamadas posterior (determinismo estricto).
        """
        rng = random.Random(seed)
        epoch = datetime(2020, 1, 1, tzinfo=UTC)
        catalog: dict[str, VideoSource] = {}
        for index in range(size):
            external_id = f"mock-vid-{index:04d}"
            has_storyboard = index % _NO_STORYBOARD_EVERY != 0
            catalog[external_id] = VideoSource(
                source="mock",
                external_id=external_id,
                title=f"{rng.choice(_TITLES)} #{index:04d}",
                page_url=f"{MOCK_BASE_URL}/videos/{external_id}",
                duration_ms=rng.randint(30_000, 3_600_000),
                thumbnail_url=f"{MOCK_BASE_URL}/assets/{external_id}/thumb-0.jpg",
                preview_url=f"{MOCK_BASE_URL}/assets/{external_id}/preview.mp4",
                storyboard_urls=(
                    [f"{MOCK_BASE_URL}/assets/{external_id}/storyboard.jpg"]
                    if has_storyboard
                    else []
                ),
                tags=rng.sample(_TAGS, k=rng.randint(2, 5)),
                published_at=epoch + timedelta(days=rng.randint(0, 2400)),
            )
        return catalog

    def _assets_for(self, video: VideoSource) -> list[VisualAsset]:
        """Assets deterministas del vídeo, derivados del catálogo (sin RNG).

        - storyboard: `_STORYBOARD_TILES` tiles con posición secuencial y
          timestamp aproximado dentro de la duración (FR-005); vídeos con
          `index % _NO_STORYBOARD_EVERY == 0` no tienen storyboard (degradación
          a thumbnails + preview, spec edge cases).
        - thumbnail: `_THUMBNAILS` miniaturas sin referencia temporal fiable.
        - preview: un preview corto (nunca el vídeo completo, SC-006).
        """
        external_id = video.external_id
        index = self._index_by_id[external_id]
        assets: list[VisualAsset] = []
        if index % _NO_STORYBOARD_EVERY != 0:
            duration = video.duration_ms
            for position in range(_STORYBOARD_TILES):
                assets.append(
                    VisualAsset(
                        kind="storyboard",
                        url=f"{MOCK_BASE_URL}/assets/{external_id}/storyboard.jpg",
                        position=position,
                        timestamp_ms=(
                            position * duration // _STORYBOARD_TILES
                            if duration is not None
                            else None
                        ),
                    )
                )
        for position in range(_THUMBNAILS):
            assets.append(
                VisualAsset(
                    kind="thumbnail",
                    url=f"{MOCK_BASE_URL}/assets/{external_id}/thumb-{position}.jpg",
                    position=position,
                    timestamp_ms=None,
                )
            )
        assets.append(
            VisualAsset(
                kind="preview",
                url=f"{MOCK_BASE_URL}/assets/{external_id}/preview.mp4",
                position=0,
                timestamp_ms=None,
            )
        )
        return assets

    def _raise_global_fault(self, kind: FaultKind | None, context: str) -> None:
        if kind is not None:
            self._raise_fault(kind, context)

    def _raise_video_fault(
        self, external_id: str, context: str, global_kind: FaultKind | None
    ) -> None:
        """Fallo por external_id (más específico) gana al fallo global del método."""
        kind = self.faults.by_external_id.get(external_id)
        if kind is not None:
            self._raise_fault(kind, f"{context} ({external_id})")
        self._raise_global_fault(global_kind, context)

    def _raise_fault(self, kind: FaultKind, context: str) -> NoReturn:
        """Propaga el fallo como error tipado del adapter (tasks.md PR-021)."""
        if kind == "transient":
            raise MockAdapterTransientError(
                f"fallo transitorio inyectado del mock adapter en {context}"
            )
        if kind == "timeout":
            raise MockAdapterTimeoutError(f"timeout inyectado del mock adapter en {context}")
        raise MockAdapterRemovedError(f"mock adapter: video removed (fallo inyectado) en {context}")

    @staticmethod
    def _parse_cursor(cursor: str) -> int:
        """Interpreta el cursor opaco como índice; valores inválidos → ValueError."""
        try:
            start = int(cursor)
        except ValueError:
            raise ValueError(f"cursor de paginación inválido: {cursor!r}") from None
        if start < 0:
            raise ValueError(f"cursor de paginación inválido (negativo): {cursor!r}")
        return start
