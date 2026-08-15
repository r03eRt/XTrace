"""Tests del FakeEmbeddingProvider (PR-002 · FR-005 · ADR-0007 · contracts §3).

Criterios verificables (contrato §3):
- `embed_images(images) -> np.ndarray` con shape (N, D), donde D = `dimension`.
- Vectores L2-normalizados (norma unitaria por fila).
- Determinismo: misma entrada -> mismo vector, en cualquier instancia/llamada.
- `dimension` y `model_id` configurables; contrato expuesto como `EmbeddingProvider`.
"""

import numpy as np
import pytest
from PIL import Image

from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.embeddings.provider import EmbeddingProvider

DEFAULT_DIMENSION = 512


def _solid_image(color: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> Image.Image:
    """Imagen sólida determinista (RGB)."""
    return Image.new("RGB", size, color)


def _gradient_image(size: tuple[int, int] = (32, 32)) -> Image.Image:
    """Gradiente determinista (RGB): varía rojo y azul por fila."""
    return Image.fromarray(
        np.array(
            [
                [(r, 128, (r * 255) // (size[1] - 1)) for r in range(size[0])]
                for _ in range(size[1])
            ],
            dtype=np.uint8,
        ),
        mode="RGB",
    )


def test_provider_contract_exposed() -> None:
    """El fake cumple el contrato `EmbeddingProvider` (contracts §3, FR-005)."""
    provider = FakeEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)
    assert isinstance(provider.model_id, str)
    assert isinstance(provider.dimension, int)
    assert provider.model_id == "fake-deterministic-hash"
    assert provider.dimension == DEFAULT_DIMENSION


def test_embed_images_shape() -> None:
    """N imágenes -> array (N, D) (contracts §3: shape (N, D))."""
    provider = FakeEmbeddingProvider(dimension=DEFAULT_DIMENSION)
    images = [_solid_image((255, 0, 0)), _solid_image((0, 255, 0)), _solid_image((0, 0, 255))]
    out = provider.embed_images(images)
    assert out.shape == (len(images), DEFAULT_DIMENSION)
    assert out.dtype == np.float32


def test_embed_images_l2_normalized() -> None:
    """Cada fila tiene norma L2 unitaria (contracts §3: L2-normalized)."""
    provider = FakeEmbeddingProvider(dimension=DEFAULT_DIMENSION)
    images = [_solid_image((10, 20, 30)), _gradient_image(), _solid_image((1, 1, 1), (16, 16))]
    out = provider.embed_images(images)
    norms = np.linalg.norm(out, axis=1)
    assert norms.shape == (len(images),)
    np.testing.assert_allclose(norms, 1.0, rtol=1e-6)


def test_embed_images_deterministic() -> None:
    """Misma entrada -> mismo resultado entre llamadas e instancias (FR-005: determinismo)."""
    images = [_gradient_image(), _solid_image((200, 100, 50)), _solid_image((7, 7, 7), (8, 8))]
    first = FakeEmbeddingProvider(dimension=DEFAULT_DIMENSION).embed_images(images)
    second = FakeEmbeddingProvider(dimension=DEFAULT_DIMENSION).embed_images(images)
    assert np.array_equal(first, second)


def test_different_images_produce_different_vectors() -> None:
    """Imágenes distintas -> vectores distintos (hash sensible al contenido)."""
    provider = FakeEmbeddingProvider(dimension=DEFAULT_DIMENSION)
    red = provider.embed_images([_solid_image((255, 0, 0))])
    blue = provider.embed_images([_solid_image((0, 0, 255))])
    assert not np.array_equal(red, blue)


def test_dimension_configurable() -> None:
    """`dimension` configurable afecta a la forma (contracts §3: dimension)."""
    for dimension in (1, 8, 64, 1024):
        provider = FakeEmbeddingProvider(dimension=dimension)
        out = provider.embed_images([_solid_image((1, 2, 3)), _gradient_image((16, 16))])
        assert out.shape == (2, dimension)
        np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, rtol=1e-6)


def test_model_id_configurable() -> None:
    """`model_id` configurable y expuesto en el provider (contracts §3)."""
    provider = FakeEmbeddingProvider(model_id="fake-test-model")
    assert provider.model_id == "fake-test-model"


def test_empty_batch_returns_empty_array() -> None:
    """Lote vacío -> shape (0, D) sin errores (contrato: N = len(images))."""
    provider = FakeEmbeddingProvider(dimension=DEFAULT_DIMENSION)
    out = provider.embed_images([])
    assert out.shape == (0, DEFAULT_DIMENSION)
    assert out.dtype == np.float32


def test_invalid_dimension_rejected() -> None:
    """Dimensiones no válidas se rechazan en la construcción."""
    with pytest.raises(ValueError):
        FakeEmbeddingProvider(dimension=0)
    with pytest.raises(ValueError):
        FakeEmbeddingProvider(dimension=-4)
