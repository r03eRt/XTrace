"""Deduplicación de frames por pHash (PR-009 · FR-003 · ADR-0005).

Reduce el conjunto de frames representativos extraídos (PR-008) eliminando
los casi idénticos: dos frames cuya distancia de Hamming de pHash es
<= umbral se consideran duplicados y solo se conserva el primero de cada
grupo (el representativo, FR-003).

Algoritmo: greedy determinista. Se recorren los frames en el orden de
entrada (el de extracción: cronológico, ver ingest.frames) y cada frame se
conserva solo si su distancia de Hamming a **todos** los representativos ya
conservados supera el umbral. Invariante del resultado: ningún par de frames
conservados es near-duplicate (distancia > umbral). Complejidad O(N x K)
con K <= N (frames por vídeo, típicamente 30).

Reutiliza hashing.phash (PR-004): compute_phash para la firma y
hamming_distance para la comparación. No añade dependencias.

Errores controlados: un frame ilegible (fichero desaparecido o corrupto) se
propaga como DedupeError (subclase de IngestError) para que el pipeline
pueda marcar el vídeo como failed sin abortar el resto del dataset
(FR-001, acceptance scenario 3 de US1). Configuración inválida
(threshold fuera de [0, PHASH_BITS]) -> ValueError antes de leer imágenes.
"""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image

from xtrace_spike.hashing.phash import PHASH_BITS, compute_phash, hamming_distance
from xtrace_spike.ingest import IngestError
from xtrace_spike.ingest.frames import ExtractedFrame

DEFAULT_HAMMING_THRESHOLD: int = 10


class DedupeError(IngestError):
    """Error controlado del dedupe: un frame no se pudo leer para su pHash."""


def dedupe_frames(
    frames: Sequence[ExtractedFrame],
    *,
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
) -> tuple[ExtractedFrame, ...]:
    """Devuelve el subconjunto representativo de frames deduplicados (FR-003).

    Un frame se conserva si su distancia de Hamming a todos los
    representativos ya conservados es mayor que threshold; en caso
    contrario se descarta. El resultado preserva el orden del input y es
    determinista (mismo input -> mismo output, SC-007 mindset).

    Args:
        frames: frames representativos extraídos (orden temporal de
            ingest.frames, p. ej. ExtractionResult.frames).
        threshold: distancia de Hamming máxima considerada duplicado; en
            [0, PHASH_BITS]. 0 = solo duplicados exactos; valores altos =
            dedupe agresivo (edge case spec: vídeo casi idéntico -> 1 frame).

    Returns:
        Tupla de frames representativos, subconjunto del input en el mismo
        orden, con la garantía de que ningún par tiene distancia <= threshold.

    Raises:
        ValueError: si threshold está fuera de [0, PHASH_BITS].
        DedupeError: si un frame no puede leerse (archivo ilegible/corrupto).
    """
    if not 0 <= threshold <= PHASH_BITS:
        raise ValueError(f"threshold debe estar en [0, {PHASH_BITS}] (recibido {threshold})")

    kept: list[ExtractedFrame] = []
    kept_hashes: list[int] = []
    for frame in frames:
        frame_hash = _frame_phash(frame)
        if all(hamming_distance(frame_hash, kept_hash) > threshold for kept_hash in kept_hashes):
            kept.append(frame)
            kept_hashes.append(frame_hash)
    return tuple(kept)


def _frame_phash(frame: ExtractedFrame) -> int:
    """pHash del frame leyendo su imagen desde disco (reutiliza PR-004)."""
    try:
        with Image.open(frame.path) as image:
            return compute_phash(image)
    except OSError as exc:
        raise DedupeError(f"no se pudo leer el frame '{frame.path}' para el dedupe: {exc}") from exc
