"""Fail-closed policy for the on-demand temporal refinement pass."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xtrace_api.config import Settings


DEFAULT_POLICY_VERSION = "temporal-refinement-v1"
DEFAULT_MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_CANDIDATES = 5
MAX_ASSETS_PER_CANDIDATE = 30
MAX_SEARCH_TIMEOUT_MS = 10_000
MAX_CANDIDATE_TIMEOUT_MS = 3_000

_OVERRIDE_FIELDS = frozenset(
    {
        "enabled",
        "candidate_limit",
        "max_assets_per_candidate",
        "search_timeout_ms",
        "candidate_timeout_ms",
        "max_asset_bytes",
    }
)


@dataclass(frozen=True)
class RefinementPolicy:
    """Immutable, bounded configuration for one refinement execution.

    The absolute limits are enforced for both direct construction and source
    overrides. Invalid environment JSON or values raise ``ValueError`` rather
    than silently widening the budget.
    """

    enabled: bool = True
    candidate_limit: int = 3
    max_assets_per_candidate: int = 30
    search_timeout_ms: int = 10_000
    candidate_timeout_ms: int = 3_000
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES
    policy_version: str = DEFAULT_POLICY_VERSION
    source_overrides: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled debe ser booleano")
        _require_int("candidate_limit", self.candidate_limit, minimum=1, maximum=MAX_CANDIDATES)
        _require_int(
            "max_assets_per_candidate",
            self.max_assets_per_candidate,
            minimum=1,
            maximum=MAX_ASSETS_PER_CANDIDATE,
        )
        _require_int(
            "search_timeout_ms",
            self.search_timeout_ms,
            minimum=1,
            maximum=MAX_SEARCH_TIMEOUT_MS,
        )
        _require_int(
            "candidate_timeout_ms",
            self.candidate_timeout_ms,
            minimum=1,
            maximum=MAX_CANDIDATE_TIMEOUT_MS,
        )
        if self.candidate_timeout_ms > self.search_timeout_ms:
            raise ValueError("candidate_timeout_ms no puede superar search_timeout_ms")
        _require_int("max_asset_bytes", self.max_asset_bytes, minimum=1)
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version no puede estar vacío")

        normalised: dict[str, Mapping[str, Any]] = {}
        for source, override in self.source_overrides.items():
            if not isinstance(source, str) or not source.strip():
                raise ValueError("cada override necesita una fuente no vacía")
            if not isinstance(override, Mapping):
                raise ValueError(f"override inválido para {source!r}")
            unknown = set(override) - _OVERRIDE_FIELDS
            if unknown:
                raise ValueError(
                    f"campos no permitidos en override de {source!r}: {sorted(unknown)}"
                )
            # Validar el resultado efectivo ahora, no solo al seleccionar la
            # fuente, para que una configuración inválida no quede latente.
            replace(self, **dict(override), source_overrides={})
            normalised[source.strip().lower()] = MappingProxyType(dict(override))
        object.__setattr__(self, "source_overrides", MappingProxyType(normalised))

    @classmethod
    def from_env(cls) -> RefinementPolicy:
        """Build a policy from server-only ``XTRACE_REFINEMENT_*`` settings."""

        from xtrace_api.config import Settings

        return cls.from_settings(Settings())

    @classmethod
    def from_settings(cls, settings: Settings) -> RefinementPolicy:
        """Build from typed API settings and validate source JSON fail-closed."""

        try:
            raw_overrides = json.loads(settings.refinement_source_overrides)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("XTRACE_REFINEMENT_SOURCE_OVERRIDES debe ser JSON válido") from exc
        if not isinstance(raw_overrides, dict):
            raise ValueError("XTRACE_REFINEMENT_SOURCE_OVERRIDES debe ser un objeto JSON")

        return cls(
            enabled=settings.refinement_enabled,
            candidate_limit=settings.refinement_candidate_limit,
            max_assets_per_candidate=settings.refinement_max_assets_per_candidate,
            search_timeout_ms=settings.refinement_search_timeout_ms,
            candidate_timeout_ms=settings.refinement_candidate_timeout_ms,
            max_asset_bytes=settings.refinement_max_asset_bytes,
            policy_version=settings.refinement_policy_version,
            source_overrides=raw_overrides,
        )

    def for_source(self, source: str | None) -> RefinementPolicy:
        """Return a bounded source override, or this policy when none exists."""

        if not source:
            return self
        override = self.source_overrides.get(source.strip().lower())
        if override is None:
            return self
        return replace(self, **dict(override), source_overrides={})


def _require_int(name: str, value: object, *, minimum: int, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} debe ser un entero")
    if value < minimum or (maximum is not None and value > maximum):
        limit = f"{minimum}, {maximum}" if maximum is not None else f">={minimum}"
        raise ValueError(f"{name} fuera de rango [{limit}]")
