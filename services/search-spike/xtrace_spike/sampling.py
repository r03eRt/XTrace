"""Deterministic adaptive frame sampling (TASK-005-001).

The policy operates on metadata and already available assets.  It never creates an
asset or interpolates a timestamp: a position that is not backed by the source is
represented as ``None`` by the selected item when the item supports that field.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AdaptiveSamplingPolicy:
    """Sampling parameters shared by local extraction and permitted web assets."""

    target_interval_ms: int = 120_000
    max_frames: int = 8

    def __post_init__(self) -> None:
        if self.target_interval_ms <= 0:
            raise ValueError("target_interval_ms debe ser > 0")
        if not 1 <= self.max_frames <= 8:
            raise ValueError("max_frames debe estar en [1, 8]")

    def target_count(self, duration_ms: int | None, available_count: int | None = None) -> int:
        """Return the requested count, bounded by policy and available assets.

        A non-positive or absent duration is unknown, so the policy requests its
        configured maximum and leaves temporal precision to the source.  Zero
        available assets correctly returns zero; callers can then report a failed
        or skipped item instead of fabricating a frame.
        """
        if duration_ms is None or duration_ms <= 0:
            requested = self.max_frames
        else:
            requested = max(1, math.ceil(duration_ms / self.target_interval_ms))
            requested = min(requested, self.max_frames)
        if available_count is None:
            return requested
        if available_count < 0:
            raise ValueError("available_count debe ser >= 0")
        return min(requested, available_count)

    def ideal_timestamps(self, duration_ms: int) -> tuple[int, ...]:
        """Return centered, uniformly spaced points for a reliable duration."""
        if duration_ms <= 0:
            raise ValueError("duration_ms debe ser > 0 para calcular puntos ideales")
        count = self.target_count(duration_ms)
        interval = duration_ms / count
        points = tuple(
            min(duration_ms - 1, max(0, round((index + 0.5) * interval))) for index in range(count)
        )
        # The count formula makes duplicates impossible for normal millisecond
        # durations, but retaining this guard keeps the public invariant true for
        # very small custom intervals as well.
        return tuple(dict.fromkeys(points))


@dataclass(frozen=True, slots=True)
class _Candidate(Generic[T]):
    item: T
    timestamp_ms: int | None
    original_index: int


def select_representative_frames(
    frames: Sequence[T],
    *,
    duration_ms: int | None,
    timestamp: Callable[[T], int | None],
    policy: AdaptiveSamplingPolicy,
) -> list[T]:
    """Select at most the policy target from the assets in deterministic order.

    Valid positions are deduplicated by timestamp (the caller is responsible for
    content/pHash deduplication), then assigned once to the nearest centered point.
    Unknown or invalid positions are retained as ``None``-position candidates and
    used only when the source does not provide enough positioned assets.
    """
    reliable_duration = duration_ms is not None and duration_ms > 0
    candidates: list[_Candidate[T]] = []
    seen_positions: set[int] = set()
    unknown: list[_Candidate[T]] = []

    for original_index, item in enumerate(frames):
        raw_timestamp = timestamp(item)
        normalized = _normalize_timestamp(raw_timestamp, duration_ms)
        normalized_item = _with_timestamp(item, normalized, raw_timestamp)
        candidate = _Candidate(normalized_item, normalized, original_index)
        if normalized is None:
            unknown.append(candidate)
        elif normalized not in seen_positions:
            seen_positions.add(normalized)
            candidates.append(candidate)

    available_count = len(candidates) + len(unknown)
    target = policy.target_count(duration_ms if reliable_duration else None, available_count)
    if target == 0:
        return []

    if reliable_duration:
        if len(candidates) >= target:
            assert duration_ms is not None
            selected = _nearest_to_ideal(candidates, policy.ideal_timestamps(duration_ms))
        else:
            selected = list(candidates)
            selected.extend(unknown[: target - len(selected)])
    else:
        # Known positions are ordered by evidence; unknown positions remain stable
        # in source order because they have no reliable temporal ordering.
        selected = sorted(candidates, key=lambda item: (item.timestamp_ms, item.original_index))
        selected = selected[:target]
        if len(selected) < target:
            selected.extend(unknown[: target - len(selected)])

    # The result contract is temporal order for known positions and stable order
    # for unknown positions.  This also makes repeated runs byte-for-byte stable.
    return [
        candidate.item
        for candidate in sorted(
            selected,
            key=lambda item: (
                item.timestamp_ms is None,
                item.timestamp_ms if item.timestamp_ms is not None else 0,
                item.original_index,
            ),
        )
    ]


def _nearest_to_ideal(
    candidates: Sequence[_Candidate[T]], ideal_timestamps: Sequence[int]
) -> list[_Candidate[T]]:
    """Assign each candidate at most once to a centered point."""
    remaining = list(candidates)
    selected: list[_Candidate[T]] = []
    for ideal in ideal_timestamps:
        if not remaining:
            break
        nearest = min(
            remaining,
            key=lambda item: (
                abs(cast(int, item.timestamp_ms) - ideal),
                cast(int, item.timestamp_ms),
                item.original_index,
            ),
        )
        selected.append(nearest)
        remaining.remove(nearest)
    return selected


def _normalize_timestamp(raw: int | float | None, duration_ms: int | None) -> int | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, float):
            if not math.isfinite(raw) or not raw.is_integer():
                return None
        normalized = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if normalized < 0:
        return None
    if duration_ms is not None and duration_ms > 0 and normalized >= duration_ms:
        return None
    return normalized


def _with_timestamp(item: T, normalized: int | None, raw: int | None) -> T:
    """Best-effortly expose degraded timestamps without requiring a frame type.

    The shared contract intentionally accepts any frame type.  Dataclasses and
    mappings can be copied without mutation; immutable third-party objects are
    returned unchanged, while their normalized position still drives selection.
    """
    if normalized == raw:
        return item
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        field_names = {field.name for field in dataclasses.fields(item)}
        if "timestamp_ms" in field_names:
            return cast(T, dataclasses.replace(item, timestamp_ms=normalized))
    if isinstance(item, Mapping):
        copied = dict(item)
        if "timestamp_ms" in copied:
            copied["timestamp_ms"] = normalized
            return cast(T, copied)
    try:
        copied_item = copy.copy(item)
        cast(Any, copied_item).timestamp_ms = normalized
        return copied_item
    except (AttributeError, TypeError):
        return item
