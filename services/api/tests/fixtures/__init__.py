"""Fixtures sintéticos de los tests del servicio API (PR-055 · SC-007 mindset).

Imágenes PNG deterministas generadas con PIL (mismos parámetros → mismos
píxeles → mismo embedding del `FakeEmbeddingProvider` y mismo pHash), sin
contenido real ni aleatoriedad: el contenido real nunca se commitea (spec
003: solo fixtures sintéticos permitidos, paridad con fases 1-2).

Helpers de siembra del índice in-memory (paridad con los tests de la CLI del
spike: backend compartido por proceso vía `build_backend`).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from PIL import Image
from xtrace_spike.cli import build_backend  # type: ignore[import-untyped]
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider  # type: ignore[import-untyped]
from xtrace_spike.hashing.phash import compute_phash  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.base import FrameRecord  # type: ignore[import-untyped]

#: Dimensión del provider fake por defecto de la CLI/API (D fijada por PR-005).
EMBEDDING_DIMENSION = 768

#: Firma PNG válida (para media corrupta con firma real: 400 media_corrupt).
PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


def make_query_image(path: Path, *, size: tuple[int, int] = (64, 48)) -> Path:
    """PNG determinista (sin aleatoriedad): mismos parámetros → mismos bytes.

    Distintos tamaños producen píxeles distintos → embeddings/pHashes
    distintos con el provider fake (consultas representativas de paridad).
    """
    width, height = size
    image = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            image.putpixel((x, y), (x * 4 % 256, y * 4 % 256, (x + y) % 256))
    image.save(path, format="PNG")
    return path


def make_bogus_file(path: Path, *, with_png_signature: bool = False) -> Path:
    """Fichero sin firma de imagen (415) o con firma PNG + contenido ilegible (400).

    - `with_png_signature=False`: payload sin firma MIME reconocida.
    - `with_png_signature=True`: firma PNG válida seguida de basura → pasa la
      validación de firma pero falla la decodificación PIL (media_corrupt).
    """
    if with_png_signature:
        path.write_bytes(PNG_SIGNATURE + b"garbage garbage garbage garbage")
    else:
        path.write_bytes(b"XTrace fake media - not really an image" + bytes(range(16)))
    return path


def rgb_of(path: Path) -> Image.Image:
    """Carga la imagen en memoria y la normaliza a RGB (paridad con la búsqueda)."""
    with Image.open(path) as image:
        image.load()
        return image.convert("RGB")


def embedding_of(path: Path) -> list[float]:
    """Embedding determinista de la imagen (mismo proveedor que CLI/API)."""
    vector = FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION).embed_images([rgb_of(path)])[0]
    return [float(value) for value in vector]


def seed_in_memory_index(
    image: Path,
    *,
    video_id: str | None = None,
    timestamp_ms: int | None = 94_000,
) -> tuple[str, str]:
    """Siembra el índice in-memory compartido con un frame idéntico a la imagen.

    Usa los mismos componentes que la cadena real (Fake D=768 + compute_phash
    + `build_backend` del spike): la búsqueda de esa imagen encuentra el frame
    con distancia 0.0, match_score 1.0 y evidencia pHash 1.0 (paridad con los
    tests de la CLI). Devuelve (video_id, frame_id).
    """
    backend = build_backend()
    resolved_video = video_id or str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    record = FrameRecord(
        frame_id=frame_id,
        video_id=resolved_video,
        timestamp_ms=timestamp_ms,
        phash=compute_phash(rgb_of(image)),
        embedding=embedding_of(image),
    )
    asyncio.run(backend.store.upsert_frames([record]))
    return resolved_video, frame_id
