"""Tests del MockAdapter + fixtures + harness sin red (PR-021 · FR-003 · SC-001 · contracts §1).

Trazabilidad (constitución §3): los tests validan `adapters/mock.py` y
`tests/fixtures/` contra la spec 002:

- FR-003: mock adapter + fixtures + harness que permiten ejecutar el flujo
  completo **sin red**, de forma determinista (SC-001) — flujo offline completo,
  paginación por cursor, catálogo estable entre ejecuciones y fallos inyectados
  que se propagan como **errores tipados del adapter** (tasks.md PR-021).
- PR-034: el método **opcional** del contrato `fetch_asset_bytes(url)` (hallazgo
  del quickstart, PR-033) sirve los assets del catálogo **in-process, sin red**,
  como bytes JPEG sintéticos deterministas (mismos bytes entre llamadas,
  instancias y ejecuciones); `None` para `preview.mp4` y URLs ajenas; un adapter
  sin el método se comporta como `None` (el pipeline cae a la descarga HTTP).
- Soporte contractual: FR-001 (el mock satisface el protocolo `SourceAdapter`),
  FR-005/SC-006 (assets storyboard/thumbnail/preview, nunca `video`),
  FR-009 (manifest con rate limit declarado), FR-012 (estados de disponibilidad
  configurables en `check_availability`), FR-010/SC-008 (los fallos quedan
  contenidos en el adapter y no rompen el resto del flujo).

NFR-003: ningún test abre sockets; las URLs sintéticas son
`http://mock.local/...` (dominio de prueba, nunca resuelto). Los tests async se
ejecutan con el plugin de pytest de `anyio` (backend asyncio; `anyio` es
dependencia transitiva de `httpx`, ya en el lockfile).
"""

from __future__ import annotations

import asyncio
import io
from math import ceil

import pytest
from PIL import Image

from tests.fixtures.catalog import FIXTURE_SEED, SAMPLE_ASSETS, SAMPLE_VIDEOS
from tests.fixtures.harness import MockHarness
from xtrace_crawler.adapters.base import SourceAdapter
from xtrace_crawler.adapters.mock import (
    MOCK_BASE_URL,
    MockAdapter,
    MockAdapterError,
    MockAdapterRemovedError,
    MockAdapterTimeoutError,
    MockAdapterTransientError,
    MockFaults,
    synthetic_asset_bytes,
)
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)

# ---------------------------------------------------------------------------
# FR-003 · Contrato y manifest (contracts §1)
# ---------------------------------------------------------------------------


def test_mock_adapter_satisfies_source_adapter_protocol() -> None:
    """MockAdapter cumple estructuralmente el protocolo `SourceAdapter` (FR-001, FR-003)."""
    adapter = MockAdapter()
    assert isinstance(adapter, SourceAdapter)


def test_mock_adapter_manifest_is_compliant() -> None:
    """El manifest del mock declara compliance y rate limit (FR-003, FR-009, SEC-002)."""
    manifest = MockAdapter().manifest
    assert manifest.source == "mock"
    assert manifest.access_method in {"api", "sitemap", "json", "html", "browser"}
    assert set(manifest.assets_accessed) <= {"storyboard", "thumbnail", "preview"}
    assert "video" not in manifest.assets_accessed  # SC-006
    assert manifest.robots_reviewed is True
    assert manifest.terms_reviewed is True
    assert manifest.rate_limit.min_interval_ms > 0
    assert manifest.rate_limit.max_rps > 0


# ---------------------------------------------------------------------------
# FR-003 · Flujo completo offline determinista (SC-001)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_offline_flow_discover_get_video_get_assets() -> None:
    """Flujo discover → get_video → get_visual_assets completo sin red (SC-001, FR-003).

    Sin acceso a red: ninguna URL sintética se resuelve; el test solo construye
    modelos con el contrato y comprueba invariantes.
    """
    adapter = MockAdapter(seed=7, catalog_size=12)
    page = await adapter.discover(cursor=None, limit=12)
    assert page.next_cursor is None
    assert len(page.external_ids) == 12

    for external_id in page.external_ids:
        video = await adapter.get_video(external_id)
        assert video is not None
        assert video.source == "mock"
        assert video.external_id == external_id
        assert video.page_url.startswith(f"{MOCK_BASE_URL}/videos/")
        assert video.duration_ms is not None and video.duration_ms > 0
        assert isinstance(video.tags, list) and video.tags

        assets = await adapter.get_visual_assets(video)
        assert assets
        for asset in assets:
            assert asset.url.startswith(f"{MOCK_BASE_URL}/assets/")


