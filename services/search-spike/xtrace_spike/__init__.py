"""Paquete `xtrace_spike`: servicio del spike de búsqueda visual de XTrace."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("xtrace-spike")
except PackageNotFoundError:  # paquete no instalado (p. ej. import directo del árbol)
    __version__ = "0.1.0"

__all__ = ["__version__"]
