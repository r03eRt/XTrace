"""Paquete `xtrace_api`: servicio API REST de búsqueda visual de XTrace (spec 003)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("xtrace-api")
except PackageNotFoundError:  # paquete no instalado (p. ej. import directo del árbol)
    __version__ = "0.1.0"

__all__ = ["__version__"]
