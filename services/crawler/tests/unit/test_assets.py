"""Tests de descarga y transformación de visual assets (PR-029 · FR-005 · FR-015 · SC-006).

Cubren el contrato PR-029 (contracts §7):

- `assets/storyboard.py` (FR-005): recorte de tiles de un sprite con Pillow
  (cols×rows + tamaño de tile), timestamp aproximado desde `position`/duración,
  determinismo (NFR-003).
- `assets/fetch.py` (FR-005/FR-015): descarga vía `SafeHTTPClient` (PR-024) a
  directorio temporal con `max_bytes` y cleanup `try/finally` garantizado.
- `assets/preview.py` (FR-005/SC-006): FFmpeg extrae frames de previews
  CORTOS con intervalo configurable y **rehúsa** cualquier input que parezca
  vídeo completo (duración > límite configurable, default 120 s), con error
  tipado. Nunca se descarga/procesa un vídeo completo (SC-006).

Todo sin red real (NFR-003): sprite sintético generado con Pillow y descargas
con `httpx.MockTransport`. Los tests de preview requieren FFmpeg; si no está
disponible se marcan `skip` con motivo documentado (contrato PR-029).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

from tests.fixtures.assets.preview_factory import ffmpeg_available, make_preview_mp4
from tests.fixtures.assets.sprite_factory import BORDER_COLOR, make_sprite, tile_color
from xtrace_crawler.adapters.models import AssetKind, VisualAsset
from xtrace_crawler.assets.fetch import DEFAULT_MAX_BYTES, AssetFetcher
from xtrace_crawler.assets.preview import (
    DEFAULT_MAX_PREVIEW_SECONDS,
    PreviewExtractionError,
    PreviewFrameExtractor,
    PreviewTooLongError,
)
from xtrace_crawler.assets.storyboard import StoryboardError, split_storyboard
from xtrace_crawler.crawling.http import DownloadTooLargeError, SafeHTTPClient

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(),
    reason="FFmpeg no disponible en este entorno: tests de preview omitidos "
    "(contrato PR-029: si ffmpeg está disponible se genera un mp4 corto con lavfi; si no, skip)",
)


def _run(coro: Callable[[], object]) -> None:
    """Ejecuta el escenario async sin dependencia de pytest-asyncio (determinista)."""
    asyncio.run(coro())


def _handler(
    *,
    status: int = 200,
    content: bytes = b"asset",
) -> Callable[[httpx.Request], httpx.Response]:
    """Handler de MockTransport que devuelve un body fijo (sin red, NFR-003)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content, request=request)

    return handler


def _asset(
    kind: AssetKind = "storyboard", url: str = "https://example.com/asset.jpg"
) -> VisualAsset:
    return VisualAsset(kind=kind, url=url)


def _leftover_asset_dirs() -> list[Path]:
    return [
        p
        for p in Path(tempfile.gettempdir()).iterdir()
        if p.is_dir() and p.name.startswith("xtrace-crawler-asset-")
    ]


# --- Storyboard: recorte de tiles (FR-005) -----------------------------------


def test_storyboard_numero_de_tiles_y_tamano_correctos() -> None:
    """FR-005: un sprite 3×4 de tiles 64×36 produce exactamente 12 tiles de 64×36.

    Posiciones row-major 0..11 con `row`/`col` coherentes (metadatos).
    """
    sprite = make_sprite(cols=3, rows=4, tile_w=64, tile_h=36)
    tiles = split_storyboard(sprite, cols=3, rows=4)

    assert len(tiles) == 12
    assert [t.position for t in tiles] == list(range(12))
    for tile in tiles:
        assert tile.image.size == (64, 36)
        assert tile.row == tile.position // 3
        assert tile.col == tile.position % 3


def test_storyboard_recorte_exacto_y_tiles_distinguibles() -> None:
    """FR-005: cada tile se recorta de su celda exacta y es distinguible.

    El píxel central de cada tile coincide con el color único de su `(row, col)`
    y el borde es blanco: los tiles no se mezclan ni se desplazan.
    """
    sprite = make_sprite(cols=3, rows=4, tile_w=64, tile_h=36)
    tiles = split_storyboard(sprite, cols=3, rows=4)

    for tile in tiles:
        assert tile.image.getpixel((32, 18)) == tile_color(tile.row, tile.col)
        assert tile.image.getpixel((0, 0)) == BORDER_COLOR


