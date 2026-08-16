"""Tests del contrato SourceAdapter + entidades normalizadas (PR-020 · FR-001/FR-002 · ADR-0009).

Trazabilidad (constitución §3): los tests validan el contrato de
`specs/002-source-sdk-crawler/contracts/README.md` §1 (SourceAdapter + AdapterManifest +
DiscoverPage + VideoAvailability) y §2 (VideoSource + VisualAsset):

- FR-001: protocolo async `SourceAdapter` (discover/get_video/get_visual_assets/
  check_availability) + `AdapterManifest` con campos de compliance obligatorios (SEC-002).
- FR-002: entidad normalizada `VideoSource` con URLs http(s) y campos opcionales None.

Decisión PR-020: `AdapterManifest` es un modelo pydantic **frozen** (no un TypedDict) para
que los campos de compliance se exijan en runtime ("manifest inmutable en su contracto",
tasks.md PR-020); la firma de campos/métodos es idéntica a contracts §1/§2.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec, SourceAdapter
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)


def make_manifest(**overrides: object) -> AdapterManifest:
    """Manifest válido de base; los tests pasan overrides para casos de compliance."""
    defaults: dict[str, object] = {
        "source": "mock",
        "access_method": "html",
        "assets_accessed": ["storyboard", "thumbnail"],
        "robots_reviewed": True,
        "terms_reviewed": True,
        "rate_limit": RateLimitSpec(min_interval_ms=1_000, max_rps=2.0),
    }
    defaults.update(overrides)
    return AdapterManifest(**defaults)


# ---------------------------------------------------------------------------
# FR-002 · VideoSource (contracts §2)
# ---------------------------------------------------------------------------


def test_video_source_valid_minimal() -> None:
    """VideoSource mínimo: URLs http(s) obligatorias, campos opcionales None (FR-002)."""
    video = VideoSource(
        source="mock",
        external_id="vid-1",
        page_url="https://example.com/videos/1",
    )
    assert video.source == "mock"
    assert video.external_id == "vid-1"
    assert video.page_url == "https://example.com/videos/1"
    assert video.title is None
    assert video.duration_ms is None
    assert video.thumbnail_url is None
    assert video.preview_url is None
    assert video.storyboard_urls == []
    assert video.tags == []
    assert video.published_at is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/video.mp4",  # esquema no http(s)
        "file:///etc/passwd",  # esquema no http(s)
        "javascript:alert(1)",  # esquema no http(s)
        "example.com/videos/1",  # sin esquema
        "",  # vacía
        "https://",  # sin host
    ],
)
def test_video_source_rejects_invalid_page_urls(url: str) -> None:
    """URLs que no son http(s) con host se rechazan en page_url (FR-002)."""
    with pytest.raises(ValidationError):
        VideoSource(source="mock", external_id="vid-1", page_url=url)


@pytest.mark.parametrize("url", ["https://example.com/v", "http://example.com/v"])
def test_video_source_accepts_http_and_https(url: str) -> None:
    """http y https válidos se aceptan en page_url (FR-002)."""
    video = VideoSource(source="mock", external_id="vid-1", page_url=url)
    assert video.page_url == url


def test_video_source_rejects_invalid_thumbnail_url() -> None:
    """thumbnail_url inválida se rechaza aunque page_url sea válida (FR-002)."""
    with pytest.raises(ValidationError):
        VideoSource(
            source="mock",
            external_id="vid-1",
            page_url="https://example.com/videos/1",
            thumbnail_url="not-a-url",
        )


def test_video_source_rejects_invalid_preview_url() -> None:
    """preview_url inválida se rechaza (FR-002)."""
    with pytest.raises(ValidationError):
        VideoSource(
            source="mock",
            external_id="vid-1",
            page_url="https://example.com/videos/1",
            preview_url="ftp://example.com/p.mp4",
        )


def test_video_source_rejects_any_invalid_storyboard_url() -> None:
    """storyboard_urls valida cada URL; una inválida rechaza el modelo (FR-002)."""
    with pytest.raises(ValidationError):
        VideoSource(
            source="mock",
            external_id="vid-1",
            page_url="https://example.com/videos/1",
            storyboard_urls=["https://example.com/ok.jpg", "file:///etc/passwd"],
        )


def test_video_source_rejects_credentials_in_url() -> None:
    """URLs con credenciales embebidas se rechazan (constitución §7, sin secretos)."""
    with pytest.raises(ValidationError):
        VideoSource(
            source="mock",
            external_id="vid-1",
            page_url="https://user:pass@example.com/videos/1",
        )


def test_video_source_full_fields_roundtrip() -> None:
    """Todos los campos opcionales poblados se conservan tal cual (FR-002)."""
    published = datetime(2026, 8, 15, tzinfo=UTC)
    video = VideoSource(
        source="mock",
        external_id="vid-2",
        title="Título de ejemplo",
        page_url="https://example.com/videos/2",
        duration_ms=125_000,
        thumbnail_url="https://example.com/t.jpg",
        preview_url="https://example.com/p.mp4",
        storyboard_urls=["https://example.com/sb1.jpg", "https://example.com/sb2.jpg"],
        tags=["tag-a", "tag-b"],
        published_at=published,
    )
    assert video.title == "Título de ejemplo"
    assert video.duration_ms == 125_000
    assert video.thumbnail_url == "https://example.com/t.jpg"
    assert video.preview_url == "https://example.com/p.mp4"
    assert video.storyboard_urls == [
        "https://example.com/sb1.jpg",
        "https://example.com/sb2.jpg",
    ]
    assert video.tags == ["tag-a", "tag-b"]
    assert video.published_at == published


def test_video_source_rejects_negative_duration() -> None:
    """duration_ms negativo se rechaza (FR-002, pydantic estricto)."""
    with pytest.raises(ValidationError):
        VideoSource(
            source="mock",
            external_id="vid-1",
            page_url="https://example.com/videos/1",
            duration_ms=-1,
        )


def test_video_source_is_immutable() -> None:
    """VideoSource es inmutable (frozen); mutar un campo falla (FR-002, estricto)."""
    video = VideoSource(source="mock", external_id="vid-1", page_url="https://example.com/v")
    with pytest.raises(ValidationError):
        video.title = "mutado"  # type: ignore[misc]


def test_video_source_forbids_extra_fields() -> None:
    """Campos extra no declarados en el contrato se rechazan (FR-002, estricto)."""
    with pytest.raises(ValidationError):
        VideoSource(  # type: ignore[call-arg]
            source="mock",
            external_id="vid-1",
            page_url="https://example.com/v",
            extra_field=1,
        )


# ---------------------------------------------------------------------------
# FR-001 · AdapterManifest (contracts §1) — compliance (SEC-002)
# ---------------------------------------------------------------------------


def test_manifest_requires_compliance_fields() -> None:
    """El manifest exige los campos de compliance: sin ellos, ValidationError (FR-001, SEC-002)."""
    with pytest.raises(ValidationError):
        AdapterManifest(  # type: ignore[call-arg]
            source="mock",
            access_method="html",
            assets_accessed=["storyboard"],
            rate_limit=RateLimitSpec(min_interval_ms=1_000, max_rps=2.0),
        )  # faltan robots_reviewed y terms_reviewed


def test_manifest_requires_rate_limit() -> None:
    """El manifest exige `rate_limit` (defaults D5) (FR-001, contracts §1)."""
    with pytest.raises(ValidationError):
        AdapterManifest(  # type: ignore[call-arg]
            source="mock",
            access_method="html",
            assets_accessed=["storyboard"],
            robots_reviewed=True,
            terms_reviewed=True,
        )


def test_manifest_rejects_non_bool_compliance_fields() -> None:
    """robots_reviewed/terms_reviewed deben ser bool (FR-001, SEC-002)."""
    with pytest.raises(ValidationError):
        make_manifest(robots_reviewed="yes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        make_manifest(terms_reviewed=1)  # type: ignore[arg-type]


def test_manifest_valid_compliant_manifest() -> None:
    """Manifest completo válido: campos de compliance presentes y rate_limit declarado (FR-001)."""
    manifest = make_manifest()
    assert manifest.source == "mock"
    assert manifest.access_method == "html"
    assert manifest.assets_accessed == ["storyboard", "thumbnail"]
    assert manifest.robots_reviewed is True
    assert manifest.terms_reviewed is True
    assert manifest.review_date is None
    assert manifest.rate_limit.min_interval_ms == 1_000
    assert manifest.rate_limit.max_rps == 2.0


def test_manifest_review_date_optional() -> None:
    """review_date es opcional (None por defecto) y se conserva si se declara (FR-001)."""
    assert make_manifest().review_date is None
    assert make_manifest(review_date="2026-08-15").review_date == "2026-08-15"


def test_manifest_rejects_unknown_access_method() -> None:
    """access_method solo admite la jerarquía documentada (FR-004, contracts §1)."""
    with pytest.raises(ValidationError):
        make_manifest(access_method="ftp")  # type: ignore[arg-type]


def test_manifest_rejects_video_in_assets_accessed() -> None:
    """assets_accessed nunca declara `video` (SC-006, FR-005)."""
    with pytest.raises(ValidationError):
        make_manifest(assets_accessed=["storyboard", "video"])  # type: ignore[list-item]


def test_manifest_is_immutable() -> None:
    """El manifest es inmutable (frozen): mutar un campo falla (tasks.md PR-020)."""
    manifest = make_manifest()
    with pytest.raises(ValidationError):
        manifest.source = "otra-fuente"  # type: ignore[misc]


def test_rate_limit_spec_rejects_negative_values() -> None:
    """RateLimitSpec no admite valores negativos (contracts §4)."""
    with pytest.raises(ValidationError):
        RateLimitSpec(min_interval_ms=-1, max_rps=1.0)
    with pytest.raises(ValidationError):
        RateLimitSpec(min_interval_ms=1_000, max_rps=-0.5)


def test_rate_limit_spec_has_defaults() -> None:
    """El RateLimitSpec canónico (adapters/base.py) trae defaults en código (FR-009/D5).

    Alineación PR-030 (contracts §1): definición ÚNICA en `adapters/base.py` con
    defaults, y `max_rps` estrictamente > 0 (el limiter divide por él).
    """
    spec = RateLimitSpec()
    assert spec.min_interval_ms == 1000
    assert spec.max_rps == 1.0
    with pytest.raises(ValidationError):
        RateLimitSpec(max_rps=0.0)


# ---------------------------------------------------------------------------
# FR-001 · DiscoverPage y VideoAvailability (contracts §1)
# ---------------------------------------------------------------------------


def test_discover_page_fields_and_optional_cursor() -> None:
    """DiscoverPage: external_ids obligatoria y next_cursor opcional (FR-001)."""
    page = DiscoverPage(external_ids=["vid-1", "vid-2"], next_cursor="cursor-2")
    assert page.external_ids == ["vid-1", "vid-2"]
    assert page.next_cursor == "cursor-2"
    assert DiscoverPage(external_ids=[]).next_cursor is None


def test_video_availability_enum_values() -> None:
    """VideoAvailability: available | unavailable | removed (FR-001, contracts §1)."""
    assert VideoAvailability.AVAILABLE == "available"
    assert VideoAvailability.UNAVAILABLE == "unavailable"
    assert VideoAvailability.REMOVED == "removed"


def test_video_availability_exact_members() -> None:
    """VideoAvailability expone exactamente los tres estados del contrato (FR-001)."""
    assert {member.value for member in VideoAvailability} == {
        "available",
        "unavailable",
        "removed",
    }


# ---------------------------------------------------------------------------
# FR-002 · VisualAsset (contracts §2)
# ---------------------------------------------------------------------------


def test_visual_asset_minimal() -> None:
    """VisualAsset mínimo: kind + url; position/timestamp_ms opcionales None (FR-002)."""
    asset = VisualAsset(kind="storyboard", url="https://example.com/sb.jpg")
    assert asset.kind == "storyboard"
    assert asset.url == "https://example.com/sb.jpg"
    assert asset.position is None
    assert asset.timestamp_ms is None


def test_visual_asset_full_fields() -> None:
    """VisualAsset con position y timestamp_ms se conservan (FR-002)."""
    asset = VisualAsset(
        kind="thumbnail",
        url="https://example.com/t.jpg",
        position=3,
        timestamp_ms=12_000,
    )
    assert asset.position == 3
    assert asset.timestamp_ms == 12_000


def test_visual_asset_rejects_video_kind() -> None:
    """kind solo admite storyboard|thumbnail|preview (SC-006, contracts §2)."""
    with pytest.raises(ValidationError):
        VisualAsset(kind="video", url="https://example.com/v.mp4")  # type: ignore[arg-type]


def test_visual_asset_rejects_invalid_url() -> None:
    """La URL del asset debe ser http(s) (FR-002)."""
    with pytest.raises(ValidationError):
        VisualAsset(kind="thumbnail", url="ftp://example.com/t.jpg")


# ---------------------------------------------------------------------------
# FR-001 · SourceAdapter (Protocol) (contracts §1)
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Implementación mínima que satisface estructuralmente el protocolo (FR-001)."""

    manifest = make_manifest()

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
        return DiscoverPage(external_ids=[], next_cursor=None)

    async def get_video(self, external_id: str) -> VideoSource | None:
        return None

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        return []

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        return VideoAvailability.AVAILABLE


def test_source_adapter_protocol_structural() -> None:
    """Un adapter con manifest y la firma del contrato satisface el protocolo (FR-001)."""
    assert isinstance(_FakeAdapter(), SourceAdapter)


def test_source_adapter_requires_manifest_attribute() -> None:
    """Un adapter sin `manifest` no satisface el protocolo (FR-001, SEC-002)."""

    class _NoManifest:
        async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
            return DiscoverPage(external_ids=[])

        async def get_video(self, external_id: str) -> VideoSource | None:
            return None

        async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
            return []

        async def check_availability(self, video: VideoSource) -> VideoAvailability:
            return VideoAvailability.AVAILABLE

    assert not isinstance(_NoManifest(), SourceAdapter)
