"""Paquete `xtrace_crawler`: servicio crawler de XTrace (spec 002 · FR-003)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("xtrace-crawler")
except PackageNotFoundError:  # paquete no instalado (p. ej. import directo del árbol)
    __version__ = "0.1.0"

__all__ = ["__version__"]