def test_storyboard_timestamps_aproximados_desde_position_y_duracion() -> None:
    """FR-005: timestamp_ms = round(position / total_tiles * duration_ms)."""
    sprite = make_sprite(cols=3, rows=4, tile_w=64, tile_h=36)
    tiles = split_storyboard(sprite, cols=3, rows=4, duration_ms=12_000)

    assert [t.timestamp_ms for t in tiles] == [round(i / 12 * 12_000) for i in range(12)]
    assert tiles[0].timestamp_ms == 0
    assert tiles[-1].timestamp_ms == 11_000
    # Coherencia: timestamps crecientes y siempre < duration_ms.
    timestamps = [t.timestamp_ms for t in tiles]
    assert timestamps == sorted(timestamps)
    assert all(ts is not None and ts < 12_000 for ts in timestamps)


def test_storyboard_sin_duracion_timestamps_none() -> None:
    """FR-005: si la fuente no da duración, los timestamps quedan en None (no falla)."""
    sprite = make_sprite(cols=2, rows=2, tile_w=64, tile_h=36)
    tiles = split_storyboard(sprite, cols=2, rows=2)

    assert [t.timestamp_ms for t in tiles] == [None, None, None, None]


def test_storyboard_determinismo() -> None:
    """NFR-003: dos recortes del mismo sprite producen tiles idénticos."""
    sprite = make_sprite(cols=3, rows=4, tile_w=64, tile_h=36)
    first = split_storyboard(sprite, cols=3, rows=4, duration_ms=9_000)
    second = split_storyboard(sprite, cols=3, rows=4, duration_ms=9_000)

    assert len(first) == len(second)
    for a, b in zip(first, second, strict=True):
        assert a.position == b.position
        assert a.timestamp_ms == b.timestamp_ms
        assert list(a.image.get_flattened_data()) == list(b.image.get_flattened_data())


def test_storyboard_tile_size_explicito() -> None:
    """FR-005: `tile_size` explícito recorta el grid exacto aunque el sprite
    tenga padding sobrante (se recorta la región superior-izquierda)."""
    # Sprite de 200×150 px con tiles declarados de 64×36 (padding sobrante).
    sprite = Image.new("RGB", (200, 150), (0, 0, 0))
    for row in range(4):
        for col in range(3):
            x0, y0 = col * 64, row * 36
            sprite.paste(Image.new("RGB", (64, 36), tile_color(row, col)), (x0, y0))

    tiles = split_storyboard(sprite, cols=3, rows=4, tile_size=(64, 36))

    assert len(tiles) == 12
    for tile in tiles:
        assert tile.image.size == (64, 36)
        assert tile.image.getpixel((32, 18)) == tile_color(tile.row, tile.col)


def test_storyboard_geometria_invalida_rechazada() -> None:
    """FR-005: grids/tiles inválidos o sprite más pequeño que el grid lanzan StoryboardError."""
    sprite = make_sprite(cols=3, rows=4, tile_w=64, tile_h=36)

    with pytest.raises(StoryboardError):
        split_storyboard(sprite, cols=0, rows=4)  # cols < 1
    with pytest.raises(StoryboardError):
        split_storyboard(sprite, cols=3, rows=0)  # rows < 1
    with pytest.raises(StoryboardError):
        split_storyboard(sprite, cols=3, rows=4, tile_size=(0, 36))  # tile inválido
    with pytest.raises(StoryboardError):
        # Sprite más pequeño que el grid declarado.
        split_storyboard(sprite, cols=5, rows=5)
    with pytest.raises(StoryboardError):
        # Sin tile_size: el sprite debe ser divisible por el grid (192×144 no es divisible por 5×4).
        split_storyboard(sprite, cols=5, rows=4)
    with pytest.raises(StoryboardError):
        split_storyboard(sprite, cols=3, rows=4, duration_ms=-1)  # duración negativa


