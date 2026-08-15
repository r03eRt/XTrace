"""CLI raíz del servicio crawler (PR-019 · FR-003 · plan §Project Structure).

Comandos operativos (`sources`, `backfill`, `run-worker`, `stats`,
`check-availability`) se añaden en PR-032 según contracts §5. Este bootstrap
solo expone la aplicación Typer `xtrace-crawler` con `--help` funcional.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="xtrace-crawler",
    help=(
        "Servicio crawler de XTrace: ingesta de fuentes web al índice visual "
        "(spec 002 · FR-003). Comandos operativos en construcción (PR-032)."
    ),
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Servicio crawler de XTrace: ingesta de fuentes web al índice visual (FR-003)."""


if __name__ == "__main__":
    app()
