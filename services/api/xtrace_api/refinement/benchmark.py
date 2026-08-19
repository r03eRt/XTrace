"""Metadata-only benchmark for the paired temporal-refinement policy.

The benchmark deliberately consumes observations produced by another process.  It
does not know how to open query media, fetch assets, access a vector store, or call
an adapter.  This keeps the adoption decision reproducible and makes it impossible
for the benchmark CLI to turn a manifest into a hidden network/media workload.

The public data classes are intentionally small.  A manifest contains independent
temporal truth (the annotated timestamp is not derived from an evaluated asset) and
one observation for each policy.  ``compare_refinement_policies`` then applies the
coverage and pair gates before an outcome can be marked as accepted.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

MIN_BENCHMARK_CASES = 30
MIN_CASES_PER_SEGMENT = 3
REQUIRED_SOURCES: tuple[str, ...] = ("local", "web")
DURATION_BUCKETS: tuple[str, ...] = ("<5m", "5-15m", ">15m")


class BenchmarkError(ValueError):
    """Raised when a benchmark manifest cannot be consumed safely."""


@dataclass(frozen=True, slots=True)
class TemporalBenchmarkCase:
    """One positive query with independently annotated temporal truth.

    ``expected_video_id`` is optional so that a metadata-only manifest can carry
    negatives for future extensions.  Coverage requirements are calculated only
    from positive cases (those with a non-null expected video); this task's
    adoption manifest is expected to contain at least thirty positives.

    Values are not rejected in ``__post_init__``.  Keeping construction permissive
    lets the validator report every malformed truth value in one fail-closed
    report, rather than making callers catch a constructor exception before they
    can inspect the diagnostics.
    """

    case_id: str
    expected_video_id: str | None
    source: str
    duration_ms: int | None
    truth_timestamp_ms: int | None

    @property
    def is_positive(self) -> bool:
        """Whether this case contributes to the positive coverage gates."""

        return self.expected_video_id is not None

    def to_dict(self) -> dict[str, object]:
        """Return only safe metadata fields (never query/media content)."""

        return {
            "case_id": self.case_id,
            "expected_video_id": self.expected_video_id,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "truth_timestamp_ms": self.truth_timestamp_ms,
        }


@dataclass(frozen=True, slots=True)
class TemporalBenchmarkObservation:
    """Metadata emitted by one policy for one benchmark case.

    ``ranked_video_ids`` is kept as a tuple because ranking order is part of the
    Top-1/Top-5 contract.  Cost fields are aggregate counters only; no image,
    video, URL, query bytes, or asset bytes are accepted by this model.
    """

    case_id: str
    ranked_video_ids: tuple[str, ...] = ()
    predicted_timestamp_ms: int | None = None
    assets_evaluated: int = 0
    bytes_downloaded: int = 0
    embedding_count: int = 0
    elapsed_ms: int = 0

    @property
    def predicted_video_id(self) -> str | None:
        """The first ranked result, when one exists."""

        return self.ranked_video_ids[0] if self.ranked_video_ids else None

    @property
    def ranked_video_refs(self) -> tuple[str, ...]:
        """Compatibility alias used by the earlier benchmark contract."""

        return self.ranked_video_ids

    def to_dict(self) -> dict[str, object]:
        """Return safe counters and ranking metadata only."""

        return {
            "case_id": self.case_id,
            "ranked_video_ids": list(self.ranked_video_ids),
            "predicted_timestamp_ms": self.predicted_timestamp_ms,
            "assets_evaluated": self.assets_evaluated,
            "bytes_downloaded": self.bytes_downloaded,
            "embedding_count": self.embedding_count,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class TemporalBenchmarkCoverage:
    """Coverage diagnostics used to decide whether a run is adoptable."""

    valid: bool
    case_count: int
    unique_case_count: int
    positive_case_count: int
    unique_positive_case_count: int
    negative_case_count: int
    sources: tuple[str, ...]
    duration_buckets: tuple[str, ...]
    segment_counts: Mapping[str, int]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "case_count": self.case_count,
            "unique_case_count": self.unique_case_count,
            "positive_case_count": self.positive_case_count,
            "unique_positive_case_count": self.unique_positive_case_count,
            "negative_case_count": self.negative_case_count,
            "sources": list(self.sources),
            "duration_buckets": list(self.duration_buckets),
            "segment_counts": dict(sorted(self.segment_counts.items())),
            "errors": list(self.errors),
        }


# A shorter name is convenient for callers that mirror the sampling benchmark.
TemporalCoverageReport = TemporalBenchmarkCoverage
CoverageReport = TemporalBenchmarkCoverage


@dataclass(frozen=True, slots=True)
class TemporalPolicyMetrics:
    """Quality, temporal accuracy, latency, and aggregate cost for one policy."""

    policy: str
    cases: int
    positives: int
    top1: float
    top5: float
    temporal_cases: int
    temporal_error_ms: Mapping[str, int | None]
    latency_ms: Mapping[str, int]
    cost: Mapping[str, int]
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "cases": self.cases,
            "positives": self.positives,
            "top1": self.top1,
            "top5": self.top5,
            "temporal_cases": self.temporal_cases,
            "temporal_error_ms": dict(self.temporal_error_ms),
            "latency_ms": dict(self.latency_ms),
            "cost": dict(self.cost),
            "failures": list(self.failures),
        }


PolicyMetrics = TemporalPolicyMetrics


@dataclass(frozen=True, slots=True)
class TemporalSegmentMetrics:
    """Policy metrics for one source/duration segment."""

    policy: str
    source: str
    duration_bucket: str
    cases: int
    top1: float
    top5: float
    temporal_cases: int
    temporal_error_ms: Mapping[str, int | None]
    latency_ms: Mapping[str, int]
    cost: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "source": self.source,
            "duration_bucket": self.duration_bucket,
            "cases": self.cases,
            "top1": self.top1,
            "top5": self.top5,
            "temporal_cases": self.temporal_cases,
            "temporal_error_ms": dict(self.temporal_error_ms),
            "latency_ms": dict(self.latency_ms),
            "cost": dict(self.cost),
        }


SegmentMetrics = TemporalSegmentMetrics


@dataclass(frozen=True, slots=True)
class TemporalSegmentComparison:
    """Base and refined metrics for the same segment."""

    base: TemporalSegmentMetrics | None
    refined: TemporalSegmentMetrics | None

    def to_dict(self) -> dict[str, object]:
        return {
            "base": self.base.to_dict() if self.base is not None else None,
            "refined": self.refined.to_dict() if self.refined is not None else None,
        }


SegmentComparison = TemporalSegmentComparison


@dataclass(frozen=True, slots=True)
class TemporalBenchmarkReport:
    """Canonical report and fail-closed adoption decision."""

    cases: int
    coverage: TemporalBenchmarkCoverage
    policies: Mapping[str, TemporalPolicyMetrics]
    segments: Mapping[str, TemporalSegmentComparison]
    top5_loss_percentage_points: float | None
    temporal_improvement_percentage: float | None
    gates: Mapping[str, bool]
    missing_base_case_ids: tuple[str, ...]
    missing_refined_case_ids: tuple[str, ...]
    duplicate_observation_ids: tuple[str, ...]
    extra_observation_ids: tuple[str, ...]
    observation_id_mismatches: tuple[str, ...]
    invalid_observation_ids: tuple[str, ...]
    accepted: bool

    @property
    def missing_observation_ids(self) -> tuple[str, ...]:
        """Compatibility view combining missing observations from both policies."""

        return tuple(sorted(set(self.missing_base_case_ids) | set(self.missing_refined_case_ids)))

    @property
    def default_adopted(self) -> bool:
        """A benchmark never changes the index/refinement defaults by itself."""

        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "cases": self.cases,
            "coverage": self.coverage.to_dict(),
            "policies": {name: self.policies[name].to_dict() for name in sorted(self.policies)},
            "segments": {name: self.segments[name].to_dict() for name in sorted(self.segments)},
            "top5_loss_percentage_points": self.top5_loss_percentage_points,
            "temporal_improvement_percentage": self.temporal_improvement_percentage,
            "gates": {name: self.gates[name] for name in sorted(self.gates)},
            "missing_base_case_ids": list(self.missing_base_case_ids),
            "missing_refined_case_ids": list(self.missing_refined_case_ids),
            "missing_observation_ids": list(self.missing_observation_ids),
            "duplicate_observation_ids": list(self.duplicate_observation_ids),
            "extra_observation_ids": list(self.extra_observation_ids),
            "observation_id_mismatches": list(self.observation_id_mismatches),
            "invalid_observation_ids": list(self.invalid_observation_ids),
            "accepted": self.accepted,
            "default_adopted": self.default_adopted,
        }

    def to_json(self) -> str:
        """Serialize a deterministic, media-free JSON report."""

        return (
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )


@dataclass(frozen=True, slots=True)
class TemporalBenchmarkManifest:
    """Parsed manifest containing metadata and already-produced observations."""

    cases: tuple[TemporalBenchmarkCase, ...]
    base: tuple[TemporalBenchmarkObservation, ...]
    refined: tuple[TemporalBenchmarkObservation, ...]


def duration_bucket(duration_ms: int | None) -> str | None:
    """Return the stable duration segment used by the benchmark."""

    if not _is_int(duration_ms) or duration_ms <= 0:
        return None
    if duration_ms < 5 * 60_000:
        return "<5m"
    if duration_ms <= 15 * 60_000:
        return "5-15m"
    return ">15m"


def validate_temporal_benchmark_coverage(
    cases: Sequence[TemporalBenchmarkCase],
    *,
    min_cases: int = MIN_BENCHMARK_CASES,
    min_per_segment: int = MIN_CASES_PER_SEGMENT,
) -> TemporalBenchmarkCoverage:
    """Validate positive count, source/duration coverage, truth, and identities.

    The function never silently drops malformed cases.  Every invalidity becomes
    an error and ``valid`` remains false, so callers can still show diagnostics
    without accidentally treating a partial run as evidence for adoption.
    """

    _validate_coverage_thresholds(min_cases, min_per_segment)
    cases_tuple = tuple(cases)
    errors: list[str] = []
    identities = [_case_identity(case) for case in cases_tuple]
    identity_counts = Counter(identities)
    duplicate_ids = sorted(
        identity for identity, count in identity_counts.items() if identity and count > 1
    )
    if duplicate_ids:
        errors.append("duplicate_case_ids:" + ",".join(duplicate_ids))

    for index, identity in enumerate(identities):
        if not identity:
            errors.append(f"invalid_case_id:{index}")

    positive_cases = tuple(case for case in cases_tuple if case.is_positive)
    positive_identities = [_case_identity(case) for case in positive_cases]
    if len(positive_cases) < min_cases:
        errors.append(f"minimum_positive_cases:{len(positive_cases)}<{min_cases}")

    source_values = tuple(
        sorted(
            {
                case.source.strip()
                for case in positive_cases
                if isinstance(case.source, str) and case.source.strip()
            }
        )
    )
    segment_counts: dict[str, int] = defaultdict(int)
    for case in positive_cases:
        identity = _case_identity(case)
        source = case.source.strip() if isinstance(case.source, str) else ""
        if not source:
            errors.append(f"invalid_source:{identity}")
        if not isinstance(case.expected_video_id, str) or not case.expected_video_id.strip():
            errors.append(f"invalid_expected_video:{identity}")

        duration = case.duration_ms
        truth = case.truth_timestamp_ms
        if not _is_int(duration) or duration <= 0:
            errors.append(f"invalid_duration:{identity}")
        if not _is_int(truth) or truth < 0:
            errors.append(f"invalid_timestamp:{identity}")
        if (
            not _is_int(duration)
            or duration <= 0
            or not _is_int(truth)
            or truth < 0
            or truth >= duration
        ):
            errors.append(f"invalid_truth:{identity}")

        bucket = duration_bucket(duration)
        if bucket is not None and source:
            segment_counts[f"{source}/{bucket}"] += 1

    missing_sources = [source for source in REQUIRED_SOURCES if source not in source_values]
    if missing_sources:
        errors.append("missing_sources:" + ",".join(missing_sources))

    present_buckets = tuple(
        bucket
        for bucket in DURATION_BUCKETS
        if any(key.endswith(f"/{bucket}") for key in segment_counts)
    )
    missing_buckets = [bucket for bucket in DURATION_BUCKETS if bucket not in present_buckets]
    if missing_buckets:
        errors.append("missing_duration_buckets:" + ",".join(missing_buckets))
    for segment, count in sorted(segment_counts.items()):
        if count < min_per_segment:
            errors.append(f"segment_too_small:{segment}:{count}<{min_per_segment}")

    unique_positive_ids = len(set(positive_identities))
    return TemporalBenchmarkCoverage(
        valid=not errors and unique_positive_ids >= min_cases,
        case_count=len(cases_tuple),
        unique_case_count=len(set(identities)),
        positive_case_count=len(positive_cases),
        unique_positive_case_count=unique_positive_ids,
        negative_case_count=len(cases_tuple) - len(positive_cases),
        sources=source_values,
        duration_buckets=present_buckets,
        segment_counts=dict(sorted(segment_counts.items())),
        errors=tuple(sorted(set(errors))),
    )


def compare_refinement_policies(
    cases: Sequence[TemporalBenchmarkCase],
    *,
    base: Iterable[TemporalBenchmarkObservation] | Mapping[str, TemporalBenchmarkObservation],
    refined: Iterable[TemporalBenchmarkObservation] | Mapping[str, TemporalBenchmarkObservation],
    min_cases: int = MIN_BENCHMARK_CASES,
    min_per_segment: int = MIN_CASES_PER_SEGMENT,
) -> TemporalBenchmarkReport:
    """Compare base and refined observations for exactly the same cases.

    Missing, extra, duplicate, mismatched, or malformed observation identities are
    kept as diagnostics and make all adoption-sensitive gates fail.  Metrics are
    still computed for available observations so an operator can understand why a
    run was rejected.
    """

    _validate_coverage_thresholds(min_cases, min_per_segment)
    cases_tuple = tuple(cases)
    coverage = validate_temporal_benchmark_coverage(
        cases_tuple, min_cases=min_cases, min_per_segment=min_per_segment
    )
    expected_ids = tuple(_case_identity(case) for case in cases_tuple)
    expected_id_set = set(expected_ids)

    base_map, base_duplicates, base_mismatches, base_invalid = _observation_map(base)
    refined_map, refined_duplicates, refined_mismatches, refined_invalid = _observation_map(refined)

    duplicate_observation_ids = tuple(sorted(set(base_duplicates + refined_duplicates)))
    observation_id_mismatches = tuple(sorted(set(base_mismatches + refined_mismatches)))
    invalid_observation_ids = tuple(sorted(set(base_invalid + refined_invalid)))
    all_observation_ids = set(base_map) | set(refined_map)
    extra_observation_ids = tuple(sorted(all_observation_ids - expected_id_set))
    missing_base_case_ids = tuple(sorted(expected_id_set - set(base_map)))
    missing_refined_case_ids = tuple(sorted(expected_id_set - set(refined_map)))
    pair_integrity = not (
        missing_base_case_ids
        or missing_refined_case_ids
        or duplicate_observation_ids
        or extra_observation_ids
        or observation_id_mismatches
        or invalid_observation_ids
    )

    base_metrics, base_segments, base_errors = _evaluate_policy("base", cases_tuple, base_map)
    refined_metrics, refined_segments, refined_errors = _evaluate_policy(
        "refined", cases_tuple, refined_map
    )
    segments = _merge_segments(base_segments, refined_segments)

    top5_loss: float | None = None
    if pair_integrity and coverage.valid:
        top5_loss = _round((base_metrics.top5 - refined_metrics.top5) * 100.0, 4)

    improvement_percentage = _temporal_improvement_percentage(
        cases_tuple, base_map, refined_map, pair_integrity and coverage.valid
    )
    observations_valid = bool(
        not base_errors
        and not refined_errors
        and not base_metrics.failures
        and not refined_metrics.failures
    )

    sc001 = bool(
        pair_integrity
        and coverage.valid
        and observations_valid
        and improvement_percentage is not None
        and improvement_percentage >= 80.0
    )
    sc002 = bool(
        pair_integrity
        and coverage.valid
        and observations_valid
        and top5_loss is not None
        and top5_loss <= 5.0
    )
    sc003 = coverage.valid
    sc007 = bool(pair_integrity and coverage.valid and observations_valid)
    gates = {
        "SC-001": sc001,
        "SC-002": sc002,
        "SC-003": sc003,
        "SC-007": sc007,
    }
    return TemporalBenchmarkReport(
        cases=len(cases_tuple),
        coverage=coverage,
        policies={"base": base_metrics, "refined": refined_metrics},
        segments=segments,
        top5_loss_percentage_points=top5_loss,
        temporal_improvement_percentage=improvement_percentage,
        gates=gates,
        missing_base_case_ids=missing_base_case_ids,
        missing_refined_case_ids=missing_refined_case_ids,
        duplicate_observation_ids=duplicate_observation_ids,
        extra_observation_ids=extra_observation_ids,
        observation_id_mismatches=observation_id_mismatches,
        invalid_observation_ids=invalid_observation_ids,
        accepted=all(gates.values()),
    )


# A concise alias mirrors the API used by the adaptive-sampling benchmark.
compare_policies = compare_refinement_policies


def load_temporal_benchmark_manifest(path: str | Path) -> TemporalBenchmarkManifest:
    """Load a metadata-only manifest without opening any media paths.

    Accepted top-level shapes are intentionally explicit but forgiving about
    naming: ``base``/``refined`` may be top-level lists, live under an
    ``observations`` object, or be embedded under each case.  Unknown fields are
    ignored, which prevents query/media fields from leaking into the report.
    """

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"no se puede leer el manifest '{source}': {exc}") from exc
    if not isinstance(raw, Mapping):
        raise BenchmarkError("el manifest debe ser un objeto JSON")

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list):
        raise BenchmarkError("el manifest debe contener una lista 'cases'")
    case_items = tuple(_as_mapping(item, "case", index) for index, item in enumerate(raw_cases))
    cases = tuple(_parse_case(item, index) for index, item in enumerate(case_items))

    base_payload, refined_payload = _manifest_observation_payloads(raw, case_items)
    base = _parse_observations(base_payload, "base")
    refined = _parse_observations(refined_payload, "refined")
    return TemporalBenchmarkManifest(cases=cases, base=base, refined=refined)


def _manifest_observation_payloads(
    raw: Mapping[str, object], case_items: Sequence[Mapping[str, object]]
) -> tuple[object, object]:
    observations = raw.get("observations")
    if isinstance(observations, Mapping):
        base = _first_present(observations, "base", "base_observations")
        refined = _first_present(observations, "refined", "refined_observations")
    else:
        base = _first_present(raw, "base", "base_observations", "baseline")
        refined = _first_present(raw, "refined", "refined_observations")

    if base is _MISSING:
        embedded_base = _embedded_observations(case_items, "base")
        base = embedded_base if embedded_base else _MISSING
    if refined is _MISSING:
        embedded_refined = _embedded_observations(case_items, "refined")
        refined = embedded_refined if embedded_refined else _MISSING
    return base, refined


def _embedded_observations(
    case_items: Sequence[Mapping[str, object]], policy: str
) -> tuple[dict[str, object], ...]:
    values: list[dict[str, object]] = []
    for item in case_items:
        payload = item.get(policy)
        if payload is None:
            payload = item.get(f"{policy}_observation")
        if isinstance(payload, Mapping):
            value = {str(key): raw_value for key, raw_value in payload.items()}
            if "case_id" not in value:
                value["case_id"] = item.get("case_id", "")
            values.append(value)
    return tuple(values)


def _parse_case(item: Mapping[str, object], index: int) -> TemporalBenchmarkCase:
    truth = _first_present(item, "truth_timestamp_ms", "truth_ms", "timestamp_ms")
    if truth is _MISSING and isinstance(item.get("truth"), Mapping):
        truth = _first_present(cast(Mapping[str, object], item["truth"]), "timestamp_ms", "time_ms")
    expected = _first_present(item, "expected_video_id", "expected_video_ref", "video_id")
    return TemporalBenchmarkCase(
        case_id=_text(item.get("case_id")),
        expected_video_id=_optional_text(expected),
        source=_text(item.get("source")),
        duration_ms=_optional_int(item.get("duration_ms")),
        truth_timestamp_ms=_optional_int(None if truth is _MISSING else truth),
    )


def _parse_observations(payload: object, policy: str) -> tuple[TemporalBenchmarkObservation, ...]:
    if payload is _MISSING or payload is None:
        return ()
    entries: list[tuple[str | None, Mapping[str, object]]] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(value, Mapping):
                entries.append((str(key), cast(Mapping[str, object], value)))
            else:
                raise BenchmarkError(f"la observación {key!r} de {policy} no es un objeto")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            entries.append((None, _as_mapping(value, policy, index)))
    else:
        raise BenchmarkError(f"las observaciones de {policy} deben ser lista u objeto")

    observations: list[TemporalBenchmarkObservation] = []
    for fallback_id, item in entries:
        value = dict(item)
        if not _text(value.get("case_id")) and fallback_id is not None:
            value["case_id"] = fallback_id
        observations.append(_parse_observation(value, policy, len(observations)))
    return tuple(observations)


def _parse_observation(
    item: Mapping[str, object], policy: str, index: int
) -> TemporalBenchmarkObservation:
    ranked = _first_present(item, "ranked_video_ids", "ranked_video_refs", "ranked_videos")
    if ranked is _MISSING:
        ranked = _ranked_from_results(item.get("results"))
    if ranked is _MISSING or ranked is None:
        ranked_ids: tuple[str, ...] = ()
    elif isinstance(ranked, (list, tuple)):
        if not all(isinstance(value, str) and value.strip() for value in ranked):
            raise BenchmarkError(f"ranking inválido en observación {index} de {policy}")
        ranked_ids = tuple(value.strip() for value in ranked)
    else:
        raise BenchmarkError(f"ranking inválido en observación {index} de {policy}")

    timestamp = _first_present(item, "predicted_timestamp_ms", "match_timestamp_ms", "timestamp_ms")
    return TemporalBenchmarkObservation(
        case_id=_text(item.get("case_id")),
        ranked_video_ids=ranked_ids,
        predicted_timestamp_ms=_optional_int(None if timestamp is _MISSING else timestamp),
        assets_evaluated=_int_default(
            _first_present(item, "assets_evaluated", "asset_count"),
            0,
            "assets_evaluated",
        ),
        bytes_downloaded=_int_default(
            _first_present(item, "bytes_downloaded", "asset_bytes"),
            0,
            "bytes_downloaded",
        ),
        embedding_count=_int_default(
            _first_present(item, "embedding_count", "embeddings"),
            0,
            "embedding_count",
        ),
        elapsed_ms=_int_default(
            _first_present(item, "elapsed_ms", "latency_ms"),
            0,
            "elapsed_ms",
        ),
    )


def _ranked_from_results(value: object) -> object:
    if not isinstance(value, list):
        return _MISSING
    ids: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            return _MISSING
        candidate = _first_present(item, "video_id", "video_ref", "id")
        if not isinstance(candidate, str) or not candidate.strip():
            return _MISSING
        ids.append(candidate.strip())
    return ids


_MISSING = object()


def _first_present(mapping: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return _MISSING


def _as_mapping(value: object, kind: str, index: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"{kind} {index} no es un objeto")
    return cast(Mapping[str, object], value)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text(value: object) -> str | None:
    if value is None or value is _MISSING:
        return None
    return value.strip() if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    if value is None or value is _MISSING or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _int_default(value: object, default: int, field: str) -> int:
    if value is _MISSING:
        return default
    parsed = _optional_int(value)
    if parsed is None:
        raise BenchmarkError(f"contador inválido en observación: {field}")
    return parsed


def _case_identity(case: TemporalBenchmarkCase) -> str:
    return case.case_id.strip() if isinstance(case.case_id, str) else ""


def _observation_map(
    observations: Iterable[TemporalBenchmarkObservation]
    | Mapping[str, TemporalBenchmarkObservation],
) -> tuple[
    dict[str, TemporalBenchmarkObservation], tuple[str, ...], tuple[str, ...], tuple[str, ...]
]:
    items: Iterable[tuple[object, object]]
    if isinstance(observations, Mapping):
        mapping_input = True
        mapping = cast(Mapping[object, object], observations)
        items = mapping.items()
    else:
        mapping_input = False
        items = ((None, item) for item in observations)
    result: dict[str, TemporalBenchmarkObservation] = {}
    duplicates: list[str] = []
    mismatches: list[str] = []
    invalid: list[str] = []
    for index, (declared_key, raw_observation) in enumerate(items):
        if not isinstance(raw_observation, TemporalBenchmarkObservation):
            invalid.append(str(declared_key) if declared_key is not None else f"<invalid-{index}>")
            continue
        identity = (
            raw_observation.case_id.strip() if isinstance(raw_observation.case_id, str) else ""
        )
        if not identity:
            invalid.append(str(declared_key) if declared_key is not None else f"<invalid-{index}>")
            continue
        if mapping_input and str(declared_key) != identity:
            mismatches.append(f"{declared_key}!={identity}")
        if identity in result:
            duplicates.append(identity)
        result[identity] = raw_observation
    return (
        result,
        tuple(sorted(set(duplicates))),
        tuple(sorted(set(mismatches))),
        tuple(sorted(set(invalid))),
    )


def _evaluate_policy(
    policy: str,
    cases: Sequence[TemporalBenchmarkCase],
    observations: Mapping[str, TemporalBenchmarkObservation],
) -> tuple[TemporalPolicyMetrics, dict[str, TemporalSegmentMetrics], tuple[str, ...]]:
    positives = [case for case in cases if case.is_positive]
    failures: list[str] = []
    top1_hits = 0
    top5_hits = 0
    temporal_errors: list[int] = []
    latencies: list[int] = []
    costs = {"assets_evaluated": 0, "bytes_downloaded": 0, "embedding_count": 0}

    for case in cases:
        identity = _case_identity(case)
        observation = observations.get(identity)
        if observation is None:
            failures.append(f"missing:{identity}")
            continue
        observation_failures = _observation_failures(observation)
        if observation_failures:
            failures.extend(f"{failure}:{identity}" for failure in observation_failures)
        else:
            costs["assets_evaluated"] += observation.assets_evaluated
            costs["bytes_downloaded"] += observation.bytes_downloaded
            costs["embedding_count"] += observation.embedding_count
            latencies.append(observation.elapsed_ms)

        if not case.is_positive:
            continue
        ranked = observation.ranked_video_ids
        expected = case.expected_video_id
        if _valid_ranking(ranked, expected):
            if ranked[0] == expected:
                top1_hits += 1
            if expected in ranked[:5]:
                top5_hits += 1
        else:
            failures.append(f"ranking:{identity}")

        if _valid_truth(case) and _valid_predicted_timestamp(observation, case):
            assert case.truth_timestamp_ms is not None
            assert observation.predicted_timestamp_ms is not None
            temporal_errors.append(
                abs(observation.predicted_timestamp_ms - case.truth_timestamp_ms)
            )
        else:
            failures.append(f"temporal:{identity}")

    metric = TemporalPolicyMetrics(
        policy=policy,
        cases=len(cases),
        positives=len(positives),
        top1=_ratio(top1_hits, len(positives)),
        top5=_ratio(top5_hits, len(positives)),
        temporal_cases=len(temporal_errors),
        temporal_error_ms={
            "median": _percentile_int(temporal_errors, 50),
            "p95": _percentile_int(temporal_errors, 95),
        },
        latency_ms={
            "p50": _percentile_int(latencies, 50) or 0,
            "p95": _percentile_int(latencies, 95) or 0,
        },
        cost=costs,
        failures=tuple(sorted(set(failures))),
    )
    segments = _evaluate_segments(policy, cases, observations)
    return metric, segments, tuple(sorted(set(failures)))


def _evaluate_segments(
    policy: str,
    cases: Sequence[TemporalBenchmarkCase],
    observations: Mapping[str, TemporalBenchmarkObservation],
) -> dict[str, TemporalSegmentMetrics]:
    grouped: dict[str, list[tuple[TemporalBenchmarkCase, TemporalBenchmarkObservation]]] = (
        defaultdict(list)
    )
    for case in cases:
        if not case.is_positive:
            continue
        bucket = duration_bucket(case.duration_ms)
        observation = observations.get(_case_identity(case))
        if bucket is not None and observation is not None:
            grouped[f"{case.source}/{bucket}"].append((case, observation))

    segments: dict[str, TemporalSegmentMetrics] = {}
    for name, values in grouped.items():
        positives = [item for item in values if item[0].is_positive]
        top1 = 0
        top5 = 0
        errors: list[int] = []
        latencies: list[int] = []
        costs = {"assets_evaluated": 0, "bytes_downloaded": 0, "embedding_count": 0}
        for case, observation in values:
            expected = case.expected_video_id
            if _valid_ranking(observation.ranked_video_ids, expected):
                top1 += int(observation.ranked_video_ids[0] == expected)
                top5 += int(expected in observation.ranked_video_ids[:5])
            if _valid_truth(case) and _valid_predicted_timestamp(observation, case):
                assert case.truth_timestamp_ms is not None
                assert observation.predicted_timestamp_ms is not None
                errors.append(abs(observation.predicted_timestamp_ms - case.truth_timestamp_ms))
            if not _observation_failures(observation):
                latencies.append(observation.elapsed_ms)
                costs["assets_evaluated"] += observation.assets_evaluated
                costs["bytes_downloaded"] += observation.bytes_downloaded
                costs["embedding_count"] += observation.embedding_count
        segments[name] = TemporalSegmentMetrics(
            policy=policy,
            source=values[0][0].source,
            duration_bucket=duration_bucket(values[0][0].duration_ms) or "unknown",
            cases=len(values),
            top1=_ratio(top1, len(positives)),
            top5=_ratio(top5, len(positives)),
            temporal_cases=len(errors),
            temporal_error_ms={
                "median": _percentile_int(errors, 50),
                "p95": _percentile_int(errors, 95),
            },
            latency_ms={
                "p50": _percentile_int(latencies, 50) or 0,
                "p95": _percentile_int(latencies, 95) or 0,
            },
            cost=costs,
        )
    return segments


def _merge_segments(
    base: Mapping[str, TemporalSegmentMetrics],
    refined: Mapping[str, TemporalSegmentMetrics],
) -> dict[str, TemporalSegmentComparison]:
    return {
        name: TemporalSegmentComparison(base=base.get(name), refined=refined.get(name))
        for name in sorted(set(base) | set(refined))
    }


def _temporal_improvement_percentage(
    cases: Sequence[TemporalBenchmarkCase],
    base: Mapping[str, TemporalBenchmarkObservation],
    refined: Mapping[str, TemporalBenchmarkObservation],
    eligible: bool,
) -> float | None:
    if not eligible:
        return None
    comparable = 0
    improved_or_equal = 0
    for case in cases:
        if not case.is_positive:
            continue
        identity = _case_identity(case)
        base_observation = base.get(identity)
        refined_observation = refined.get(identity)
        if base_observation is None or refined_observation is None:
            continue
        # SC-001 is explicitly about queries for which additional assets were
        # available; a base observation may have zero assets by definition.
        if refined_observation.assets_evaluated <= 0:
            continue
        if not _valid_truth(case):
            continue
        if not _valid_predicted_timestamp(base_observation, case):
            continue
        if not _valid_predicted_timestamp(refined_observation, case):
            continue
        assert case.truth_timestamp_ms is not None
        assert base_observation.predicted_timestamp_ms is not None
        assert refined_observation.predicted_timestamp_ms is not None
        comparable += 1
        base_error = abs(base_observation.predicted_timestamp_ms - case.truth_timestamp_ms)
        refined_error = abs(refined_observation.predicted_timestamp_ms - case.truth_timestamp_ms)
        # SC-001 accepts a refinement that reduces or maintains the temporal
        # error. Equality is a valid deterministic fallback outcome and must
        # count towards the adoption gate rather than being treated as loss.
        improved_or_equal += int(refined_error <= base_error)
    if comparable == 0:
        return None
    return 100.0 * improved_or_equal / comparable


def _valid_truth(case: TemporalBenchmarkCase) -> bool:
    return bool(
        _is_int(case.duration_ms)
        and case.duration_ms > 0
        and _is_int(case.truth_timestamp_ms)
        and case.truth_timestamp_ms >= 0
        and case.truth_timestamp_ms < case.duration_ms
    )


def _valid_predicted_timestamp(
    observation: TemporalBenchmarkObservation, case: TemporalBenchmarkCase
) -> bool:
    timestamp = observation.predicted_timestamp_ms
    return bool(
        _is_int(timestamp)
        and timestamp >= 0
        and _is_int(case.duration_ms)
        and case.duration_ms > 0
        and timestamp < case.duration_ms
    )


def _valid_ranking(ranked: object, expected: str | None) -> bool:
    if (
        not isinstance(expected, str)
        or not expected.strip()
        or not isinstance(ranked, (tuple, list))
        or not ranked
    ):
        return False
    if not all(isinstance(value, str) and value.strip() for value in ranked):
        return False
    return len(set(ranked)) == len(ranked)


def _observation_failures(observation: TemporalBenchmarkObservation) -> tuple[str, ...]:
    failures: list[str] = []
    if not _is_int(observation.assets_evaluated) or observation.assets_evaluated < 0:
        failures.append("assets_evaluated")
    if not _is_int(observation.bytes_downloaded) or observation.bytes_downloaded < 0:
        failures.append("bytes_downloaded")
    if not _is_int(observation.embedding_count) or observation.embedding_count < 0:
        failures.append("embedding_count")
    if not _is_int(observation.elapsed_ms) or observation.elapsed_ms < 0:
        failures.append("elapsed_ms")
    return tuple(failures)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _ratio(numerator: int, denominator: int) -> float:
    # Keep the full deterministic ratio.  Callers commonly compare 29/30 with
    # a tight tolerance, while JSON remains reproducible across invocations.
    return numerator / denominator if denominator else 0.0


def _percentile_int(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return int(ordered[min(rank, len(ordered)) - 1])


def _round(value: float, digits: int) -> float:
    return float(round(value, digits))


def _validate_coverage_thresholds(min_cases: int, min_per_segment: int) -> None:
    if min_cases < MIN_BENCHMARK_CASES:
        raise ValueError(
            f"min_cases no puede ser inferior a {MIN_BENCHMARK_CASES} (recibido {min_cases})"
        )
    if min_per_segment < 1:
        raise ValueError(f"min_per_segment debe ser >= 1 (recibido {min_per_segment})")


__all__ = [
    "BenchmarkError",
    "CoverageReport",
    "DURATION_BUCKETS",
    "MIN_BENCHMARK_CASES",
    "MIN_CASES_PER_SEGMENT",
    "PolicyMetrics",
    "REQUIRED_SOURCES",
    "SegmentComparison",
    "SegmentMetrics",
    "TemporalBenchmarkCase",
    "TemporalBenchmarkCoverage",
    "TemporalBenchmarkManifest",
    "TemporalBenchmarkObservation",
    "TemporalBenchmarkReport",
    "TemporalCoverageReport",
    "TemporalPolicyMetrics",
    "TemporalSegmentComparison",
    "TemporalSegmentMetrics",
    "compare_policies",
    "compare_refinement_policies",
    "duration_bucket",
    "load_temporal_benchmark_manifest",
    "validate_temporal_benchmark_coverage",
]