# --- Fetch: descarga de assets permitidos (FR-005/FR-015) --------------------


def test_fetch_descarga_asset_a_directorio_temporal() -> None:
    """FR-005: la descarga escribe el asset en un temporal dedicado con el contenido exacto."""
    path: Path | None = None
    parent: Path | None = None

    async def scenario() -> None:
        nonlocal path, parent
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"asset-bytes")),
        ) as client:
            fetcher = AssetFetcher(client)
            async with fetcher.fetch(_asset()) as downloaded:
                path = downloaded
                parent = downloaded.parent
                assert downloaded.is_file()
                assert downloaded.read_bytes() == b"asset-bytes"
                assert downloaded.name.endswith(".jpg")  # storyboard → jpg
                assert parent.name.startswith("xtrace-crawler-asset-")
                assert str(parent).startswith(tempfile.gettempdir())

    _run(scenario)
    # Cleanup try/finally: al salir del contexto el directorio temporal ya no existe (FR-015).
    assert path is not None and parent is not None
    assert not parent.exists()


def test_fetch_sufijo_segun_tipo_de_asset() -> None:
    """FR-005: cada tipo de asset se guarda con su sufijo (preview → mp4)."""

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler()),
        ) as client:
            fetcher = AssetFetcher(client)
            async with fetcher.fetch(
                _asset(kind="preview", url="https://example.com/p.mp4")
            ) as preview:
                assert preview.name.endswith(".mp4")
            async with fetcher.fetch(
                _asset(kind="thumbnail", url="https://example.com/t.jpg")
            ) as thumb:
                assert thumb.name.endswith(".jpg")

    _run(scenario)


def test_fetch_respeta_max_bytes() -> None:
    """FR-005/FR-015: max_bytes aborta la descarga y no deja temporales."""
    before = _leftover_asset_dirs()

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"x" * 100)),
        ) as client:
            fetcher = AssetFetcher(client)
            with pytest.raises(DownloadTooLargeError):
                async with fetcher.fetch(_asset(), max_bytes=50):
                    pytest.fail("no debería llegar a yield con max_bytes superado")

    _run(scenario)
    assert _leftover_asset_dirs() == before


def test_fetch_usa_default_max_bytes_del_fetcher() -> None:
    """FR-005: sin max_bytes explícito aplica el default configurable del fetcher (10 MiB)."""
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"x" * 100)),
        ) as client:
            fetcher = AssetFetcher(client, default_max_bytes=50)
            with pytest.raises(DownloadTooLargeError):
                async with fetcher.fetch(_asset()):
                    pytest.fail("no debería llegar a yield con default_max_bytes superado")

    _run(scenario)


def test_fetch_error_http_contenido_y_sin_temporales() -> None:
    """FR-015: un error HTTP (404) se propaga y no quedan artefactos temporales."""
    before = _leftover_asset_dirs()

    async def scenario() -> None:
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(status=404, content=b"")),
        ) as client:
            fetcher = AssetFetcher(client)
            with pytest.raises(httpx.HTTPStatusError):
                async with fetcher.fetch(_asset(url="https://example.com/removed")):
                    pytest.fail("no debería llegar a yield con error HTTP")

    _run(scenario)
    assert _leftover_asset_dirs() == before


def test_fetch_limpia_temporales_si_el_llamador_falla() -> None:
    """FR-015: aunque el consumidor del asset falle, el try/finally elimina el temporal."""
    parent: Path | None = None

    async def scenario() -> None:
        nonlocal parent
        async with SafeHTTPClient(
            allowed_hosts={"example.com"},
            transport=httpx.MockTransport(_handler(content=b"data")),
        ) as client:
            fetcher = AssetFetcher(client)
            with pytest.raises(RuntimeError, match="fallo del llamador"):
                async with fetcher.fetch(_asset()) as downloaded:
                    parent = downloaded.parent
                    raise RuntimeError("fallo del llamador a mitad de uso")

    _run(scenario)
    assert parent is not None
    assert not parent.exists()


# --- Preview: frames de previews CORTOS, nunca vídeo completo (FR-005/SC-006) -


