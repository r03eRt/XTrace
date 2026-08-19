"""Dependency-inversion ports for temporal refinement.

Only the read/evaluate operation is exposed here. In particular, this protocol
does not accept a VectorStore and has no write method; the base index remains
owned by the existing search pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from .models import RefinementOutcome

if TYPE_CHECKING:
    from PIL import Image
    from xtrace_spike.search.ranking import RankedVideo  # type: ignore[import-untyped]

    from xtrace_api.search_service import VideoMetadata

    from .policy import RefinementPolicy


class TemporalRefinementService(Protocol):
    """In-process service boundary used by ``POST /search``."""

    async def refine(
        self,
        query_image: Image.Image,
        ranked: Sequence[RankedVideo],
        metadata: Mapping[str, VideoMetadata],
        *,
        policy: RefinementPolicy,
    ) -> RefinementOutcome:
        """Evaluate permitted visual assets and return an immutable outcome."""
