"""Tests del generador del dataset de benchmark (PR-015 · FR-015 · D3).

Criterios verificables (tasks.md PR-015 · spec 001):
- Recuento por variante: ~30 por cada una de las 6 variantes positivas +
  ~30 negativas (FR-015 · Decisión D3: ~210 casos).
- Negativas sin vídeo esperado (expected_video_ref None).
- Reproducible: mismo seed -> mismos casos y mismos bytes (SC-007 mindset).
- Variantes con semántica verificable (exacta idéntica, JPEG, tamaños,
  alteración de píxeles, etiquetas).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from tests.fixtures.benchmark import make_benchmark_frames
from xtrace_spike.benchmark import (
    NEGATIVE_VARIANT,
    POSITIVE_VARIANTS,
    BenchmarkDataset,
    BenchmarkError,
    SourceFrame,
    generate_benchmark_dataset,
    load_manifest,
    scan_frames_root,
)

VIDEO_REFS: tuple[str, ...] = ("video_000", "video_001", "video_002", "video_003")


@pytest.fixture(scope="session")
def frame_pool(tmp_path_factory: pytest.TempPathFactory) -> tuple[SourceFrame, ...]:
    """Pool compartido de 40 frames sintéticos (4 vídeos x 10), solo lectura."""
    return make_benchmark_frames(tmp_path_factory.mktemp("benchmark-frames"))


def _pixels(path: Path) -> np.ndarray[Any, Any]:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"))


def _case_keys(dataset: BenchmarkDataset) -> tuple[tuple[str, str, str], ...]:
    """Claves estables de un dataset (variante, esperado, path relativo)."""
    return tuple(
        (
            case.variant,
            case.expected_video_ref or "",
            case.query_image_path.relative_to(dataset.out_dir).as_posix(),
        )
        for case in dataset.cases
    )


# ---------------------------------------------------------------------------
# FR-015 · Recuentos por variante
# ---------------------------------------------------------------------------


def test_default_generation_counts_210(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """FR-015/D3: por defecto 6 x 30 positivos + 30 negativas = 210 casos."""
    dataset = generate_benchmark_dataset(frame_pool, tmp_path / "out")

    counts = dataset.counts_by_variant()
    assert len(dataset.cases) == 210
    assert sum(counts.values()) == 210
    for variant in POSITIVE_VARIANTS:
        assert counts[variant] == 30
    assert counts[NEGATIVE_VARIANT] == 30
    assert dataset.manifest_path.is_file()


def test_custom_counts_and_variants(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """FR-015: recuentos y variantes configurables (generador configurable)."""
    dataset = generate_benchmark_dataset(
        frame_pool,
        tmp_path / "out",
        cases_per_variant=2,
        negative_cases=3,
        variants=("exact", "color"),
    )

    assert len(dataset.cases) == 2 * 2 + 3
    assert dataset.counts_by_variant() == {"exact": 2, "color": 2, "negative": 3}


def test_variant_labels_contract() -> None:
    """Contrato de etiquetas para el runner (PR-016): 6 positivas + negative."""
    assert POSITIVE_VARIANTS == ("exact", "compressed", "cropped", "watermark", "resized", "color")
    assert NEGATIVE_VARIANT == "negative"


# ---------------------------------------------------------------------------
# Etiquetas: positivos con vídeo esperado, negativas sin él
# ---------------------------------------------------------------------------


def test_positive_cases_have_expected_video_and_files(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """FR-015: positivos etiquetados con un vídeo real y consultas en disco."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=5, negative_cases=3
    )
    positives = [c for c in dataset.cases if c.variant != NEGATIVE_VARIANT]

    assert len(positives) == 6 * 5
    for case in dataset.cases:
        assert case.query_image_path.is_file()
        assert case.query_image_path.stat().st_size > 0
    for case in positives:
        assert case.expected_video_ref in VIDEO_REFS
        assert case.source_frame_path is not None
        assert case.source_frame_path.is_file()


