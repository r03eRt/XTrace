"""Harness de construcción de casos para el MockAdapter (PR-021 · FR-003 · SC-001).

Fachada para los tests del crawler **sin red** (NFR-003): construye adapters
deterministas (seed + tamaño + fallos) y expone helpers de casos:

- `discover_all()`: recorre la paginación por cursor completa (ids en orden,
  sin duplicados) — la base de los casos de backfill/incremental (PR-030).
- `catalog()` / `video()`: valores **esperados** del catálogo generado para
  montar asserts sin tocar internos del adapter.
- `with_faults()`: nueva instancia con la misma semilla y los fallos dados
  (inyección de fallos por método, por external_id o por disponibilidad).

Los tests deben usar el harness contra el **contrato** (`SourceAdapter`), no
contra la implementación del mock (riesgo de acoplamiento del plan §Risks).
"""

from __future__ import annotations

from tests.fixtures.catalog import FIXTURE_CATALOG_SIZE, FIXTURE_SEED
from xtrace_crawler.adapters.mock import MockAdapter, MockFaults
from xtrace_crawler.adapters.models import VideoSource


class MockHarness:
    """Construye y expone un MockAdapter determinista para casos de test."""

    def __init__(
        self,
        *,
        seed: int = FIXTURE_SEED,
        catalog_size: int = FIXTURE_CATALOG_SIZE,
        faults: MockFaults | None = None,
    ) -> None:
        """Harness con el seed/tamaño canónicos por defecto (determinismo, SC-001)."""
        self._seed = seed
        self._catalog_size = catalog_size
        self._faults = faults
        self._adapter = MockAdapter(seed=seed, catalog_size=catalog_size, faults=faults)

    @property
    def adapter(self) -> MockAdapter:
        """El MockAdapter del caso (cualquier fallo inyectado se dispara aquí)."""
        return self._adapter

    async def discover_all(self, page_size: int = 10) -> list[str]:
        """Recorre la paginación completa de `discover` (ids en orden, sin duplicados)."""
        ids: list[str] = []
        cursor: str | None = None
        while True:
            page = await self._adapter.discover(cursor=cursor, limit=page_size)
            ids.extend(page.external_ids)
            if page.next_cursor is None:
                return ids
            cursor = page.next_cursor

    def catalog(self) -> dict[str, VideoSource]:
        """Snapshot del catálogo generado (external_id → VideoSource) para expectativas."""
        return self._adapter.catalog_snapshot()

    def video(self, external_id: str) -> VideoSource:
        """Valor esperado del vídeo en el catálogo (KeyError si no existe)."""
        video = self._adapter.get_catalog_video(external_id)
        if video is None:
            raise KeyError(f"external_id fuera del catálogo del harness: {external_id!r}")
        return video

    def with_faults(self, faults: MockFaults) -> MockHarness:
        """Nueva instancia con el mismo seed/tamaño y los fallos dados (inyección)."""
        return MockHarness(seed=self._seed, catalog_size=self._catalog_size, faults=faults)
