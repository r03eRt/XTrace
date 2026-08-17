"""Benchmark de búsqueda visual: generación del dataset de casos (PR-015 · FR-015 · D3).

Expone el generador determinista (~210 casos: ~30 por cada una de las 6
variantes positivas + ~30 negativas, Decisión D3) y la estructura
BenchmarkCase que consumirá el runner (PR-016).
"""

from xtrace_spike.benchmark.dataset import (
    DEFAULT_CASES_PER_VARIANT,
    DEFAULT_NEGATIVE_CASES,
    DEFAULT_SEED,
    NEGATIVE_VARIANT,
    POSITIVE_VARIANTS,
    BenchmarkCase,
    BenchmarkDataset,
    BenchmarkError,
    SourceFrame,
    generate_benchmark_dataset,
    load_benchmark_sidecar,
    load_case_sidecar,
    load_frame_sidecar,
    load_manifest,
    load_sidecar,
    scan_frames_root,
)
from xtrace_spike.benchmark.sampling import (
    BenchmarkObservation,
    CoverageReport,
    PolicyMetrics,
    SamplingComparisonReport,
    SegmentComparison,
    SegmentMetrics,
    compare_sampling_policies,
    validate_benchmark_coverage,
)

__all__ = [
    "DEFAULT_CASES_PER_VARIANT",
    "DEFAULT_NEGATIVE_CASES",
    "DEFAULT_SEED",
    "NEGATIVE_VARIANT",
    "POSITIVE_VARIANTS",
    "BenchmarkCase",
    "BenchmarkDataset",
    "BenchmarkError",
    "SourceFrame",
    "generate_benchmark_dataset",
    "load_benchmark_sidecar",
    "load_case_sidecar",
    "load_frame_sidecar",
    "load_manifest",
    "load_sidecar",
    "scan_frames_root",
    "BenchmarkObservation",
    "CoverageReport",
    "PolicyMetrics",
    "SamplingComparisonReport",
    "SegmentComparison",
    "SegmentMetrics",
    "compare_sampling_policies",
    "validate_benchmark_coverage",
]
