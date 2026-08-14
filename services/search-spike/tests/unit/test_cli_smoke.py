"""Smoke tests del bootstrap (PR-001 · FR-017 · ADR-0003).

Criterios verificables:
- El paquete `xtrace_spike` importa.
- `xtrace-spike --help` sale con código 0 y muestra la ayuda.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

import xtrace_spike
from xtrace_spike.cli import app


def test_package_import_and_version() -> None:
    """El paquete importa y expone una versión (FR-017, ADR-0003)."""
    assert xtrace_spike.__version__


def test_cli_help_exits_zero_and_shows_help() -> None:
    """`--help` sale con 0 y muestra la ayuda (criterio del PR-001)."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "xtrace-spike" in result.output


def test_cli_help_via_installed_entry_point() -> None:
    """El console script `xtrace-spike` instalado responde a `--help` con 0."""
    candidate = Path(sys.executable).with_name("xtrace-spike")
    if not candidate.exists():
        found = shutil.which("xtrace-spike")
        assert found is not None, "console script 'xtrace-spike' no encontrado"
        candidate = Path(found)
    proc = subprocess.run(
        [str(candidate), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
