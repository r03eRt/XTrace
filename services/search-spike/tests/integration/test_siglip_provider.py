"""Tests de integración del SiglipLocalProvider (PR-005 · FR-005 · ADR-0005 · contracts §3).

Valida el proveedor **real** SigLIP v1 (open_clip + torch, CPU local): contrato
`EmbeddingProvider` (contracts §3), shape (N, D) con **D = 768** fijada por
PR-005, normalización L2, batching y un smoke de precision/recall aproximado +
throughput (frames/s) sobre imágenes sintéticas deterministas.

Marcados `@slow` (opcional en CI, PR-005): se saltan si torch no está
instalado (`pytest.importorskip("torch")` — extra `siglip` de pyproject).
La carga del modelo es una vez por sesión (fixture de módulo).
"""

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

torch = pytest.importorskip("torch")  # noqa: F841  (extra siglip ausente -> módulo skip)

from xtrace_spike.embeddings.provider import EmbeddingProvider  # noqa: E402
from xtrace_spike.embeddings.siglip_local import SiglipLocalProvider  # noqa: E402

pytestmark = pytest.mark.slow

# D fijada por PR-005 (ViT-B-16-SigLIP, ADR-0005): la usará el esquema DB (PR-006).
FIXED_DIMENSION = 768


# ---------------------------------------------------------------------------
# Imágenes sintéticas deterministas (fixtures de datos; sin binarios en repo)
# ---------------------------------------------------------------------------


