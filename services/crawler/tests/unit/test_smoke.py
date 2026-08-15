"""Smoke tests del bootstrap del servicio crawler (PR-019 · FR-003 · ADR-0011).

Validan el scaffolding y la toolchain: paquete importable, dependencia
editable al spike resoluble (ADR-0011) y CLI raíz con `--help` funcional.
"""

from __future__ import annotations

import xtrace_spike
from typer.testing import CliRunner

import xtrace_crawler
from xtrace_crawler.cli import app

runner = CliRunner()


def test_package_importable() -> None:
    """El paquete `xtrace_crawler` importa y expone versión (FR-003, base)."""
    assert isinstance(xtrace_crawler.__version__, str)
    assert xtrace_crawler.__version__


def test_spike_editable_dependency_resolvable() -> None:
    """`xtrace_spike` se resuelve desde el crawler (ADR-0011)."""
    assert isinstance(xtrace_spike.__version__, str)
    assert xtrace_spike.__version__


def test_cli_help_exits_zero() -> None:
    """`xtrace-crawler --help` funciona (contrato PR-019)."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "xtrace-crawler" in result.stdout
    assert "Usage" in result.stdout
