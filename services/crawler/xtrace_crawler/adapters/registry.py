"""Registro de adapters por nombre + gate de habilitación SEC-002 (PR-028).

- **Resolución por nombre** (FR-001 · SC-007): el registro mapea el nombre canónico
  (`AdapterManifest.source`) a la instancia del adapter; añadir una fuente = registrar
  un adapter, sin tocar el core.
- **Gate SEC-002** (spec 002 · ADR-0009 · contracts §1): un adapter **real** NO se
  habilita si `manifest.robots_reviewed` es `false`, `manifest.terms_reviewed` es
  `false`, falta `manifest.review_date` (SEC-002: "con review date") o la fuente no
  está habilitada en BD (`sources.enabled=false` — aprobación humana explícita). El
  fallo es un error **tipado** (`AdapterNotEnabledError`) que enumera cada condición
  fallida.
- **MockAdapter siempre disponible** (FR-003 · SC-001): los adapters registrados como
  no-real (`real=False`, el mock de PR-021 y futuros fakes) se resuelven sin pasar por
  el gate: no tocan la red, no hay riesgo de compliance, y los tests necesitan el
  flujo completo sin `enabled` en BD.

El registro es **puro** (sin BD): la aprobación humana se inyecta vía
`enabled_in_db` (leída de `sources.enabled` por `repo.py`/CLI, PR-032).
"""

from __future__ import annotations

from dataclasses import dataclass

from xtrace_crawler.adapters.base import SourceAdapter


class AdapterRegistryError(Exception):
    """Error base del registro de adapters."""


class UnknownAdapterError(AdapterRegistryError):
    """El adapter solicitado no está registrado."""


class AdapterNotEnabledError(AdapterRegistryError):
    """Gate SEC-002: un adapter real no puede habilitarse (compliance incompleto).

    Atributos:
        adapter_name: nombre canónico del adapter denegado.
        reasons: lista legible de condiciones fallidas (robots/terms/review_date/enabled).
    """

    def __init__(self, adapter_name: str, reasons: list[str]) -> None:
        self.adapter_name = adapter_name
        self.reasons = list(reasons)
        detail = "; ".join(reasons) if reasons else "sin razones registradas"
        super().__init__(f"adapter '{adapter_name}' no habilitable (SEC-002): {detail}")


@dataclass(frozen=True)
class RegisteredAdapter:
    """Entrada del registro: instancia + política de habilitación.

    `real=True` (default) somete el adapter al gate SEC-002; `real=False` lo exime
    (mock/fakes de test, FR-003): siempre disponible.
    """

    name: str
    adapter: SourceAdapter
    real: bool = True


class AdapterRegistry:
    """Registro de adapters por nombre canónico (resolución + gate SEC-002)."""

    def __init__(self) -> None:
        self._adapters: dict[str, RegisteredAdapter] = {}

    def register(self, adapter: SourceAdapter, *, real: bool = True) -> None:
        """Registra un adapter bajo su nombre canónico (`manifest.source`).

        Args:
            adapter: instancia que satisface el protocolo `SourceAdapter` (FR-001).
            real: True somete al gate SEC-002; False = exento (mock, FR-003).

        Raises:
            ValueError: ya existe un adapter registrado con ese nombre.
        """
        name = adapter.manifest.source
        if name in self._adapters:
            raise ValueError(f"adapter '{name}' ya registrado")
        self._adapters[name] = RegisteredAdapter(name=name, adapter=adapter, real=real)

    def is_registered(self, name: str) -> bool:
        """True si hay un adapter registrado bajo `name`."""
        return name in self._adapters

    def names(self) -> list[str]:
        """Nombres canónicos registrados, en orden alfabético estable."""
        return sorted(self._adapters)

    def get(self, name: str) -> SourceAdapter:
        """Resuelve un adapter por nombre **sin** aplicar el gate (tests/uso interno).

        Raises:
            UnknownAdapterError: `name` no está registrado.
        """
        return self._lookup(name).adapter

    def get_enabled(self, name: str, *, enabled_in_db: bool) -> SourceAdapter:
        """Resuelve un adapter por nombre aplicando el gate SEC-002.

        Un adapter real solo se devuelve si `manifest.robots_reviewed` y
        `manifest.terms_reviewed` son `true`, `manifest.review_date` está presente y
        `enabled_in_db` es `true` (aprobación humana en `sources.enabled`). Los
        adapters no-real (mock, FR-003) se devuelven siempre.

        Raises:
            UnknownAdapterError: `name` no está registrado.
            AdapterNotEnabledError: el adapter real no cumple SEC-002; el mensaje
                enumera cada condición fallida.
        """
        registered = self._lookup(name)
        if not registered.real:
            return registered.adapter  # mock: siempre disponible para tests (FR-003)
        reasons = _gate_reasons(registered.adapter, enabled_in_db)
        if reasons:
            raise AdapterNotEnabledError(name, reasons)
        return registered.adapter

    def _lookup(self, name: str) -> RegisteredAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise UnknownAdapterError(f"adapter desconocido: {name!r}") from None


def _gate_reasons(adapter: SourceAdapter, enabled_in_db: bool) -> list[str]:
    """Condiciones SEC-002 fallidas del adapter, en orden estable y legible.

    Regla de hierro (contracts §1 · spec SEC-002): compliance completo (robots +
    términos + review date documentada) Y aprobación humana explícita en BD.
    """
    manifest = adapter.manifest
    reasons: list[str] = []
    if not manifest.robots_reviewed:
        reasons.append("manifest.robots_reviewed=false (sin revisión de robots)")
    if not manifest.terms_reviewed:
        reasons.append("manifest.terms_reviewed=false (sin revisión de términos/ToS)")
    if manifest.review_date is None:
        reasons.append("manifest.review_date vacío (sin fecha de revisión legal)")
    if not enabled_in_db:
        reasons.append("sources.enabled=false (fuente no habilitada en BD)")
    return reasons