def test_negative_cases_have_no_expected_video(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """FR-015: negativas sin vídeo esperado y sin frame de origen (sintéticas)."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=3, negative_cases=4
    )

    for case in dataset.cases:
        if case.variant == NEGATIVE_VARIANT:
            assert case.expected_video_ref is None
            assert case.source_frame_path is None
        else:
            assert case.expected_video_ref is not None


def test_query_image_paths_unique(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """FR-015: cada caso tiene una imagen de consulta distinta."""
    dataset = generate_benchmark_dataset(frame_pool, tmp_path / "out")
    paths = [case.query_image_path for case in dataset.cases]
    assert len(set(paths)) == len(paths)


# ---------------------------------------------------------------------------
# Reproducibilidad (semilla fija) · SC-007 mindset
# ---------------------------------------------------------------------------


def test_same_seed_reproduces_cases_and_bytes(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """SC-007: mismo seed -> mismos casos, manifest y bytes de imagen."""
    first = generate_benchmark_dataset(
        frame_pool, tmp_path / "out1", seed=7, cases_per_variant=4, negative_cases=3
    )
    second = generate_benchmark_dataset(
        frame_pool, tmp_path / "out2", seed=7, cases_per_variant=4, negative_cases=3
    )

    assert _case_keys(first) == _case_keys(second)
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    for a, b in zip(first.cases, second.cases, strict=True):
        assert a.query_image_path.read_bytes() == b.query_image_path.read_bytes()


def test_different_seed_changes_sampling(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """FR-015: el sampling depende del seed (negativas y frames elegidos)."""
    first = generate_benchmark_dataset(
        frame_pool, tmp_path / "out1", seed=1, cases_per_variant=5, negative_cases=2
    )
    second = generate_benchmark_dataset(
        frame_pool, tmp_path / "out2", seed=2, cases_per_variant=5, negative_cases=2
    )
    assert _case_keys(first) != _case_keys(second)


# ---------------------------------------------------------------------------
# Semántica de las 6 variantes positivas
# ---------------------------------------------------------------------------


def test_exact_variant_is_pixel_identical(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """exacta: píxeles idénticos al frame indexado (y bytes copiados)."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=0, variants=("exact",)
    )
    for case in dataset.cases:
        assert case.source_frame_path is not None
        assert case.query_image_path.read_bytes() == case.source_frame_path.read_bytes()
        assert np.array_equal(_pixels(case.query_image_path), _pixels(case.source_frame_path))


def test_compressed_variant_is_jpeg(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """comprimida: JPEG con calidad baja; píxeles NO idénticos."""
    dataset = generate_benchmark_dataset(
        frame_pool,
        tmp_path / "out",
        cases_per_variant=2,
        negative_cases=0,
        variants=("compressed",),
    )
    for case in dataset.cases:
        assert case.source_frame_path is not None
        assert case.query_image_path.suffix == ".jpg"
        with Image.open(case.query_image_path) as image:
            assert image.format == "JPEG"
            assert image.size == Image.open(case.source_frame_path).size
        assert not np.array_equal(_pixels(case.query_image_path), _pixels(case.source_frame_path))


def test_cropped_variant_same_size_and_differs(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """recortada: recorte central + resize al tamaño original."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=0, variants=("cropped",)
    )
    for case in dataset.cases:
        assert case.source_frame_path is not None
        query = _pixels(case.query_image_path)
        source = _pixels(case.source_frame_path)
        assert query.shape == source.shape
        assert not np.array_equal(query, source)


def test_resized_variant_is_half_size(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """redimensionada: resize menor (50%) del frame original."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=0, variants=("resized",)
    )
    for case in dataset.cases:
        assert case.source_frame_path is not None
        source = _pixels(case.source_frame_path)
        query = _pixels(case.query_image_path)
        assert source.shape == (240, 320, 3)
        assert query.shape == (120, 160, 3)


def test_watermark_variant_same_size_and_differs(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """watermark: sello semitransparente sobre el frame, mismo tamaño."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=0, variants=("watermark",)
    )
    for case in dataset.cases:
        assert case.source_frame_path is not None
        query = _pixels(case.query_image_path)
        source = _pixels(case.source_frame_path)
        assert query.shape == source.shape
        assert not np.array_equal(query, source)


def test_color_variant_alters_pixels(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """color: ganancias por canal + giro de hue; alteración relevante."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=0, variants=("color",)
    )
    for case in dataset.cases:
        assert case.source_frame_path is not None
        query = _pixels(case.query_image_path).astype(np.float32)
        source = _pixels(case.source_frame_path).astype(np.float32)
        assert query.shape == source.shape
        assert not np.array_equal(query, source)
        # ganancia R x1.15 vs G x0.85: el equilibrio R-G cambia de forma estable
        balance = (query[..., 0] - query[..., 1]).mean() - (source[..., 0] - source[..., 1]).mean()
        assert balance > 10.0


# ---------------------------------------------------------------------------
# Negativas: sintéticas, deterministas, fuera del dataset
# ---------------------------------------------------------------------------


def test_negative_images_are_synthetic_and_deterministic(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """negativas: sintetizadas (no copias de frames) y reproducibles."""
    first = generate_benchmark_dataset(
        frame_pool, tmp_path / "out1", seed=5, cases_per_variant=2, negative_cases=4
    )
    second = generate_benchmark_dataset(
        frame_pool, tmp_path / "out2", seed=5, cases_per_variant=2, negative_cases=4
    )
    negatives_first = [c for c in first.cases if c.variant == NEGATIVE_VARIANT]
    negatives_second = [c for c in second.cases if c.variant == NEGATIVE_VARIANT]
    assert len(negatives_first) == 4
    for a, b in zip(negatives_first, negatives_second, strict=True):
        assert a.query_image_path.read_bytes() == b.query_image_path.read_bytes()


# ---------------------------------------------------------------------------
# Validación de entrada y errores controlados
# ---------------------------------------------------------------------------


def test_insufficient_frames_raises(tmp_path: Path) -> None:
    """FR-015: pool menor que cases_per_variant -> error controlado."""
    pool = make_benchmark_frames(tmp_path / "frames", videos=1, frames_per_video=4)
    with pytest.raises(BenchmarkError, match="al menos 10"):
        generate_benchmark_dataset(pool, tmp_path / "out", cases_per_variant=10)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"cases_per_variant": 0}, "cases_per_variant debe ser > 0"),
        ({"cases_per_variant": -1}, "cases_per_variant debe ser > 0"),
        ({"negative_cases": -1}, "negative_cases no puede ser negativo"),
        ({"variants": ("bogus",)}, "variantes desconocidas: bogus"),
        ({"variants": ()}, "variants no puede estar vacío"),
    ],
)
def test_invalid_configuration_raises(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path, kwargs: dict[str, Any], match: str
) -> None:
    """FR-015: configuración inválida -> BenchmarkError (error controlado)."""
    with pytest.raises(BenchmarkError, match=match):
        generate_benchmark_dataset(frame_pool, tmp_path / "out", **kwargs)


