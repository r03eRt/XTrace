"""Configuración global de pytest para el servicio del spike.

Fuerza salida sin color (NO_COLOR/FORCE_COLOR=0) para que los tests de la CLI
sean deterministas también en CI: Typer/rich emiten códigos ANSI cuando detectan
un entorno de color, lo que rompía las aserciones de --help en GitHub Actions.
"""

import pytest


@pytest.fixture(autouse=True)
def _force_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desactiva el color de Typer/rich en todos los tests (estabilidad CI/local)."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "0")
