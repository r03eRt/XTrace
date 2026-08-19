"""Value objects for the temporal-refinement contract.

The objects in this module are deliberately independent of FastAPI, Supabase,
HTTP clients and the vector store. They describe one in-memory refinement
execution and are immutable once created so a fallback cannot mutate the base
search result by accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RefinementStatus(StrEnum):
    """Status of the refinement summary returned for a search."""

    COMPLETED = "completed"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    LIMITED = "limited"
    FAILED = "failed"


class ResultRefinementStatus(StrEnum):
    """Status of one candidate's timestamp decision."""

    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"
    LIMITED = "limited"
    DISABLED = "disabled"


class TimestampOrigin(StrEnum):
    """Where the timestamp presented to the user came from."""

    BASE_INDEX = "base_index"
    REFINED_ASSET = "refined_asset"


class AssetKind(StrEnum):
    """Public visual asset types allowed by the refinement contract."""

    THUMBNAIL = "thumbnail"
    STORYBOARD = "storyboard"


@dataclass(frozen=True)
class RefinementCandidate:
    """Immutable view of a base-search candidate eligible for refinement."""

    video_id: str
    source: str | None
    adapter: str | None
    external_id: str | None
    page_url: str | None
    duration_ms: int | None
    base_timestamp_ms: int | None
    base_visual_similarity: float


@dataclass(frozen=True)
class TimestampProvenance:
    """Traceability for the timestamp exposed by a result."""

    origin: TimestampOrigin
    status: ResultRefinementStatus
    source: str | None = None
    asset_kind: AssetKind | None = None
    asset_url: str | None = None
    asset_position: int | None = None


@dataclass(frozen=True)
class RefinementSummary:
    """Bounded aggregate metrics safe to expose in the REST response."""

    status: RefinementStatus
    candidates_requested: int
    candidates_processed: int
    assets_evaluated: int
    assets_discarded: int
    errors_count: int
    bytes_downloaded: int
    embedding_count: int
    embedding_elapsed_ms: int
    improved_results: int
    elapsed_ms: int


@dataclass(frozen=True)
class RefinementOutcome:
    """Refined view of the base ranking without any index mutation capability."""

    ranked: tuple[Any, ...]
    provenance: Mapping[str, TimestampProvenance]
    summary: RefinementSummary
