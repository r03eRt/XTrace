"""Comparación reproducible de muestreo adaptativo frente a referencia densa.

Este módulo no cambia el default histórico de 30 frames. Solo consume casos con
verdad conocida y observaciones ya producidas por cada índice para medir la
puerta de adopción de la política adaptativa (FR-012..014, SC-004..008).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from xtrace_spike.benchmark.dataset import BenchmarkCase, BenchmarkError

MIN_BENCHMARK_CASES = 30
MIN_CASES_PER_SEGMENT = 3
DENSE_REFERENCE_FRAMES = 30
ADAPTIVE_MAX_FRAMES = 8
MIN_TOP5 = 0.80
MAX_TOP5_LOSS_PP = 5.0
MAX_NORMALIZED_MEDIAN_ERROR = 0.50
MIN_FRAME_REDUCTION = 0.70

DURATION_BUCKETS: tuple[str, ...] = ("<5m", "5-15m", ">15m")
REQUIRED_SOURCES: tuple[str, ...] = ("local", "web")


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    """Resultado de una política para un caso de benchmark.

    ``ranked_video_refs`` debe conservar el orden del ranking real. Si se
    omite, se deriva una lista de un elemento desde ``predicted_video_ref``;
    esto facilita sidecars mínimos sin alterar la semántica de Top-1/Top-5.
    ``frame_count`` es el conteo efectivo del vídeo del caso en ese índice,
    no el número de consultas procesadas.
    """

    case_id: str
    predicted_video_ref: str | None
    predicted_timestamp_ms: int | None
    ranked_video_refs: tuple[str, ...] = ()
    frame_count: int = 0

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise BenchmarkError("cada observación necesita case_id")
        if self.predicted_timestamp_ms is not None and self.predicted_timestamp_ms < 0:
            raise BenchmarkError("predicted_timestamp_ms debe ser >= 0 o None")
        if self.frame_count < 0:
            raise BenchmarkError("frame_count debe ser >= 0")
        if not self.ranked_video_refs and self.predicted_video_ref is not None:
            object.__setattr__(self, "ranked_video_refs", (self.predicted_video_ref,))

    def to_dict(self) -> dict[str, object]:
        """Serializa una observación sin campos dependientes del reloj."""
        return {
            "case_id": self.case_id,
            "predicted_video_ref": self.predicted_video_ref,
            "predicted_timestamp_ms": self.predicted_timestamp_ms,
            "ranked_video_refs": list(self.ranked_video_refs),
            "frame_count": self.frame_count,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Resultado de las puertas de cobertura del sidecar."""

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


@dataclass(frozen=True, slots=True)
class SegmentMetrics:
    """Métricas de una combinación estable fuente/tramo de duración."""

    policy: str
    source: str
    duration_bucket: str
    cases: int
    top1: float
    top5: float
    temporal_error_ms: Mapping[str, int | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "source": self.source,
            "duration_bucket": self.duration_bucket,
            "cases": self.cases,
            "top1": self.top1,
            "top5": self.top5,
            "temporal_error_ms": dict(self.temporal_error_ms),
        }