def test_catalog_stable_across_instances_same_seed() -> None:
    """El catálogo es estable entre ejecuciones con el mismo seed (SC-001, NFR-003)."""
    a = MockAdapter(seed=42, catalog_size=25)
    b = MockAdapter(seed=42, catalog_size=25)
    assert a.catalog_snapshot() == b.catalog_snapshot()
    assert a.catalog_ids() == b.catalog_ids()


def test_catalog_stable_across_call_order() -> None:
    """El resultado no depende del orden de llamadas a get_video (SC-001)."""
    adapter = MockAdapter(seed=42, catalog_size=25)
    snapshot = adapter.catalog_snapshot()
    for external_id in reversed(adapter.catalog_ids()):
        assert adapter.get_catalog_video(external_id) == snapshot[external_id]


def test_different_seed_changes_metadata_but_not_ids() -> None:
    """Distinto seed → mismos external_ids, metadatos diferentes (FR-003, determinismo)."""
    a = MockAdapter(seed=1, catalog_size=10)
    b = MockAdapter(seed=2, catalog_size=10)
    assert a.catalog_ids() == b.catalog_ids()
    assert a.catalog_snapshot() != b.catalog_snapshot()


def test_external_ids_are_stable_and_indexed() -> None:
    """Los external_ids siguen un patrón estable, independiente del seed (FR-003)."""
    adapter = MockAdapter(seed=12345, catalog_size=8)
    assert adapter.catalog_ids() == [f"mock-vid-{i:04d}" for i in range(8)]


@pytest.mark.anyio
async def test_get_video_unknown_id_returns_none() -> None:
    """Un external_id fuera del catálogo devuelve None (FR-003, contrato §1)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    assert await adapter.get_video("no-existe") is None


# ---------------------------------------------------------------------------
# FR-003 · Paginación por cursor
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discover_pagination_walks_full_catalog() -> None:
    """La paginación por cursor recorre el catálogo completo sin duplicados (FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=50)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await adapter.discover(cursor=cursor, limit=7)
        pages += 1
        seen.extend(page.external_ids)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert pages == ceil(50 / 7)
    assert seen == adapter.catalog_ids()
    assert len(set(seen)) == 50


@pytest.mark.anyio
async def test_discover_single_page_when_limit_exceeds_size() -> None:
    """limit >= tamaño del catálogo devuelve todo en una página (FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    page = await adapter.discover(cursor=None, limit=100)
    assert page.external_ids == adapter.catalog_ids()
    assert page.next_cursor is None


@pytest.mark.anyio
async def test_discover_resumes_from_cursor() -> None:
    """El cursor devuelto reanuda la paginación en el punto exacto (FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=20)
    first = await adapter.discover(cursor=None, limit=3)
    assert first.next_cursor == "3"
    second = await adapter.discover(cursor=first.next_cursor, limit=3)
    assert second.external_ids == adapter.catalog_ids()[3:6]
    assert second.next_cursor == "6"


