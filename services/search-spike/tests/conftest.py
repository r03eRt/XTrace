"""Configuración global de pytest para el servicio del spike.

Fuerza salida sin color para que los tests de la CLI sean deterministas también
en CI: Typer/rich emiten códigos ANSI cuando detectan un entorno de color
(GitHub Actions Ubuntu define TERM=xterm-256color), lo que rompía las aserciones
de --help en CI. Se neutralizan todas las variables de color conocidas.
"""

import pytest


@pytest.fixture(autouse=True)
def _force_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desactiva el color de Typer/rich en todos los tests (estabilidad CI/local)."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "0")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLORTERM", "")
    monkeypatch.setenv("CLICOLOR", "0")
    monkeypatch.setenv("CLICOLOR_FORCE", "0")
    monkeypatch.setenv("PY_COLORS", "0")
