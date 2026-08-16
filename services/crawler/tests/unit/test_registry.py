"""Tests del registro de adapters (PR-028 · SEC-002 · FR-001 · FR-003 · ADR-0009 · contracts §1).

Trazabilidad (constitución §3): validan `adapters/registry.py` — el registro resuelve
adapters por nombre y aplica el **gate SEC-002** (spec 002): un adapter **real** NO se
habilita si `manifest.robots_reviewed` es false, `manifest.terms_reviewed` es false,
`manifest.review_date` falta (SEC-002: "con review date") o `sources.enabled` es false
(aprobación humana explícita en BD). El **MockAdapter** (y cualquier adapter registrado
como no-real) está siempre disponible para tests (FR-003, SC-001: flujo completo sin red).

Cubren también la resolución por nombre (SC-007: añadir una fuente = registrar un adapter,
sin tocar el core) y los errores tipados del registro.
"""

from __future__ import annotations

import pytest

from xtrace_crawler.adapters.base import AdapterManifest, RateLimitSpec, SourceAdapter
from xtrace_crawler.adapters.models import (
    DiscoverPage,
    VideoAvailability,
    VideoSource,
    VisualAsset,
)
from xtrace_crawler.adapters.registry import (
    AdapterNotEnabledError,
    AdapterRegistry,
    AdapterRegistryError,
    UnknownAdapterError,
)


def make_manifest(**overrides: object) -> AdapterManifest:
    """Manifest de compliance COMPLETO (gate SEC-002 pasable); los tests lo degradan."""
    defaults: dict[str, object] = {
        "source": "fake",
        "access_method": "html",
        "assets_accessed": ["storyboard", "thumbnail"],
        "robots_reviewed": True,
        "terms_reviewed": True,
        "review_date": "2026-08-15",
        "rate_limit": RateLimitSpec(min_interval_ms=1_000, max_rps=2.0),
    }
    defaults.update(overrides)
    return AdapterManifest(**defaults)


class _FakeAdapter:
    """Adapter fake que satisface estructuralmente el protocolo `SourceAdapter` (FR-001)."""

    def __init__(self, *, source: str = "fake", manifest: AdapterManifest | None = None) -> None:
        self.manifest = manifest if manifest is not None else make_manifest(source=source)

    async def discover(self, *, cursor: str | None, limit: int) -> DiscoverPage:
        return DiscoverPage(external_ids=[], next_cursor=None)

    async def get_video(self, external_id: str) -> VideoSource | None:
        return None

    async def get_visual_assets(self, video: VideoSource) -> list[VisualAsset]:
        return []

    async def check_availability(self, video: VideoSource) -> VideoAvailability:
        return VideoAvailability.AVAILABLE


def _registry_with(*adapters: tuple[SourceAdapter, bool]) -> AdapterRegistry:
    """Registro poblado: cada tupla es `(adapter, real)`."""
    registry = AdapterRegistry()
    for adapter, real in adapters:
        registry.register(adapter, real=real)
    return registry


# ---------------------------------------------------------------------------
# Resolución por nombre (FR-001 · SC-007)
# ---------------------------------------------------------------------------


def test_get_returns_registered_adapter_by_name() -> None:
    """`get` resuelve el adapter por su nombre canónico (manifest.source) (FR-001)."""
    adapter = _FakeAdapter(source="fuente-a")
    registry = _registry_with((adapter, True))
    assert registry.get("fuente-a") is adapter


def test_get_unknown_adapter_raises_typed_error() -> None:
    """Nombre no registrado → `UnknownAdapterError` (subclase de `AdapterRegistryError`)."""
    registry = AdapterRegistry()
    with pytest.raises(UnknownAdapterError):
        registry.get("no-existe")


def test_register_duplicate_name_raises() -> None:
    """Registrar dos adapters con el mismo nombre es un error (colisión de registro)."""
    registry = AdapterRegistry()
    registry.register(_FakeAdapter(source="dup"), real=True)
    with pytest.raises(ValueError):
        registry.register(_FakeAdapter(source="dup"), real=True)


def test_names_lists_registered_adapters() -> None:
    """`names` devuelve los nombres registrados (orden estable) (FR-001)."""
    registry = _registry_with(
        (_FakeAdapter(source="zeta"), True),
        (_FakeAdapter(source="alfa"), False),
    )
    assert registry.names() == ["alfa", "zeta"]


def test_errors_are_typed_hierarchy() -> None:
    """Los errores del registro comparten jerarquía tipada `AdapterRegistryError` (SEC-002)."""
    assert issubclass(UnknownAdapterError, AdapterRegistryError)
    assert issubclass(AdapterNotEnabledError, AdapterRegistryError)


