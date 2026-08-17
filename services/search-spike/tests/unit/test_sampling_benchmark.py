"""Tests del benchmark adaptativo frente a la referencia densa (TASK-005-004)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from xtrace_spike.benchmark import (
    BenchmarkCase,
    SourceFrame,
    generate_benchmark_dataset,
    load_manifest,
)
from xtrace_spike.benchmark.sampling import (
    BenchmarkObservation,
    case_identity,
    compare_sampling_policies,
    validate_benchmark_coverage,
)
from xtrace_spike.cli import app


def _cases() -> tuple[BenchmarkCase, ...]:
    cases: list[BenchmarkCase] = []
    durations = (240_000, 600_000, 1_200_000)
    sources = ("local", "web")
    index = 0
    for source in sources:
        for duration_ms in durations:
            for _ in range(6):
                case_id = f"case-{index:03d}"
                cases.append(
                    BenchmarkCase(
                        query_image_path=Path(f"queries/{case_id}.png"),
                        variant="exact",
                        expected_video_ref=f"video-{index:03d}",
                        source=source,
                        duration_ms=duration_ms,
                        timestamp_ms=duration_ms // 2,
                        case_id=case_id,
                    )
                )
                index += 1
    return tuple(cases)


def _observations(
    cases: tuple[BenchmarkCase, ...], *, adaptive: bool = False
) -> tuple[BenchmarkObservation, ...]:
    return tuple(
        BenchmarkObservation(
            case_id=case.case_id or "",
            predicted_video_ref=case.expected_video_ref,
            predicted_timestamp_ms=(case.timestamp_ms or 0) + (1_000 if adaptive else 0),
            ranked_video_refs=(case.expected_video_ref or "", "other-video"),
            frame_count=8 if adaptive else 30,
        )
        for case in cases
    )


def test_valid_comparison_is_paired_and_measures_all_gates() -> None:
    """FR-012/013 + SC-004..008: mismas 36 consultas y puertas cuantificadas."""
    cases = _cases()

    report = compare_sampling_policies(
        cases,
        dense=_observations(cases),
        adaptive=_observations(cases, adaptive=True),
    )

    assert report.accepted is True
    assert report.policies["dense"].top1 == 1.0
    assert report.policies["adaptive"].top5 == 1.0
    assert report.policies["adaptive"].temporal_error_ms["median"] == 1_000
    assert report.policies["adaptive"].normalized_error["median"] < 0.5
    assert report.frames_reduction == 0.7333
    assert report.gates == {
        "SC-004": True,
        "SC-005": True,
        "SC-006": True,
        "SC-007": True,
        "SC-008": True,
    }
    assert set(report.segments) == {
        "local/<5m",
        "local/5-15m",
        "local/>15m",
        "web/<5m",
        "web/5-15m",
        "web/>15m",
    }
    assert report.segments["local/<5m"].dense.top5 == 1.0
    assert report.segments["local/<5m"].adaptive.top5 == 1.0


def test_comparison_fails_closed_for_missing_pair_and_insufficient_coverage() -> None:
    """SC-006/SC-007: nunca se declara adopción con pares/cobertura incompletos."""
    cases = _cases()[:4]
    dense = _observations(cases)
    adaptive = _observations(cases[:-1], adaptive=True)

    coverage = validate_benchmark_coverage(cases)
    report = compare_sampling_policies(cases, dense=dense, adaptive=adaptive)

    assert coverage.valid is False
    assert report.accepted is False
    assert report.gates["SC-006"] is False
    assert report.gates["SC-007"] is False
    assert report.missing_observation_ids == ("case-003",)


def test_sc008_reduction_uses_only_dense_eligible_videos_paired_in_both_policies() -> None:
    """SC-008: dense-only vídeo no puede inflar artificialmente la reducción."""
    cases = _cases()
    dense = _observations(cases)
    adaptive = _observations(cases[:-1], adaptive=True)

    report = compare_sampling_policies(cases, dense=dense, adaptive=adaptive)

    # 35 vídeos pareados: (30 - 8) / 30, no 1 - 280/1080 contando el vídeo
    # dense-only en el denominador.
    assert report.frames_reduction == 0.7333
    assert report.reduction_video_ids == tuple(f"video-{index:03d}" for index in range(35))


def test_positive_cases_need_real_valid_duration_and_timestamp() -> None:
    """SC-006/FR-013: un positivo sin verdad temporal invalida la cobertura."""
    cases = list(_cases())
    cases[0] = BenchmarkCase(
        query_image_path=cases[0].query_image_path,
        variant=cases[0].variant,
        expected_video_ref=cases[0].expected_video_ref,
        source=cases[0].source,
        duration_ms=None,
        timestamp_ms=None,
        case_id=cases[0].case_id,
    )

    coverage = validate_benchmark_coverage(tuple(cases))

    assert coverage.valid is False
    assert any(error.startswith("invalid_truth:") for error in coverage.errors)


def test_standard_manifest_accepts_30_positives_plus_unpositioned_negatives(
    tmp_path: Path,
) -> None:
    """SC-006: negativos estándar sin duración/timestamp no invalidan 30 positivos."""
    frames: list[SourceFrame] = []
    index = 0
    for source in ("local", "web"):
        for duration_ms in (240_000, 600_000, 1_200_000):
            for _ in range(5):
                frame_path = tmp_path / "frames" / source / f"frame-{index:03d}.png"
                frame_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (index, index, index)).save(frame_path)
                frames.append(
                    SourceFrame(
                        video_ref=f"video-{index:03d}",
                        path=frame_path,
                        source=source,
                        duration_ms=duration_ms,
                        timestamp_ms=duration_ms // 2,
                    )
                )
                index += 1
    dataset = generate_benchmark_dataset(
        frames,
        tmp_path / "benchmark",
        cases_per_variant=30,
        negative_cases=5,
        variants=("exact",),
    )
    cases = load_manifest(dataset.manifest_path)

    def observations(adaptive: bool) -> tuple[BenchmarkObservation, ...]:
        return tuple(
            BenchmarkObservation(
                case_id=case_identity(case),
                predicted_video_ref=case.expected_video_ref,
                predicted_timestamp_ms=(
                    case.timestamp_ms if case.expected_video_ref is not None else None
                ),
                ranked_video_refs=(
                    (case.expected_video_ref,) if case.expected_video_ref is not None else ()
                ),
                frame_count=(8 if adaptive else 30) if case.expected_video_ref is not None else 0,
            )
            for case in cases
        )

    coverage = validate_benchmark_coverage(cases)
    report = compare_sampling_policies(
        cases,
        dense=observations(False),
        adaptive=observations(True),
    )

    assert coverage.valid is True
    assert coverage.case_count == 35
    assert coverage.positive_case_count == 30
    assert coverage.negative_case_count == 5
    assert report.missing_observation_ids == ()
    assert report.accepted is True


def test_negatives_do_not_fill_minimum_positive_cases() -> None:
    """SC-006: 29 positivos + negativos siguen sin alcanzar el mínimo de 30."""
    positives = _cases()[:29]
    negatives = tuple(
        BenchmarkCase(
            query_image_path=Path(f"queries/negative-{index}.png"),
            variant="negative",
            expected_video_ref=None,
            case_id=f"negative-{index}",
        )
        for index in range(10)
    )

    coverage = validate_benchmark_coverage(positives + negatives)

    assert coverage.valid is False
    assert coverage.case_count == 39
    assert coverage.positive_case_count == 29
    assert coverage.negative_case_count == 10
    assert "minimum_positive_cases:29<30" in coverage.errors


def test_observation_ids_must_equal_cases_and_mapping_keys_must_match() -> None:
    """SC-007: extras y claves inconsistentes se rechazan, no se silencian."""
    cases = _cases()
    dense = _observations(cases)
    adaptive = _observations(cases, adaptive=True) + (
        BenchmarkObservation(
            case_id="extra-case",
            predicted_video_ref="extra-video",
            predicted_timestamp_ms=1,
            frame_count=8,
        ),
    )

    extra_report = compare_sampling_policies(cases, dense=dense, adaptive=adaptive)
    mismatch_report = compare_sampling_policies(
        cases,
        dense={
            case.case_id or "": observation
            for case, observation in zip(cases, dense, strict=True)
        },
        adaptive={
            "case-000": BenchmarkObservation(
                case_id="case-001",
                predicted_video_ref="video-001",
                predicted_timestamp_ms=1,
                frame_count=8,
            )
        },
    )

    assert extra_report.extra_observation_ids == ("extra-case",)
    assert extra_report.gates["SC-007"] is False
    assert mismatch_report.observation_id_mismatches == ("case-000!=case-001",)
    assert mismatch_report.gates["SC-007"] is False


def test_minimum_coverage_thresholds_cannot_be_relaxed() -> None:
    """SC-006: API y CLI solo aceptan los mínimos aprobados o valores mayores."""
    cases = _cases()
    observations = _observations(cases)

    with pytest.raises(ValueError, match="min_cases"):
        compare_sampling_policies(
            cases, dense=observations, adaptive=_observations(cases, adaptive=True), min_cases=29
        )
    with pytest.raises(ValueError, match="min_per_segment"):
        compare_sampling_policies(
            cases,
            dense=observations,
            adaptive=_observations(cases, adaptive=True),
            min_per_segment=2,
        )


def test_cli_rejects_relaxed_coverage_threshold(tmp_path: Path) -> None:
    """SC-006: la CLI tampoco permite rebajar el mínimo aprobado."""
    cases = _cases()
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": case.case_id,
                        "expected_video_ref": case.expected_video_ref,
                        "source": case.source,
                        "duration_ms": case.duration_ms,
                        "timestamp_ms": case.timestamp_ms,
                    }
                    for case in cases
                ]
            }
        ),
        encoding="utf-8",
    )
    dense_path = tmp_path / "dense.json"
    adaptive_path = tmp_path / "adaptive.json"
    dense_path.write_text(
        json.dumps({"observations": [item.to_dict() for item in _observations(cases)]}),
        encoding="utf-8",
    )
    adaptive_path.write_text(
        json.dumps(
            {"observations": [item.to_dict() for item in _observations(cases, adaptive=True)]}
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "sampling-benchmark",
            "--cases",
            str(sidecar),
            "--dense-results",
            str(dense_path),
            "--adaptive-results",
            str(adaptive_path),
            "--min-cases",
            "29",
        ],
    )

    assert result.exit_code == 2
    assert "min_cases" in result.stdout


def test_top5_loss_and_frame_reduction_gates_reject_bad_adaptive_policy() -> None:
    """SC-004/SC-008: caída de recall o ahorro insuficiente bloquea el default."""
    cases = _cases()
    adaptive = tuple(
        BenchmarkObservation(
            case_id=case.case_id or "",
            predicted_video_ref=(case.expected_video_ref if index < 20 else "wrong-video"),
            predicted_timestamp_ms=case.timestamp_ms,
            ranked_video_refs=(
                (case.expected_video_ref or "",) if index < 20 else ("wrong-video",)
            ),
            frame_count=12,
        )
        for index, case in enumerate(cases)
    )

    report = compare_sampling_policies(cases, dense=_observations(cases), adaptive=adaptive)

    assert report.accepted is False
    assert report.gates["SC-004"] is False
    assert report.gates["SC-008"] is False
    assert report.policies["adaptive"].top5 < 0.8


def test_report_json_is_deterministic_and_cli_reads_sidecar_and_results(tmp_path: Path) -> None:
    """FR-012/014: JSON estable y comando reproducible sin cambiar el default legacy."""
    cases = _cases()
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": case.case_id,
                        "query_image_path": str(case.query_image_path),
                        "expected_video_ref": case.expected_video_ref,
                        "source": case.source,
                        "duration_ms": case.duration_ms,
                        "timestamp_ms": case.timestamp_ms,
                    }
                    for case in cases
                ]
            }
        ),
        encoding="utf-8",
    )
    dense_path = tmp_path / "dense.json"
    adaptive_path = tmp_path / "adaptive.json"
    for path, observations in (
        (dense_path, _observations(cases)),
        (adaptive_path, _observations(cases, adaptive=True)),
    ):
        path.write_text(
            json.dumps(
                {"observations": [observation.to_dict() for observation in observations]}
            ),
            encoding="utf-8",
        )

    direct = compare_sampling_policies(
        cases,
        dense=_observations(cases),
        adaptive=_observations(cases, adaptive=True),
    )
    assert direct.to_json() == direct.to_json()
    payload = json.loads(direct.to_json())
    assert payload["accepted"] is True
    assert payload["default_adopted"] is False

    result = CliRunner().invoke(
        app,
        [
            "sampling-benchmark",
            "--cases",
            str(sidecar),
            "--dense-results",
            str(dense_path),
            "--adaptive-results",
            str(adaptive_path),
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["accepted"] is True