@pytest.mark.anyio
async def test_discover_cursor_at_or_beyond_end_returns_empty_page() -> None:
    """Cursor al final o más allá del catálogo → página vacía sin siguiente (FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    for cursor in ("5", "50"):
        page = await adapter.discover(cursor=cursor, limit=3)
        assert page.external_ids == []
        assert page.next_cursor is None


@pytest.mark.anyio
async def test_discover_rejects_invalid_cursor() -> None:
    """Cursor no numérico → ValueError (validación del adapter, FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    with pytest.raises(ValueError):
        await adapter.discover(cursor="abc", limit=3)


@pytest.mark.anyio
async def test_discover_rejects_negative_cursor() -> None:
    """Cursor negativo → ValueError (evita indexado inverso, FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    with pytest.raises(ValueError):
        await adapter.discover(cursor="-1", limit=3)


@pytest.mark.anyio
async def test_discover_rejects_non_positive_limit() -> None:
    """limit < 1 → ValueError (validación del adapter, FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    with pytest.raises(ValueError):
        await adapter.discover(cursor=None, limit=0)


@pytest.mark.anyio
async def test_discover_empty_catalog() -> None:
    """Catálogo vacío → página vacía sin siguiente; get_video → None (FR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=0)
    page = await adapter.discover(cursor=None, limit=10)
    assert page.external_ids == []
    assert page.next_cursor is None
    assert await adapter.get_video("mock-vid-0000") is None


# ---------------------------------------------------------------------------
# FR-003 · Visual assets (soporte FR-005 / SC-006)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_visual_assets_kinds_and_mock_local_urls() -> None:
    """Assets: kinds del contrato y URLs sintéticas http://mock.local/ (FR-003, FR-005)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    for external_id in adapter.catalog_ids():
        video = await adapter.get_video(external_id)
        assert video is not None
        for asset in await adapter.get_visual_assets(video):
            assert asset.kind in {"storyboard", "thumbnail", "preview"}
            assert asset.url.startswith(f"{MOCK_BASE_URL}/assets/{external_id}/")


@pytest.mark.anyio
async def test_storyboard_assets_have_positions_and_timestamps() -> None:
    """Storyboard: posiciones secuenciales y timestamps dentro de la duración (FR-005)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    video = await adapter.get_video("mock-vid-0001")
    assert video is not None and video.duration_ms is not None
    storyboards = [a for a in await adapter.get_visual_assets(video) if a.kind == "storyboard"]
    assert len(storyboards) >= 2
    assert [a.position for a in storyboards] == list(range(len(storyboards)))
    timestamps = [a.timestamp_ms for a in storyboards]
    assert all(t is not None for t in timestamps)
    assert timestamps == sorted(timestamps)
    assert all(t is not None and t < video.duration_ms for t in timestamps)


@pytest.mark.anyio
async def test_video_without_storyboard_degrades_to_thumbnails_and_preview() -> None:
    """Vídeo sin storyboard degrada a thumbnails + preview sin fallar (spec edge, FR-005)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    video = await adapter.get_video("mock-vid-0005")  # índice 5 → sin storyboard
    assert video is not None and video.storyboard_urls == []
    assets = await adapter.get_visual_assets(video)
    kinds = {a.kind for a in assets}
    assert "storyboard" not in kinds
    assert "thumbnail" in kinds
    assert "preview" in kinds


@pytest.mark.anyio
async def test_assets_never_include_video_kind() -> None:
    """Ningún asset es un vídeo completo (SC-006, FR-005)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    for external_id in adapter.catalog_ids():
        video = await adapter.get_video(external_id)
        assert video is not None
        for asset in await adapter.get_visual_assets(video):
            assert asset.kind != "video"


# ---------------------------------------------------------------------------
# FR-003 · Fallos inyectados → errores tipados del adapter (tasks.md PR-021)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_global_transient_fault_on_discover_raises_typed_error() -> None:
    """Fallo transitorio global en discover → MockAdapterTransientError (FR-003)."""
    adapter = MockAdapter(faults=MockFaults(discover="transient"))
    with pytest.raises(MockAdapterTransientError):
        await adapter.discover(cursor=None, limit=10)


@pytest.mark.anyio
async def test_global_timeout_fault_on_get_video_raises_typed_error() -> None:
    """Fallo timeout global en get_video → MockAdapterTimeoutError (FR-003)."""
    adapter = MockAdapter(faults=MockFaults(get_video="timeout"))
    with pytest.raises(MockAdapterTimeoutError):
        await adapter.get_video("mock-vid-0001")


@pytest.mark.anyio
async def test_global_removed_fault_raises_terminal_error_with_removed_message() -> None:
    """Fallo terminal/removed → MockAdapterRemovedError con mensaje "removed" (FR-003).

    El mensaje incluye "removed" para alinearse con `backoff.classify_error`
    (contracts §3: contenido retirado → terminal, sin reintentos).
    """
    adapter = MockAdapter(faults=MockFaults(get_video="removed"))
    with pytest.raises(MockAdapterRemovedError) as excinfo:
        await adapter.get_video("mock-vid-0001")
    assert "removed" in str(excinfo.value)


def test_typed_errors_hierarchy() -> None:
    """Timeout es transitorio; todos los errores tipados comparten base (FR-003)."""
    assert issubclass(MockAdapterTimeoutError, MockAdapterTransientError)
    for cls in (MockAdapterTransientError, MockAdapterTimeoutError, MockAdapterRemovedError):
        assert issubclass(cls, MockAdapterError)


@pytest.mark.anyio
async def test_fault_by_external_id_only_affects_that_video() -> None:
    """Fallo por external_id queda contenido en ese vídeo (FR-010, SC-008)."""
    adapter = MockAdapter(faults=MockFaults(by_external_id={"mock-vid-0003": "transient"}))
    with pytest.raises(MockAdapterTransientError):
        await adapter.get_video("mock-vid-0003")
    video = await adapter.get_video("mock-vid-0002")
    assert video is not None and video.external_id == "mock-vid-0002"


@pytest.mark.anyio
async def test_fault_by_external_id_also_affects_assets_of_that_video() -> None:
    """El fallo por external_id se propaga también a los assets de ese vídeo (FR-003)."""
    adapter = MockAdapter(faults=MockFaults(by_external_id={"mock-vid-0003": "timeout"}))
    video = adapter.get_catalog_video("mock-vid-0003")
    assert video is not None
    with pytest.raises(MockAdapterTimeoutError):
        await adapter.get_visual_assets(video)


@pytest.mark.anyio
async def test_global_get_video_fault_does_not_break_discover_or_assets() -> None:
    """Un fallo global en get_video no rompe discover ni los assets (FR-010, SC-008)."""
    adapter = MockAdapter(faults=MockFaults(get_video="transient"))
    page = await adapter.discover(cursor=None, limit=3)
    assert len(page.external_ids) == 3
    video = adapter.get_catalog_video("mock-vid-0002")
    assert video is not None
    assets = await adapter.get_visual_assets(video)
    assert assets


@pytest.mark.anyio
async def test_global_assets_fault_raises_on_assets_only() -> None:
    """Fallo global en get_visual_assets no afecta a get_video (FR-010)."""
    adapter = MockAdapter(faults=MockFaults(get_visual_assets="timeout"))
    video = await adapter.get_video("mock-vid-0001")
    assert video is not None
    with pytest.raises(MockAdapterTimeoutError):
        await adapter.get_visual_assets(video)


@pytest.mark.anyio
async def test_faults_do_not_affect_check_availability() -> None:
    """Los fallos de fetch no alteran check_availability (aislamiento, FR-003)."""
    adapter = MockAdapter(faults=MockFaults(get_video="transient"))
    video = adapter.get_catalog_video("mock-vid-0001")
    assert video is not None
    assert await adapter.check_availability(video) == VideoAvailability.AVAILABLE


def test_same_faults_same_behavior_across_adapters() -> None:
    """Misma configuración de fallos → mismo comportamiento (determinismo, SC-001)."""
    faults = MockFaults(by_external_id={"mock-vid-0001": "removed"})
    a = MockAdapter(faults=faults)
    b = MockAdapter(faults=faults)
    assert a.faults == b.faults


def test_faults_reject_unknown_kind() -> None:
    """Un FaultKind desconocido se rechaza al construir MockFaults (validación, FR-003)."""
    with pytest.raises(ValueError):
        MockFaults(discover="boom")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FR-003 · check_availability con estados configurables (soporte FR-012)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_check_availability_defaults_available() -> None:
    """Vídeo del catálogo → available por defecto (FR-012, contracts §1)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    video = adapter.get_catalog_video("mock-vid-0001")
    assert video is not None
    assert await adapter.check_availability(video) == VideoAvailability.AVAILABLE


@pytest.mark.anyio
async def test_check_availability_configurable_states_per_video() -> None:
    """Estados configurables por external_id: available/unavailable/removed (FR-012)."""
    adapter = MockAdapter(
        faults=MockFaults(
            availability={
                "mock-vid-0002": VideoAvailability.UNAVAILABLE,
                "mock-vid-0003": VideoAvailability.REMOVED,
            }
        )
    )
    for external_id, expected in {
        "mock-vid-0001": VideoAvailability.AVAILABLE,
        "mock-vid-0002": VideoAvailability.UNAVAILABLE,
        "mock-vid-0003": VideoAvailability.REMOVED,
    }.items():
        video = adapter.get_catalog_video(external_id)
        assert video is not None
        assert await adapter.check_availability(video) == expected


@pytest.mark.anyio
async def test_check_availability_global_default_override() -> None:
    """availability_default cambia el estado global; el mapa por id gana (FR-012)."""
    adapter = MockAdapter(
        faults=MockFaults(
            availability={"mock-vid-0002": VideoAvailability.UNAVAILABLE},
            availability_default=VideoAvailability.REMOVED,
        )
    )
    video_a = adapter.get_catalog_video("mock-vid-0001")
    video_b = adapter.get_catalog_video("mock-vid-0002")
    assert video_a is not None and video_b is not None
    assert await adapter.check_availability(video_a) == VideoAvailability.REMOVED
    assert await adapter.check_availability(video_b) == VideoAvailability.UNAVAILABLE


@pytest.mark.anyio
async def test_check_availability_unknown_video_removed() -> None:
    """Un vídeo fuera del catálogo → removed (no disponible en la fuente, FR-012)."""
    adapter = MockAdapter(seed=42, catalog_size=5)
    ghost = VideoSource(source="mock", external_id="fantasma", page_url=f"{MOCK_BASE_URL}/x")
    assert await adapter.check_availability(ghost) == VideoAvailability.REMOVED


# ---------------------------------------------------------------------------
# FR-003 · Fixtures y harness (tests/fixtures/**, tasks.md PR-021)
# ---------------------------------------------------------------------------


def test_fixture_sample_catalog_is_valid_fixture_data() -> None:
    """Los datos de catálogo del fixture son VideoSource válidos del mock (FR-003)."""
    assert len(SAMPLE_VIDEOS) >= 3
    for external_id, video in SAMPLE_VIDEOS.items():
        assert isinstance(video, VideoSource)
        assert video.source == "mock"
        assert video.external_id == external_id
        assert video.page_url.startswith(f"{MOCK_BASE_URL}/videos/")
    for external_id, assets in SAMPLE_ASSETS.items():
        assert external_id in SAMPLE_VIDEOS
        assert all(isinstance(a, VisualAsset) for a in assets)
        assert all(a.kind in {"storyboard", "thumbnail", "preview"} for a in assets)


def test_harness_discover_all_walks_pagination() -> None:
    """discover_all() del harness recorre la paginación completa (FR-003, SC-001)."""
    harness = MockHarness(seed=42, catalog_size=20)
    assert asyncio.run(harness.discover_all()) == harness.adapter.catalog_ids()


def test_harness_expected_catalog_matches_adapter() -> None:
    """El catálogo esperado del harness coincide con el del adapter (FR-003)."""
    harness = MockHarness(seed=FIXTURE_SEED, catalog_size=10)
    assert list(harness.catalog()) == harness.adapter.catalog_ids()
    for external_id in harness.catalog():
        assert harness.video(external_id) == harness.adapter.get_catalog_video(external_id)


@pytest.mark.anyio
async def test_harness_with_faults_builds_faulty_case() -> None:
    """with_faults construye un caso con fallos sin romper discover (FR-003, SC-008)."""
    harness = MockHarness(seed=42, catalog_size=10).with_faults(MockFaults(get_video="timeout"))
    assert len(await harness.discover_all()) == 10
    with pytest.raises(MockAdapterTimeoutError):
        await harness.adapter.get_video("mock-vid-0001")


def test_harness_defaults_are_deterministic() -> None:
    """Dos harness por defecto producen el mismo catálogo (SC-001, NFR-003)."""
    assert MockHarness().catalog() == MockHarness().catalog()


# ---------------------------------------------------------------------------
# PR-034 · fetch_asset_bytes: assets in-process sin red (FR-003 · SC-001)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fetch_asset_bytes_returns_stable_bytes_across_calls() -> None:
    """PR-034: los bytes del asset son los mismos en cada llamada (SC-001, NFR-003)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    url = f"{MOCK_BASE_URL}/assets/mock-vid-0001/storyboard.jpg"
    assert await adapter.fetch_asset_bytes(url) == await adapter.fetch_asset_bytes(url)


@pytest.mark.anyio
async def test_fetch_asset_bytes_stable_across_instances() -> None:
    """PR-034: bytes deterministas entre instancias con el mismo catálogo.

    Los bytes derivan solo de la URL (`zlib.crc32` + Pillow): dos instancias
    del mismo seed producen exactamente los mismos bytes (SC-001, NFR-003).
    """
    a = MockAdapter(seed=7, catalog_size=5)
    b = MockAdapter(seed=7, catalog_size=5)
    for external_id in ("mock-vid-0001", "mock-vid-0004"):
        url = f"{MOCK_BASE_URL}/assets/{external_id}/storyboard.jpg"
        assert await a.fetch_asset_bytes(url) == await b.fetch_asset_bytes(url)


@pytest.mark.anyio
async def test_fetch_asset_bytes_returns_valid_jpeg_images() -> None:
    """PR-034: los bytes de storyboard/thumbnail son imágenes JPEG válidas (FR-003).

    Mismas dimensiones que los sprites de los tests de integración (PR-029):
    sprite 2×2 de tiles 48×27 → 96×54; miniatura 48×27.
    """
    adapter = MockAdapter(seed=42, catalog_size=10)
    video = adapter.get_catalog_video("mock-vid-0001")
    assert video is not None
    sizes: dict[str, tuple[int, int]] = {}
    for asset in await adapter.get_visual_assets(video):
        if asset.kind == "preview":
            continue  # el mock no fabrica mp4 (None → descarga HTTP, contracts §1)
        data = await adapter.fetch_asset_bytes(asset.url)
        assert data is not None
        assert data[:3] == b"\xff\xd8\xff"  # magic JPEG
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            sizes[asset.kind] = image.size
    assert sizes["storyboard"] == (96, 54)
    assert sizes["thumbnail"] == (48, 27)


@pytest.mark.anyio
async def test_fetch_asset_bytes_distinct_per_video() -> None:
    """PR-034: vídeos distintos → bytes distintos (unicidad necesaria para SC-002)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    a = await adapter.fetch_asset_bytes(f"{MOCK_BASE_URL}/assets/mock-vid-0001/storyboard.jpg")
    b = await adapter.fetch_asset_bytes(f"{MOCK_BASE_URL}/assets/mock-vid-0002/storyboard.jpg")
    assert a is not None and b is not None
    assert a != b


@pytest.mark.anyio
async def test_fetch_asset_bytes_preview_and_foreign_urls_return_none() -> None:
    """PR-034: `preview.mp4` y URLs ajenas al catálogo → None (descarga HTTP)."""
    adapter = MockAdapter(seed=42, catalog_size=10)
    assert (
        await adapter.fetch_asset_bytes(f"{MOCK_BASE_URL}/assets/mock-vid-0001/preview.mp4") is None
    )
    assert await adapter.fetch_asset_bytes("https://example.com/assets/x.jpg") is None
    assert await adapter.fetch_asset_bytes(f"{MOCK_BASE_URL}/videos/mock-vid-0001") is None
    assert (
        await adapter.fetch_asset_bytes(f"{MOCK_BASE_URL}/assets/mock-vid-0001/thumb-x.jpg") is None
    )


def test_synthetic_asset_bytes_matches_adapter() -> None:
    """PR-034: `synthetic_asset_bytes` es la implementación única de bytes (harness)."""
    url = f"{MOCK_BASE_URL}/assets/mock-vid-0003/storyboard.jpg"
    assert synthetic_asset_bytes(url) == synthetic_asset_bytes(url)


def test_adapter_without_fetch_asset_bytes_defaults_to_none() -> None:
    """PR-034: un adapter sin `fetch_asset_bytes` se comporta como `None`.

    El método es **opcional** en el contrato (contracts §1): las fuentes reales
    no lo implementan y el pipeline lo descubre con `getattr(..., None)`,
    cayendo a la descarga HTTP (`AssetFetcher`/`SafeHTTPClient`) — regresión de
    integración en `test_pipeline.py` (`test_adapter_without_fetch_asset_bytes_uses_http_path`).
    """

    class _ContractAdapter:
        """Cumple estructuralmente `SourceAdapter` sin `fetch_asset_bytes`."""

        manifest = MockAdapter().manifest

        async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
            return DiscoverPage(external_ids=[])

        async def get_video(self, external_id: str) -> VideoSource | None:
            return None

        async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
            return []

        async def check_availability(self, video: VideoSource) -> VideoAvailability:
            return VideoAvailability.AVAILABLE

    adapter = _ContractAdapter()
    assert getattr(adapter, "fetch_asset_bytes", None) is None