def _make_images(n: int, size: int = 96, seed: int = 0) -> list[Image.Image]:
    """n imágenes sintéticas deterministas con estructura espacial variada.

    Un único rng por llamada (estado secuencial) y todos los tipos consumen
    rng: cada una de las n imágenes es **única** (sin duplicados exactos),
    invariante que los tests de recall asumen sobre el índice (el generador
    anterior recreaba el rng por imagen y dejaba gradiente/tablero fijos,
    colapsando el índice a 4 imágenes repetidas — PR-005).
    """
    rng = np.random.default_rng(seed)
    images: list[Image.Image] = []
    for i in range(n):
        kind = i % 4
        if kind == 0:  # ruido suave (rng secuencial -> única por i)
            arr = rng.integers(0, 255, (size, size, 3), dtype=np.uint8)
            images.append(Image.fromarray(arr, "RGB").filter(ImageFilter.GaussianBlur(1)))
        elif kind == 1:  # gradiente con desplazamiento y ganancia por imagen
            g = np.linspace(0, 255, size, dtype=np.uint8)
            shift = int(rng.integers(0, size))
            gain = int(rng.integers(60, 220))
            arr = np.stack([np.roll(g, shift), 255 - g, (g * gain) // 255], axis=-1)
            images.append(Image.fromarray(np.tile(arr[:, None, :], (1, size, 1)), "RGB"))
        elif kind == 2:  # tablero de ajedrez con bloque/colores por imagen
            block = int(rng.integers(8, 24))
            c1 = rng.integers(40, 255, 3, dtype=np.uint8)
            c2 = rng.integers(0, 120, 3, dtype=np.uint8)
            c = (np.indices((size, size)).sum(axis=0) // block) % 2
            arr = (
                c[:, :, None].astype(np.int16) * c1 + (1 - c[:, :, None]).astype(np.int16) * c2
            ).astype(np.uint8)
            images.append(Image.fromarray(arr, "RGB"))
        else:  # formas geométricas sobre fondo (rng secuencial -> única por i)
            img = Image.new("RGB", (size, size), (30, 30, 30))
            draw = ImageDraw.Draw(img)
            for _ in range(5):
                x0, y0, x1, y1 = [int(v) for v in rng.integers(0, size, 4)]
                draw.rectangle(
                    (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                    outline=tuple(int(v) for v in rng.integers(0, 255, 3)),
                    width=3,
                )
            images.append(img)
    return images


def _crop_query(image: Image.Image, size: int = 96) -> Image.Image:
    return image.crop((12, 12, size - 12, size - 12)).resize((size, size))


def _jpeg_query(image: Image.Image) -> Image.Image:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=60)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _resize_query(image: Image.Image, size: int = 96) -> Image.Image:
    return image.resize((size // 2, size // 2)).resize((size, size))


def _color_query(image: Image.Image) -> Image.Image:
    arr = np.asarray(image).astype(np.int16)
    arr = np.clip(arr * np.array([1.1, 0.9, 0.8]), 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


# ---------------------------------------------------------------------------
# Fixture del proveedor (una carga de modelo por módulo)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def provider() -> SiglipLocalProvider:
    return SiglipLocalProvider(model_name="ViT-B-16-SigLIP", pretrained="webli")


# ---------------------------------------------------------------------------
# Contrato (contracts §3) y dimensión D fijada
# ---------------------------------------------------------------------------


def test_contract_exposed(provider: SiglipLocalProvider) -> None:
    """Cumple el contrato EmbeddingProvider (FR-005 · ADR-0007 · contracts §3)."""
    assert isinstance(provider, EmbeddingProvider)
    assert isinstance(provider.model_id, str)
    assert provider.model_id.startswith("openclip-ViT-B-16-SigLIP")


def test_dimension_fixed_768(provider: SiglipLocalProvider) -> None:
    """PR-005 fija D=768 (ViT-B-16-SigLIP) — decisión documentada en ADR-0005."""
    assert provider.dimension == FIXED_DIMENSION


def test_embed_images_shape_dtype_and_l2(provider: SiglipLocalProvider) -> None:
    """N imágenes -> (N, 768) float32 con filas L2-normalizadas (contracts §3)."""
    images = _make_images(4, seed=11)
    out = provider.embed_images(images)
    assert out.shape == (4, FIXED_DIMENSION)
    assert out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, rtol=1e-5)


def test_batch_splitting_consistent(provider: SiglipLocalProvider) -> None:
    """El sub-batching no cambia el resultado (FR-005: procesado en batches)."""
    images = _make_images(5, seed=23)
    full = SiglipLocalProvider(batch_size=64).embed_images(images)
    split = SiglipLocalProvider(batch_size=2).embed_images(images)
    assert full.shape == split.shape == (5, FIXED_DIMENSION)
    np.testing.assert_allclose(full, split, rtol=1e-5, atol=1e-6)


def test_empty_batch(provider: SiglipLocalProvider) -> None:
    """Lote vacío -> shape (0, D) sin errores (contrato: N = len(images))."""
    out = provider.embed_images([])
    assert out.shape == (0, FIXED_DIMENSION)
    assert out.dtype == np.float32


def test_deterministic_across_calls(provider: SiglipLocalProvider) -> None:
    """Misma entrada -> mismos vectores entre llamadas (evaluación sin dropout)."""
    images = _make_images(3, seed=31)
    first = provider.embed_images(images)
    second = provider.embed_images(images)
    np.testing.assert_allclose(first, second, rtol=1e-5, atol=1e-6)


# ---------------------------------------------------------------------------
# Smoke: precision/recall aproximados + throughput (mini-benchmark PR-005)
# ---------------------------------------------------------------------------


def test_smoke_exact_queries_recall_top1(provider: SiglipLocalProvider) -> None:
    """Consulta exacta de un frame indexado -> su vector es el vecino más próximo.

    Es la base del pipeline de búsqueda (FR-010); con el mismo píxel el
    embedding debe ser (casi) idéntico: recall@1 = 1.0.
    """
    index_images = _make_images(12, seed=41)
    index = provider.embed_images(index_images)
    query_indices = [0, 3, 7, 11]
    queries = provider.embed_images([index_images[i] for i in query_indices])
    for q_idx, row in zip(query_indices, queries, strict=True):
        sims = row @ index.T
        assert int(np.argmax(sims)) == q_idx, (
            f"exact query {q_idx} matched a different frame (top={int(np.argmax(sims))})"
        )


def test_smoke_variant_recall(provider: SiglipLocalProvider) -> None:
    """Variantes (resize/jpeg/crop/color) recuperan el frame correcto en Top-1.

    Precision/recall aproximados del mini-benchmark de PR-005 (ADR-0005):
    SigLIP debe ser robusto a transformaciones leves. El umbral es
    conservador; las métricas reales se reportan en el handoff PR-005.
    """
    index_images = _make_images(16, seed=53)
    index = provider.embed_images(index_images)
    variants: dict[str, list[Image.Image]] = {
        "resize": [_resize_query(img) for img in index_images],
        "jpeg": [_jpeg_query(img) for img in index_images],
        "crop": [_crop_query(img) for img in index_images],
        "color": [_color_query(img) for img in index_images],
    }
    report: list[str] = []
    for name, queries in variants.items():
        out = provider.embed_images(queries)
        correct = sum(int(np.argmax(row @ index.T) == i) for i, row in enumerate(out))
        recall = correct / len(index_images)
        report.append(f"{name}: recall@1={recall:.2f} ({correct}/{len(index_images)})")
        assert recall >= 0.5, f"{name}: recall@1={recall:.2f} por debajo del umbral 0.5"
    print("variant recall:", ", ".join(report))


def test_smoke_negatives_not_confident(provider: SiglipLocalProvider) -> None:
    """Imágenes de otra distribución no superan la confianza de un match real.

    SC-002 (aproximado): el mejor match de una negativa queda por debajo de
    la similitud mínima entre consultas exactas y su frame — umbral de
    match configurable en el ranking (FR-013).
    """
    index_images = _make_images(8, seed=61)
    index = provider.embed_images(index_images)
    true_sims = np.array(
        [provider.embed_images([img])[0] @ index[i] for i, img in enumerate(index_images)]
    )
    negatives = provider.embed_images(_make_images(4, size=96, seed=99))
    neg_max = float((negatives @ index.T).max())
    true_min = float(true_sims.min())
    print(f"negatives: max_sim={neg_max:.4f}, true-match min_sim={true_min:.4f}")
    assert neg_max < true_min, (
        f"una negativa ({neg_max:.4f}) supera la confianza mínima de un match real ({true_min:.4f})"
    )


def test_smoke_throughput(provider: SiglipLocalProvider) -> None:
    """Throughput de embedding en CPU (frames/s): sanity de rendimiento FR-005.

    No es un benchmark formal (llega con PR-016); solo detecta regresiones
    catastróficas. El valor medido real se reporta en el handoff PR-005.
    """
    import time

    images = _make_images(8, size=96, seed=71)
    provider.embed_images(images)  # warm-up (preprocess code paths)
    t0 = time.perf_counter()
    provider.embed_images(images)
    elapsed = time.perf_counter() - t0
    fps = len(images) / elapsed
    print(f"throughput: {fps:.1f} frames/s ({len(images)} frames en {elapsed:.2f}s)")
    assert fps >= 1.0, f"throughput inesperadamente bajo: {fps:.2f} frames/s"