@dataclass(frozen=True, slots=True)
class SegmentComparison:
    """Métricas dense y adaptive conservadas para un mismo segmento."""

    dense: SegmentMetrics | None
    adaptive: SegmentMetrics | None

    def to_dict(self) -> dict[str, object]:
        return {
            "dense": self.dense.to_dict() if self.dense is not None else None,
            "adaptive": self.adaptive.to_dict() if self.adaptive is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PolicyMetrics:
    """Resultados agregados de una política para el mismo conjunto de casos."""

    policy: str
    cases: int
    positives: int
    top1: float
    top5: float
    temporal_cases: int
    temporal_error_ms: Mapping[str, int | None]
    normalized_error: Mapping[str, float | None]
    frames: int
    videos: int
    video_frames: Mapping[str, int]
    sufficient_videos: int
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
            "normalized_error": dict(self.normalized_error),
            "frames": self.frames,
            "videos": self.videos,
            "video_frames": dict(sorted(self.video_frames.items())),
            "sufficient_videos": self.sufficient_videos,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class SamplingComparisonReport:
    """Informe y decisión fail-closed del benchmark adaptativo."""

    cases: int
    coverage: CoverageReport
    policies: Mapping[str, PolicyMetrics]
    segments: Mapping[str, SegmentComparison]
    top5_loss_percentage_points: float | None
    frames_reduction: float | None
    gates: Mapping[str, bool]
    missing_observation_ids: tuple[str, ...]
    duplicate_observation_ids: tuple[str, ...]
    extra_observation_ids: tuple[str, ...]
    observation_id_mismatches: tuple[str, ...]
    reduction_video_ids: tuple[str, ...]
    accepted: bool

    @property
    def default_adopted(self) -> bool:
        """Siempre false aquí: la adopción del default requiere decisión posterior."""
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "cases": self.cases,
            "coverage": self.coverage.to_dict(),
            "policies": {
                name: self.policies[name].to_dict() for name in sorted(self.policies)
            },
            "segments": {
                name: self.segments[name].to_dict() for name in sorted(self.segments)
            },
            "top5_loss_percentage_points": self.top5_loss_percentage_points,
            "frames_reduction": self.frames_reduction,
            "gates": {name: self.gates[name] for name in sorted(self.gates)},
            "missing_observation_ids": list(self.missing_observation_ids),
            "duplicate_observation_ids": list(self.duplicate_observation_ids),
            "extra_observation_ids": list(self.extra_observation_ids),
            "observation_id_mismatches": list(self.observation_id_mismatches),
            "reduction_video_ids": list(self.reduction_video_ids),
            "accepted": self.accepted,
            "default_adopted": self.default_adopted,
        }

    def to_json(self) -> str:
        """JSON canónico, ordenado y terminado en salto de línea."""
        return (
            json.dumps(
                self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        )


def duration_bucket(duration_ms: int | None) -> str | None:
    """Clasifica una duración en los tres tramos de la spec."""
    if duration_ms is None or duration_ms <= 0:
        return None
    if duration_ms < 5 * 60_000:
        return "<5m"
    if duration_ms <= 15 * 60_000:
        return "5-15m"
    return ">15m"


def case_identity(case: BenchmarkCase) -> str:
    """Obtiene una identidad estable incluso para manifests antiguos."""
    if case.case_id:
        return case.case_id
    return "|".join(
        (
            case.source,
            case.expected_video_ref or "",
            str(case.duration_ms or ""),
            str(case.timestamp_ms or ""),
            str(case.query_image_path),
        )
    )


def validate_benchmark_coverage(
    cases: Sequence[BenchmarkCase],
    *,
    min_cases: int = MIN_BENCHMARK_CASES,
    min_per_segment: int = MIN_CASES_PER_SEGMENT,
) -> CoverageReport:
    """Valida cobertura local/web y segmentos sin lanzar adopción parcial.

    Se exige una identidad única por consulta para positivos y negativos. Los
    mínimos, la verdad temporal, ambas fuentes y los tres tramos se calculan
    solo sobre positivos; los negativos estándar pueden carecer de duración y
    timestamp, pero continúan formando parte del pareado exacto posterior.
    """
    _validate_coverage_thresholds(min_cases, min_per_segment)
    errors: list[str] = []
    identities = [case_identity(case) for case in cases]
    positive_cases = tuple(case for case in cases if case.expected_video_ref is not None)
    positive_identities = [case_identity(case) for case in positive_cases]
    duplicate_ids = sorted({value for value in identities if identities.count(value) > 1})
    if len(positive_cases) < min_cases:
        errors.append(f"minimum_positive_cases:{len(positive_cases)}<{min_cases}")
    if duplicate_ids:
        errors.append("duplicate_case_ids:" + ",".join(duplicate_ids))
    source_values = tuple(sorted({case.source for case in positive_cases}))
    for case in positive_cases:
        identity = case_identity(case)
        if not case.source.strip():
            errors.append(f"invalid_source:{identity}")
        if case.timestamp_ms is not None and case.timestamp_ms < 0:
            errors.append(f"invalid_timestamp:{identity}")
        if (
            case.duration_ms is None
            or case.duration_ms <= 0
            or case.timestamp_ms is None
            or case.timestamp_ms < 0
            or case.timestamp_ms >= case.duration_ms
        ):
            errors.append(f"invalid_truth:{identity}")
    missing_sources = [source for source in REQUIRED_SOURCES if source not in source_values]
    if missing_sources:
        errors.append("missing_sources:" + ",".join(missing_sources))
    segment_counts: dict[str, int] = defaultdict(int)
    for case in positive_cases:
        bucket = duration_bucket(case.duration_ms)
        if bucket is None:
            errors.append(f"invalid_duration:{case_identity(case)}")
            continue
        if case.timestamp_ms is not None and case.timestamp_ms >= (case.duration_ms or 0):
            errors.append(f"invalid_timestamp:{case_identity(case)}")
        segment_counts[f"{case.source}/{bucket}"] += 1
    present_buckets = tuple(bucket for bucket in DURATION_BUCKETS if any(
        key.endswith(f"/{bucket}") for key in segment_counts
    ))
    missing_buckets = [bucket for bucket in DURATION_BUCKETS if bucket not in present_buckets]
    if missing_buckets:
        errors.append("missing_duration_buckets:" + ",".join(missing_buckets))
    for segment, count in sorted(segment_counts.items()):
        if count < min_per_segment:
            errors.append(f"segment_too_small:{segment}:{count}<{min_per_segment}")
    return CoverageReport(
        valid=not errors and len(set(positive_identities)) >= min_cases,
        case_count=len(cases),
        unique_case_count=len(set(identities)),
        positive_case_count=len(positive_cases),
        unique_positive_case_count=len(set(positive_identities)),
        negative_case_count=len(cases) - len(positive_cases),
        sources=source_values,
        duration_buckets=present_buckets,
        segment_counts=dict(sorted(segment_counts.items())),
        errors=tuple(sorted(set(errors))),
    )


def compare_sampling_policies(
    cases: Sequence[BenchmarkCase],
    *,
    dense: Iterable[BenchmarkObservation] | Mapping[str, BenchmarkObservation],
    adaptive: Iterable[BenchmarkObservation] | Mapping[str, BenchmarkObservation],
    min_cases: int = MIN_BENCHMARK_CASES,
    min_per_segment: int = MIN_CASES_PER_SEGMENT,
) -> SamplingComparisonReport:
    """Compara dos políticas sobre exactamente el mismo conjunto de casos.

    Las entradas pueden ser listas (se detectan duplicados) o mappings por
    ``case_id``. Cualquier ausencia, conflicto de identidad o cobertura
    insuficiente invalida la decisión, pero se conserva toda la evidencia en
    el informe para diagnóstico.
    """
    _validate_coverage_thresholds(min_cases, min_per_segment)
    cases_tuple = tuple(cases)
    coverage = validate_benchmark_coverage(
        cases_tuple, min_cases=min_cases, min_per_segment=min_per_segment
    )
    expected_ids = tuple(case_identity(case) for case in cases_tuple)
    dense_map, dense_duplicates, dense_mismatches = _observation_map(dense)
    adaptive_map, adaptive_duplicates, adaptive_mismatches = _observation_map(adaptive)
    duplicates = tuple(sorted(set(dense_duplicates + adaptive_duplicates)))
    mismatches = tuple(sorted(set(dense_mismatches + adaptive_mismatches)))
    expected_id_set = set(expected_ids)
    missing = tuple(
        sorted(
            (expected_id_set - set(dense_map)) | (expected_id_set - set(adaptive_map))
        )
    )
    extras = tuple(
        sorted((set(dense_map) | set(adaptive_map)) - expected_id_set)
    )
    dense_metrics, dense_segments = _evaluate_policy("dense", cases_tuple, dense_map)
    adaptive_metrics, adaptive_segments = _evaluate_policy("adaptive", cases_tuple, adaptive_map)
    segments = _merge_segments(dense_segments, adaptive_segments)
    top5_loss: float | None = None
    if not missing and not extras and not duplicates and not mismatches:
        top5_loss = _round((dense_metrics.top5 - adaptive_metrics.top5) * 100.0, 4)
    reduction, reduction_video_ids = _frame_reduction(dense_metrics, adaptive_metrics)
    sc004 = bool(
        not missing
        and not extras
        and not duplicates
        and not mismatches
        and adaptive_metrics.top5 >= MIN_TOP5
        and top5_loss is not None
        and top5_loss <= MAX_TOP5_LOSS_PP
    )
    sc005 = _temporal_gate(adaptive_metrics, cases_tuple, adaptive_map)
    sc006 = coverage.valid
    sc007 = (
        not missing
        and not extras
        and not duplicates
        and not mismatches
        and not dense_metrics.failures
        and not adaptive_metrics.failures
    )
    sc008 = reduction is not None and reduction >= MIN_FRAME_REDUCTION
    gates = {
        "SC-004": sc004,
        "SC-005": sc005,
        "SC-006": sc006,
        "SC-007": sc007,
        "SC-008": sc008,
    }
    return SamplingComparisonReport(
        cases=len(cases_tuple),
        coverage=coverage,
        policies={"dense": dense_metrics, "adaptive": adaptive_metrics},
        segments=segments,
        top5_loss_percentage_points=top5_loss,
        frames_reduction=reduction,
        gates=gates,
        missing_observation_ids=missing,
        duplicate_observation_ids=duplicates,
        extra_observation_ids=extras,
        observation_id_mismatches=mismatches,
        reduction_video_ids=reduction_video_ids,
        accepted=all(gates.values()),
    )


# Alias de nombre corto para consumidores de la API Python.
compare_policies = compare_sampling_policies


def load_policy_observations(path: str | Path) -> tuple[BenchmarkObservation, ...]:
    """Carga observaciones JSON de una política de forma reproducible."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"no se pueden leer resultados '{path}': {exc}") from exc
    if isinstance(raw, dict):
        raw_observations = raw.get("observations") or raw.get("results")
    else:
        raw_observations = raw
    if not isinstance(raw_observations, list):
        raise BenchmarkError(f"los resultados '{path}' no contienen observations")
    observations: list[BenchmarkObservation] = []
    for index, item in enumerate(raw_observations):
        if not isinstance(item, dict):
            raise BenchmarkError(f"la observación {index} no es un objeto")
        value = cast(dict[str, Any], item)
        ranked = value.get("ranked_video_refs", value.get("ranked_videos", ()))
        if not isinstance(ranked, (list, tuple)):
            raise BenchmarkError(f"ranked_video_refs inválido en observación {index}")
        observations.append(
            BenchmarkObservation(
                case_id=str(value.get("case_id", "")),
                predicted_video_ref=(
                    str(value["predicted_video_ref"])
                    if value.get("predicted_video_ref") is not None
                    else None
                ),
                predicted_timestamp_ms=(
                    int(value["predicted_timestamp_ms"])
                    if value.get("predicted_timestamp_ms") is not None
                    else None
                ),
                ranked_video_refs=tuple(str(ref) for ref in ranked),
                frame_count=int(value.get("frame_count", 0)),
            )
        )
    return tuple(observations)


def _observation_map(
    observations: Iterable[BenchmarkObservation] | Mapping[str, BenchmarkObservation],
) -> tuple[dict[str, BenchmarkObservation], tuple[str, ...], tuple[str, ...]]:
    items = observations.items() if isinstance(observations, Mapping) else (
        (item.case_id, item) for item in observations
    )
    result: dict[str, BenchmarkObservation] = {}
    duplicates: list[str] = []
    mismatches: list[str] = []
    for key, observation in items:
        declared_id = str(key)
        identity = observation.case_id
        if isinstance(observations, Mapping) and declared_id != identity:
            mismatches.append(f"{declared_id}!={identity}")
        if identity in result:
            duplicates.append(identity)
        result[identity] = observation
    return result, tuple(sorted(set(duplicates))), tuple(sorted(set(mismatches)))


def _evaluate_policy(
    policy: str,
    cases: Sequence[BenchmarkCase],
    observations: Mapping[str, BenchmarkObservation],
) -> tuple[PolicyMetrics, dict[str, SegmentMetrics]]:
    positives = [case for case in cases if case.expected_video_ref is not None]
    failures: list[str] = []
    top1_hits = 0
    top5_hits = 0
    temporal_errors: list[int] = []
    normalized_errors: list[float] = []
    frame_by_video: dict[str, int] = {}
    sufficient_videos: set[str] = set()
    for case in cases:
        identity = case_identity(case)
        observation = observations.get(identity)
        if observation is None:
            failures.append(identity)
            continue
        if case.expected_video_ref is not None:
            ranked = observation.ranked_video_refs
            if ranked and ranked[0] == case.expected_video_ref:
                top1_hits += 1
            if case.expected_video_ref in ranked[:5]:
                top5_hits += 1
            if (
                observation.predicted_video_ref == case.expected_video_ref
                and case.timestamp_ms is not None
                and case.duration_ms is not None
                and observation.predicted_timestamp_ms is not None
                and observation.frame_count > 0
            ):
                error = abs(observation.predicted_timestamp_ms - case.timestamp_ms)
                temporal_errors.append(error)
                normalized_errors.append(
                    error / (case.duration_ms / observation.frame_count)
                )
            elif case.timestamp_ms is not None:
                failures.append(f"temporal:{identity}")
        video_ref = case.expected_video_ref
        if video_ref:
            previous = frame_by_video.get(video_ref)
            if previous is not None and previous != observation.frame_count:
                failures.append(f"frame_count_conflict:{video_ref}")
            frame_by_video[video_ref] = observation.frame_count
            if observation.frame_count >= DENSE_REFERENCE_FRAMES:
                sufficient_videos.add(video_ref)
    frames = sum(frame_by_video.values())
    metrics = PolicyMetrics(
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
        normalized_error={
            "median": _percentile_float(normalized_errors, 50),
            "p95": _percentile_float(normalized_errors, 95),
        },
        frames=frames,
        videos=len(frame_by_video),
        video_frames=dict(sorted(frame_by_video.items())),
        sufficient_videos=len(sufficient_videos),
        failures=tuple(sorted(set(failures))),
    )
    segments = _evaluate_segments(policy, cases, observations)
    return metrics, segments


def _evaluate_segments(
    policy: str,
    cases: Sequence[BenchmarkCase],
    observations: Mapping[str, BenchmarkObservation],
) -> dict[str, SegmentMetrics]:
    grouped: dict[str, list[tuple[BenchmarkCase, BenchmarkObservation]]] = defaultdict(list)
    for case in cases:
        bucket = duration_bucket(case.duration_ms)
        observation = observations.get(case_identity(case))
        if (
            case.expected_video_ref is not None
            and bucket is not None
            and observation is not None
        ):
            grouped[f"{case.source}/{bucket}"].append((case, observation))
    segments: dict[str, SegmentMetrics] = {}
    for name, values in grouped.items():
        positives = [item for item in values if item[0].expected_video_ref is not None]
        top1 = sum(
            bool(
                item[1].ranked_video_refs
                and item[1].ranked_video_refs[0] == item[0].expected_video_ref
            )
            for item in positives
        )
        top5 = sum(
            item[0].expected_video_ref in item[1].ranked_video_refs[:5] for item in positives
        )
        errors = [
            abs(item[1].predicted_timestamp_ms - item[0].timestamp_ms)
            for item in positives
            if item[1].predicted_video_ref == item[0].expected_video_ref
            and item[0].timestamp_ms is not None
            and item[1].predicted_timestamp_ms is not None
        ]
        segments[name] = SegmentMetrics(
            policy=policy,
            source=values[0][0].source,
            duration_bucket=duration_bucket(values[0][0].duration_ms) or "unknown",
            cases=len(values),
            top1=_ratio(top1, len(positives)),
            top5=_ratio(top5, len(positives)),
            temporal_error_ms={
                "median": _percentile_int(errors, 50),
                "p95": _percentile_int(errors, 95),
            },
        )
    return segments


def _merge_segments(
    dense: Mapping[str, SegmentMetrics], adaptive: Mapping[str, SegmentMetrics]
) -> dict[str, SegmentComparison]:
    merged: dict[str, SegmentComparison] = {}
    for name in sorted(set(dense) | set(adaptive)):
        merged[name] = SegmentComparison(dense=dense.get(name), adaptive=adaptive.get(name))
    return merged


def _frame_reduction(
    dense: PolicyMetrics, adaptive: PolicyMetrics
) -> tuple[float | None, tuple[str, ...]]:
    """Calcula SC-008 solo sobre vídeos dense>=30 presentes en ambos índices."""
    video_ids = tuple(
        sorted(
            video_id
            for video_id, frame_count in dense.video_frames.items()
            if frame_count >= DENSE_REFERENCE_FRAMES and video_id in adaptive.video_frames
        )
    )
    dense_frames = sum(dense.video_frames[video_id] for video_id in video_ids)
    adaptive_frames = sum(adaptive.video_frames[video_id] for video_id in video_ids)
    if dense_frames <= 0:
        return None, video_ids
    return _round(1.0 - adaptive_frames / dense_frames, 4), video_ids


def _temporal_gate(
    metrics: PolicyMetrics,
    cases: Sequence[BenchmarkCase],
    observations: Mapping[str, BenchmarkObservation],
) -> bool:
    median = metrics.normalized_error.get("median")
    if median is None or metrics.temporal_cases == 0:
        return False
    interval_limits = [
        case.duration_ms / observation.frame_count / 2.0
        for case in cases
        if (observation := observations.get(case_identity(case))) is not None
        and case.duration_ms is not None
        and observation.frame_count > 0
        and case.timestamp_ms is not None
        and observation.predicted_timestamp_ms is not None
        and observation.predicted_video_ref == case.expected_video_ref
    ]
    absolute_median = metrics.temporal_error_ms.get("median")
    interval_median = _percentile_float(interval_limits, 50)
    return bool(
        median <= MAX_NORMALIZED_MEDIAN_ERROR
        and absolute_median is not None
        and interval_median is not None
        and absolute_median <= interval_median
    )


def _ratio(numerator: int, denominator: int) -> float:
    return _round(numerator / denominator if denominator else 0.0, 4)


def _percentile_int(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return int(ordered[rank - 1])


def _percentile_float(values: Sequence[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return _round(float(ordered[rank - 1]), 4)


def _round(value: float, digits: int) -> float:
    return float(round(value, digits))


def _validate_coverage_thresholds(min_cases: int, min_per_segment: int) -> None:
    if min_cases < MIN_BENCHMARK_CASES:
        raise ValueError(
            f"min_cases no puede ser inferior a {MIN_BENCHMARK_CASES} "
            f"(recibido {min_cases})"
        )
    if min_per_segment < MIN_CASES_PER_SEGMENT:
        raise ValueError(
            f"min_per_segment no puede ser inferior a {MIN_CASES_PER_SEGMENT} "
            f"(recibido {min_per_segment})"
        )