@requires_ffmpeg
def test_preview_extrae_frames_con_intervalo_configurable(tmp_path: Path) -> None:
    """FR-005: FFmpeg extrae un frame por intervalo (≈1 s) de un preview corto (2 s).

    Se acepta 2–3 frames (el filtro fps puede descartar el frame de cierre
    según versión); los timestamps son coherentes y monotónicos desde 0.
    """
    preview = make_preview_mp4(tmp_path / "preview.mp4", duration_s=2.0)
    extractor = PreviewFrameExtractor()
    frames = extractor.extract_frames(preview, interval_s=1.0, out_dir=tmp_path / "out")

    assert 2 <= len(frames) <= 3
    assert frames[0].timestamp_ms == 0
    timestamps = [f.timestamp_ms for f in frames]
    assert timestamps == sorted(timestamps)
    assert all(b - a == 1000 for a, b in zip(timestamps, timestamps[1:], strict=False))
    for frame in frames:
        assert frame.path.is_file()
        # Los frames son JPEGs decodificables (se abren con Pillow).
        with Image.open(frame.path) as img:
            img.load()


@requires_ffmpeg
def test_preview_determinismo(tmp_path: Path) -> None:
    """NFR-003: extraer dos veces del mismo preview produce los mismos timestamps."""
    preview = make_preview_mp4(tmp_path / "preview.mp4", duration_s=1.5)
    extractor = PreviewFrameExtractor()
    first = extractor.extract_frames(preview, interval_s=0.5, out_dir=tmp_path / "out1")
    second = extractor.extract_frames(preview, interval_s=0.5, out_dir=tmp_path / "out2")

    assert [f.timestamp_ms for f in first] == [f.timestamp_ms for f in second]
    assert len(first) >= 3  # 1.5 s / 0.5 s → al menos 3 frames


@requires_ffmpeg
def test_preview_rechaza_video_completo_por_duracion(tmp_path: Path) -> None:
    """SC-006: un input que excede el límite configurable se rehúsa con error tipado.

    El límite es configurable (default 120 s); aquí se baja a 1 s para no
    codificar un vídeo largo en el test.
    """
    preview = make_preview_mp4(tmp_path / "long.mp4", duration_s=2.0)
    extractor = PreviewFrameExtractor(max_duration_s=1.0)

    with pytest.raises(PreviewTooLongError):
        extractor.extract_frames(preview, interval_s=1.0, out_dir=tmp_path / "out")


def test_preview_limite_por_defecto_120_segundos() -> None:
    """SC-006: el límite por defecto que distingue preview corto de vídeo completo es 120 s."""
    assert DEFAULT_MAX_PREVIEW_SECONDS == 120.0


def test_preview_duracion_desconocida_rehusada(tmp_path: Path) -> None:
    """SC-006: si no se puede verificar que el input es corto, se rehúsa (nunca vídeo completo)."""
    not_a_video = tmp_path / "not_a_video.bin"
    not_a_video.write_bytes(b"esto no es un video")
    extractor = PreviewFrameExtractor()

    with pytest.raises(PreviewExtractionError):
        extractor.extract_frames(not_a_video, interval_s=1.0, out_dir=tmp_path / "out")


def test_preview_intervalo_invalido_rechazado(tmp_path: Path) -> None:
    """FR-005: intervalos <= 0 se rechazan antes de tocar ffmpeg."""
    extractor = PreviewFrameExtractor()

    with pytest.raises(ValueError):
        extractor.extract_frames(tmp_path / "nope.mp4", interval_s=0.0, out_dir=tmp_path / "out")
    with pytest.raises(ValueError):
        extractor.extract_frames(tmp_path / "nope.mp4", interval_s=-1.0, out_dir=tmp_path / "out")


def test_preview_max_duration_invalido_rechazado() -> None:
    """SC-006: el límite de duración debe ser > 0 (configuración inválida falla pronto)."""
    with pytest.raises(ValueError):
        PreviewFrameExtractor(max_duration_s=0.0)
    with pytest.raises(ValueError):
        PreviewFrameExtractor(max_duration_s=-5.0)
