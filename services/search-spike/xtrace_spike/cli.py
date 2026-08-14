"""CLI interna del spike de búsqueda visual (FR-017, ADR-0008).

Los comandos `index`, `search`, `benchmark`, `exclude` y `stats` se añaden en
PRs posteriores (contratos en `specs/001-visual-search-spike/contracts/README.md`).
En el bootstrap la CLI solo expone `--help` y `--version`.
"""

from typing import Annotated

import typer

from xtrace_spike import __version__


def _version_callback(value: bool) -> None:
    """Imprime la versión y sale cuando se pasa `--version`."""
    if value:
        typer.echo(f"xtrace-spike {__version__}")
        raise typer.Exit()


def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Muestra la versión y sale.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Punto de entrada de la CLI; los subcomandos llegan en PRs posteriores."""


app = typer.Typer(
    name="xtrace-spike",
    callback=main,
    help="CLI interna del spike de búsqueda visual de XTrace (validación, no producto).",
    no_args_is_help=True,
)
