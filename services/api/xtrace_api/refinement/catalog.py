"""Pure mapping from indexed metadata to refinement candidates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .models import RefinementCandidate


def candidate_from_record(record: Mapping[str, Any]) -> RefinementCandidate:
    """Build a candidate from already validated DB metadata.

    HTML, provider payloads and generated URLs are intentionally not accepted in
    this layer; the adapter owns source discovery and URL validation.
    """

    video_id = _required_str(record, "video_id")
    source = _optional_str(record.get("source"))
    adapter = _optional_str(record.get("adapter"))
    external_id = _optional_str(record.get("external_id"))
    page_url = _optional_str(record.get("page_url"))
    duration_ms = _optional_int(record.get("duration_ms"), "duration_ms")
    base_timestamp_ms = _optional_int(record.get("base_timestamp_ms"), "base_timestamp_ms")
    base_visual_similarity = record.get("base_visual_similarity")
    if isinstance(base_visual_similarity, bool) or not isinstance(
        base_visual_similarity, (int, float)
    ):
        raise ValueError("base_visual_similarity debe ser numérico")
    if not 0.0 <= float(base_visual_similarity) <= 1.0:
        raise ValueError("base_visual_similarity debe estar en [0,1]")
    return RefinementCandidate(
        video_id=video_id,
        source=source,
        adapter=adapter,
        external_id=external_id,
        page_url=page_url,
        duration_ms=duration_ms,
        base_timestamp_ms=base_timestamp_ms,
        base_visual_similarity=float(base_visual_similarity),
    )


def is_refinable_candidate(candidate: RefinementCandidate) -> bool:
    """A local candidate is never sent; nullable web metadata is supported.

    The adapter is allowed to resolve a canonical page from ``external_id``
    when the catalog has no ``page_url``. Likewise, a missing base timestamp
    is valid evidence for a refinement that can supply its first timestamp.
    """

    return bool(
        candidate.source
        and candidate.adapter
        and candidate.external_id
        and (candidate.duration_ms is None or candidate.duration_ms >= 0)
    )


class RefinementCatalog:
    """Namespace for catalog eligibility used by the orchestration service."""

    @staticmethod
    def is_refinable(candidate: RefinementCandidate) -> bool:
        return is_refinable_candidate(candidate)


def _required_str(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} debe ser texto no vacío")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("el campo debe ser texto o null")
    return value


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} debe ser entero o null")
    if value < 0:
        raise ValueError(f"{field} no puede ser negativo")
    return cast(int, value)
