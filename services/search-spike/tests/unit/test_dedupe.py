"""Unit tests de deduplicación de frames por pHash (PR-009 · FR-003 · ADR-0005).

Criterios verificables (tasks.md PR-009 · spec 001 FR-003):
- Dataset con frames idénticos -> 1 representativo (el primero).
- El umbral Hamming es configurable y respeta la variación: frames casi
  idénticos se colapsan solo cuando su distancia es <= umbral.
- Dedupe determinista: mismo input -> mismo output (SC-007 mindset).

Los tests no requieren FFmpeg: construyen ExtractedFrame directamente con
imágenes sintéticas deterministas (PIL + numpy) guardadas como PNG en
tmp_path, reutilizando el contrato de hashing.phash (PR-004).
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from xtrace_spike.hashing.phash import PHASH_BITS, compute_phash, hamming_distance
from xtrace_spike.ingest import IngestError
from xtrace_spike.ingest.dedupe import DEFAULT_HAMMING_THRESHOLD, DedupeError, dedupe_frames
from xtrace_spike.ingest.frames import ExtractedFrame

_WIDTH = 640
_HEIGHT = 480


def _synthetic_image(seed: int) -> Image.Image:
    """Imagen sintética determinista con estructura tipo frame de vídeo.

    Misma familia que tests/unit/test_phash.py: la semilla controla paleta,
    formas y ruido; semillas distintas producen imágenes claramente distintas.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:_HEIGHT, 0:_WIDTH]

    phase = int(rng.integers(0, 256))
    img = np.zeros((_HEIGHT, _WIDTH, 3), dtype=np.uint8)
    img[..., 0] = ((xx * 255) // _WIDTH + phase) % 256
    img[..., 1] = ((yy * 255) // _HEIGHT + phase * 2) % 256
    img[..., 2] = (((xx + yy) * 255) // (_WIDTH + _HEIGHT) + phase * 3) % 256

    block = int(rng.integers(12, 40))
    x_max = int(rng.integers(120, 280))
    y_max = int(rng.integers(80, 200))
    checker = ((xx // block) + (yy // block)) % 2
    mask = (xx < x_max) & (yy < y_max)
    tone = int(rng.integers(160, 240))
    img[mask] = np.where(checker[mask, None] == 0, tone, tone // 5)

    x0, y0 = int(rng.integers(0, 200)), int(rng.integers(0, 120))
    x1, y1 = int(rng.integers(320, 480)), int(rng.integers(200, 360))
    img[y0 : y0 + 100, x0 : x0 + 160] = rng.integers(0, 256, size=3)
    img[y1 : y1 + 90, x1 : x1 + 160] = rng.integers(0, 256, size=3)

    cx, cy = int(rng.integers(80, 560)), int(rng.integers(60, 420))
    r_in = int(rng.integers(20, 50))
    radius = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    ring = (radius > r_in) & (radius < r_in + 30)
    img[ring] = rng.integers(0, 256, size=3)

    noise = rng.integers(0, 24, size=(_HEIGHT, _WIDTH, 1), dtype=np.uint8)
    return Image.fromarray(np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8))


def _reencode(image: Image.Image, fmt: str, **save_kwargs: object) -> Image.Image:
    """Re-encodea la imagen en memoria (p. ej. JPEG) y la recarga."""
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **save_kwargs)
    buffer.seek(0)
    return Image.open(buffer)


def _frame(path: Path, timestamp_ms: int, image: Image.Image) -> ExtractedFrame:
    """Construye un ExtractedFrame apuntando a un PNG en disco (FR-002 contrato)."""
    return ExtractedFrame(
        path=path,
        timestamp_ms=timestamp_ms,
        width=image.width,
        height=image.height,
    )


def _write_frame(
    tmp_path: Path, name: str, timestamp_ms: int, image: Image.Image
) -> ExtractedFrame:
    """Guarda la imagen como PNG y devuelve el ExtractedFrame correspondiente."""
    path = tmp_path / f"{name}_{timestamp_ms}.png"
    image.save(path, format="PNG")
    return _frame(path, timestamp_ms, image)


def _dist(a: Image.Image, b: Image.Image) -> int:
    """Distancia de Hamming entre los pHash de dos imágenes (API pública PR-004)."""
    return hamming_distance(compute_phash(a), compute_phash(b))


def _near_variant(base: Image.Image) -> tuple[Image.Image, int]:
    """Variante con distancia pequeña pero > 0 respecto a base.

    Prueba una lista fija de perturbaciones leves (crop 1-4% y brillo) y
    devuelve la primera con 0 < distancia <= umbral por defecto. Para las
    semillas fijas usadas en los tests siempre existe al menos una; si no,
    el assert falla alto indicando que el escenario ya no es válido.
    """
    candidates: list[Image.Image] = [
        base.crop((int(_WIDTH * pct / 100), int(_HEIGHT * pct / 100), _WIDTH, _HEIGHT))
        for pct in (1, 2, 3, 4)
    ]
    candidates += [base.point(lambda p, delta=d: min(255, int(p) + delta)) for d in (40, 60, 90)]
    for variant in candidates:
        distance = _dist(base, variant)
        if 0 < distance <= DEFAULT_HAMMING_THRESHOLD:
            return variant, distance
    raise AssertionError("ninguna perturbación leve produce 0 < distancia <= umbral")


# ---------------------------------------------------------------------------
# FR-003 · frames idénticos -> 1 representativo
# ---------------------------------------------------------------------------


def test_dedupe_identical_frames_keep_one_representative(tmp_path: Path) -> None:
    """FR-003: N copias idénticas -> 1 representativo (el primero en el tiempo)."""
    image = _synthetic_image(1)
    frames = tuple(_write_frame(tmp_path, "f", i * 100, image) for i in range(5))

    kept = dedupe_frames(frames)

    assert len(kept) == 1
    assert kept[0].timestamp_ms == 0


def test_dedupe_near_identical_encodings_collapse_with_default_threshold(
    tmp_path: Path,
) -> None:
    """FR-003/ADR-0005: recompresión/resize (distancia 0) colapsan por defecto."""
    base = _synthetic_image(3)
    variants = [
        base,
        _reencode(base, "JPEG", quality=85),
        base.resize((320, 240), Image.Resampling.LANCZOS),
        _reencode(base.resize((320, 240), Image.Resampling.LANCZOS), "JPEG", quality=80),
    ]
    # Precondición del escenario: variantes near-exact (distancia <= umbral, ADR-0005).
    assert all(_dist(base, v) <= DEFAULT_HAMMING_THRESHOLD for v in variants)
    frames = tuple(
        _write_frame(tmp_path, "f", i * 100, variant) for i, variant in enumerate(variants)
    )

    kept = dedupe_frames(frames)

    assert len(kept) == 1


# ---------------------------------------------------------------------------
# FR-003 · umbral configurable respeta la variación
# ---------------------------------------------------------------------------


def test_dedupe_threshold_boundary_respects_variation(tmp_path: Path) -> None:
    """FR-003: con distancia d real, umbral < d conserva ambos; umbral == d colapsa.

    La variante (crop del 2%) tiene una distancia pequeña pero > 0 medida con
    la API pública de PR-004: el umbral decide exactamente en ese límite.
    """
    base = _synthetic_image(5)
    variant = base.crop((int(_WIDTH * 0.02), int(_HEIGHT * 0.02), _WIDTH, _HEIGHT))
    distance = _dist(base, variant)
    assert 0 < distance < PHASH_BITS, f"variante inesperada: distancia {distance}"

    frames = (
        _write_frame(tmp_path, "f", 0, base),
        _write_frame(tmp_path, "f", 100, variant),
    )

    below = dedupe_frames(frames, threshold=distance - 1)
    assert len(below) == 2, "umbral por debajo de la distancia no debe deduplicar"

    at = dedupe_frames(frames, threshold=distance)
    assert len(at) == 1, "umbral == distancia debe colapsar la variante"
    assert at[0].timestamp_ms == 0, "se conserva el representativo (el primero)"


def test_dedupe_default_threshold_keeps_distinct_frames(tmp_path: Path) -> None:
    """FR-003: frames claramente distintos (distancia > umbral) se conservan todos."""
    images = [_synthetic_image(seed) for seed in (1, 2, 3)]
    # Precondición: todas las distancias superan el umbral por defecto (ADR-0005).
    assert all(_dist(images[0], other) > DEFAULT_HAMMING_THRESHOLD for other in images[1:])
    frames = tuple(_write_frame(tmp_path, "f", i * 100, image) for i, image in enumerate(images))

    kept = dedupe_frames(frames)

    assert [f.timestamp_ms for f in kept] == [0, 100, 200]


def test_dedupe_threshold_zero_keeps_slightly_varying_frames(tmp_path: Path) -> None:
    """FR-003: umbral 0 solo elimina duplicados exactos; la variación se conserva."""
    base = _synthetic_image(7)
    variant, distance = _near_variant(base)
    assert distance > 0
    frames = (
        _write_frame(tmp_path, "f", 0, base),
        _write_frame(tmp_path, "f", 100, variant),
    )

    kept = dedupe_frames(frames, threshold=0)

    assert len(kept) == 2


# ---------------------------------------------------------------------------
# FR-003 · representatividad, orden y determinismo
# ---------------------------------------------------------------------------


def test_dedupe_drops_frames_near_an_earlier_representative(tmp_path: Path) -> None:
    """FR-003: un frame casi idéntico a un representativo previo se descarta.

    El representativo conservado es el primero del grupo; los frames
    posteriores distintos se conservan.
    """
    first = _synthetic_image(9)
    near_first = first.crop((int(_WIDTH * 0.02), int(_HEIGHT * 0.02), _WIDTH, _HEIGHT))
    distinct = _synthetic_image(10)
    frames = (
        _write_frame(tmp_path, "f", 0, first),
        _write_frame(tmp_path, "f", 100, near_first),
        _write_frame(tmp_path, "f", 200, distinct),
    )

    kept = dedupe_frames(frames)

    assert [f.timestamp_ms for f in kept] == [0, 200]


def test_dedupe_preserves_input_order_and_is_a_subset(tmp_path: Path) -> None:
    """FR-003: el resultado preserva el orden temporal y es subconjunto del input."""
    frames = (
        _write_frame(tmp_path, "f", 0, _synthetic_image(1)),
        _write_frame(tmp_path, "f", 100, _synthetic_image(1)),
        _write_frame(tmp_path, "f", 200, _synthetic_image(2)),
        _write_frame(tmp_path, "f", 300, _synthetic_image(2)),
        _write_frame(tmp_path, "f", 400, _synthetic_image(3)),
    )

    kept = dedupe_frames(frames)

    assert set(kept) <= set(frames)
    assert [f.timestamp_ms for f in kept] == sorted(f.timestamp_ms for f in kept)


def test_dedupe_is_deterministic(tmp_path: Path) -> None:
    """FR-003 (SC-007 mindset): mismo input -> mismo output en ejecuciones repetidas."""
    frames = (
        _write_frame(tmp_path, "f", 0, _synthetic_image(1)),
        _write_frame(tmp_path, "f", 100, _synthetic_image(2)),
        _write_frame(tmp_path, "f", 200, _synthetic_image(1)),
    )

    first_run = dedupe_frames(frames)
    second_run = dedupe_frames(frames)

    assert first_run == second_run
    assert [f.timestamp_ms for f in first_run] == [f.timestamp_ms for f in second_run]


def test_dedupe_output_pairs_all_exceed_threshold(tmp_path: Path) -> None:
    """FR-003: invariante — ningún par del resultado es near-duplicate (dist > umbral)."""
    images = [_synthetic_image(seed) for seed in (1, 2, 3, 5, 9)]
    frames = tuple(_write_frame(tmp_path, "f", i * 100, image) for i, image in enumerate(images))

    kept = dedupe_frames(frames, threshold=6)

    kept_hashes = [compute_phash(Image.open(f.path)) for f in kept]
    for i, a in enumerate(kept_hashes):
        for b in kept_hashes[i + 1 :]:
            assert hamming_distance(a, b) > 6


# ---------------------------------------------------------------------------
# Edge cases (spec §Edge Cases) y errores controlados
# ---------------------------------------------------------------------------


def test_dedupe_empty_and_single_frame(tmp_path: Path) -> None:
    """Edge case: input vacío -> vacío; un único frame se conserva siempre."""
    assert dedupe_frames(()) == ()

    single = _write_frame(tmp_path, "f", 0, _synthetic_image(1))
    assert dedupe_frames((single,)) == (single,)


def test_dedupe_aggressive_threshold_collapses_all_frames(tmp_path: Path) -> None:
    """Edge case (spec): vídeo donde todos los frames son casi idénticos -> 1 frame."""
    frames = tuple(
        _write_frame(tmp_path, "f", i * 100, _synthetic_image(seed))
        for i, seed in enumerate((1, 2, 3, 5, 9))
    )

    kept = dedupe_frames(frames, threshold=PHASH_BITS)

    assert len(kept) == 1
    assert kept[0].timestamp_ms == 0


def test_dedupe_invalid_threshold_raises_value_error(tmp_path: Path) -> None:
    """FR-003: umbral fuera de [0, PHASH_BITS] -> ValueError (config inválida)."""
    frame = _write_frame(tmp_path, "f", 0, _synthetic_image(1))
    with pytest.raises(ValueError, match="threshold"):
        dedupe_frames((frame,), threshold=-1)
    with pytest.raises(ValueError, match="threshold"):
        dedupe_frames((frame,), threshold=PHASH_BITS + 1)


def test_dedupe_unreadable_frame_raises_controlled_error(tmp_path: Path) -> None:
    """FR-003: frame ilegible -> DedupeError (error controlado, jerarquía IngestError)."""
    frame = _write_frame(tmp_path, "f", 0, _synthetic_image(1))
    frame.path.write_bytes(b"not a real png image")

    with pytest.raises(DedupeError, match="no se pudo leer"):
        dedupe_frames((frame,))


def test_dedupe_errors_are_controlled() -> None:
    """FR-001/FR-003: los errores de dedupe heredan de IngestError (pipeline continúa)."""
    assert issubclass(DedupeError, IngestError)