# ---------------------------------------------------------------------------
# Gate SEC-002: un adapter REAL no se habilita sin compliance completo
# ---------------------------------------------------------------------------


def test_gate_denies_real_adapter_without_robots_review() -> None:
    """SEC-002: `robots_reviewed=false` deniega la habilitación con error claro tipado."""
    adapter = _FakeAdapter(manifest=make_manifest(robots_reviewed=False))
    registry = _registry_with((adapter, True))
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("fake", enabled_in_db=True)
    assert "robots_reviewed" in str(excinfo.value)
    assert "fake" in str(excinfo.value)


def test_gate_denies_real_adapter_without_terms_review() -> None:
    """SEC-002: `terms_reviewed=false` deniega la habilitación (ToS sin revisar)."""
    adapter = _FakeAdapter(manifest=make_manifest(terms_reviewed=False))
    registry = _registry_with((adapter, True))
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("fake", enabled_in_db=True)
    assert "terms_reviewed" in str(excinfo.value)


def test_gate_denies_real_adapter_without_review_date() -> None:
    """SEC-002: sin `review_date` (revisión legal documentada) deniega la habilitación."""
    adapter = _FakeAdapter(manifest=make_manifest(review_date=None))
    registry = _registry_with((adapter, True))
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("fake", enabled_in_db=True)
    assert "review_date" in str(excinfo.value)


def test_gate_denies_real_adapter_when_source_disabled_in_db() -> None:
    """SEC-002: `sources.enabled=false` (sin aprobación humana en BD) deniega."""
    adapter = _FakeAdapter()  # manifest compliant
    registry = _registry_with((adapter, True))
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("fake", enabled_in_db=False)
    assert "enabled" in str(excinfo.value)


def test_gate_error_lists_every_failing_condition() -> None:
    """El error del gate enumera TODAS las condiciones fallidas (mensaje claro)."""
    adapter = _FakeAdapter(manifest=make_manifest(robots_reviewed=False, terms_reviewed=False))
    registry = _registry_with((adapter, True))
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("fake", enabled_in_db=False)
    message = str(excinfo.value)
    assert "robots_reviewed" in message
    assert "terms_reviewed" in message
    assert "enabled" in message


def test_gate_error_exposes_name_and_reasons() -> None:
    """`AdapterNotEnabledError` expone `adapter_name` y `reasons` como atributos (SEC-002)."""
    adapter = _FakeAdapter(manifest=make_manifest(robots_reviewed=False))
    registry = _registry_with((adapter, True))
    with pytest.raises(AdapterNotEnabledError) as excinfo:
        registry.get_enabled("fake", enabled_in_db=True)
    error = excinfo.value
    assert error.adapter_name == "fake"
    assert any("robots_reviewed" in reason for reason in error.reasons)


def test_gate_allows_fully_compliant_real_adapter() -> None:
    """Manifest completo + `enabled=true` en BD → el adapter se habilita (SEC-002)."""
    adapter = _FakeAdapter()  # robots/terms/review_date OK
    registry = _registry_with((adapter, True))
    assert registry.get_enabled("fake", enabled_in_db=True) is adapter


# ---------------------------------------------------------------------------
# Gate SEC-002: el MockAdapter (no-real) está siempre disponible (FR-003)
# ---------------------------------------------------------------------------


def test_mock_adapter_always_available_for_tests() -> None:
    """Un adapter registrado como mock se resuelve aunque `enabled=false` en BD (FR-003)."""
    mock = _FakeAdapter(source="mock")
    registry = _registry_with((mock, False))
    assert registry.get_enabled("mock", enabled_in_db=False) is mock


def test_mock_adapter_ignores_manifest_compliance() -> None:
    """El mock no se somete al gate: su manifest puede no estar revisado (FR-003, SC-001)."""
    mock = _FakeAdapter(
        source="mock",
        manifest=make_manifest(
            source="mock", robots_reviewed=False, terms_reviewed=False, review_date=None
        ),
    )
    registry = _registry_with((mock, False))
    assert registry.get_enabled("mock", enabled_in_db=False) is mock


def test_get_does_not_apply_gate() -> None:
    """`get` (sin gate) devuelve el adapter aunque esté deshabilitado (uso en tests/CI)."""
    adapter = _FakeAdapter(manifest=make_manifest(robots_reviewed=False))
    registry = _registry_with((adapter, True))
    assert registry.get("fake") is adapter
