"""Contratos puros para el refinamiento temporal bajo demanda.

Este paquete no crea conexiones ni registra adapters al importarse. Las
implementaciones concretas se cargan desde el servicio de la API.
"""

from .models import (
    AssetKind,
    RefinementCandidate,
    RefinementOutcome,
    RefinementStatus,
    RefinementSummary,
    ResultRefinementStatus,
    TimestampOrigin,
    TimestampProvenance,
)
from .ports import TemporalRefinementService

__all__ = [
    "AssetKind",
    "RefinementCandidate",
    "RefinementOutcome",
    "RefinementStatus",
    "RefinementSummary",
    "ResultRefinementStatus",
    "TemporalRefinementService",
    "TimestampOrigin",
    "TimestampProvenance",
]