def test_missing_frame_file_raises(tmp_path: Path) -> None:
    """FR-015: frame declarado pero ausente en disco -> error controlado."""
    pool = make_benchmark_frames(tmp_path / "frames", videos=1, frames_per_video=4)
    pool[0].path.unlink()
    with pytest.raises(BenchmarkError, match="faltan ficheros"):
        generate_benchmark_dataset(pool, tmp_path / "out", cases_per_variant=2)


def test_scan_frames_root_missing_raises(tmp_path: Path) -> None:
    """scan_frames_root: root inexistente -> BenchmarkError."""
    with pytest.raises(BenchmarkError, match="no existe"):
        scan_frames_root(tmp_path / "nope")


# ---------------------------------------------------------------------------
# Manifest JSON (consumido por el runner, PR-016)
# ---------------------------------------------------------------------------


def test_manifest_json_roundtrip(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """Manifest: configuración + casos con paths relativos; load_manifest los resuelve."""
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", seed=11, cases_per_variant=3, negative_cases=2
    )
    data = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))

    assert data["seed"] == 11
    assert data["cases_per_variant"] == 3
    assert data["negative_cases"] == 2
    assert data["total_cases"] == 6 * 3 + 2
    assert len(data["cases"]) == data["total_cases"]
    assert all(not Path(entry["query_image_path"]).is_absolute() for entry in data["cases"])

    loaded = load_manifest(dataset.manifest_path)
    assert len(loaded) == len(dataset.cases)
    for original, case in zip(dataset.cases, loaded, strict=True):
        assert case.query_image_path.is_file()
        assert case.variant == original.variant
        assert case.expected_video_ref == original.expected_video_ref
        assert case.source_frame_path == original.source_frame_path


def test_scan_frames_root_layout(tmp_path: Path) -> None:
    """scan_frames_root: layout <root>/<video_ref>/frame_*.png, orden estable."""
    pool = make_benchmark_frames(tmp_path / "frames", videos=2, frames_per_video=3)
    scanned = scan_frames_root(tmp_path / "frames")

    assert len(scanned) == 6
    assert [frame.video_ref for frame in scanned] == ["video_000"] * 3 + ["video_001"] * 3
    assert all(frame.path.is_file() for frame in scanned)
    assert scanned == tuple(sorted(pool, key=lambda frame: (frame.video_ref, str(frame.path))))


def test_generator_works_without_db_or_torch(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """El generador solo necesita Pillow + numpy (sin DB ni torch)."""
    before = set(sys.modules)
    dataset = generate_benchmark_dataset(
        frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=1
    )
    newly_imported = {name.split(".")[0] for name in set(sys.modules) - before}
    assert not newly_imported & {"torch", "psycopg", "sqlalchemy"}
    assert len(dataset.cases) == 6 * 2 + 1
