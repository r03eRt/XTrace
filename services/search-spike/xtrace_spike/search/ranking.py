"""Ranking configurable de candidatos por vídeo (PR-013 · FR-012/013/014 · SC-002
· ADR-0005 · contracts §4).

Combina, con **pesos configurables** (FR-013), tres señales por candidato:

- **Similitud visual**: `1 − best_distance` (distancia coseno del mejor
  frame, contracts §2/§5: menor = más similar). Señal principal del ANN.
- **Nº de frames coincidentes**: `matching_frames` normalizado por el máximo
  entre los candidatos **no excluidos** (evidencia de apoyo; escala invariante
  a `top_k`).
- **Evidencia pHash** (ADR-0005): `1 − Hamming(query_phash, phash del mejor
  frame) / 64`. Menor distancia de Hamming = mejor (0 → near-exact).

El score final es la media ponderada (los pesos se normalizan a suma 1):

    match_score = (w_v·s_visual + w_f·s_frames + w_p·s_phash) / (w_v + w_f + w_p)

normalizado en [0, 1] (FR-012) y acompañado de `match_timestamp_ms` (el
timestamp del mejor frame; None si no existe, FR-012). Un umbral
`min_score` descarta resultados débiles (SC-002: las consultas negativas
quedan por debajo). Los vídeos excluidos se ignoran (FR-014) vía el filtro
`excluded_videos` proporcionado por el llamador.

**Acceso al pHash de los frames (decisión de API)**: el ranking es una
función pura sin DB; la evidencia pHash necesita el pHash del mejor frame de
cada candidato, que el llamador entrega como mapeo `frame_id -> phash`
(pHash de 64 bits SIN signo, salida de `compute_phash`). En producción el
llamador lo obtiene con `PgRepo.get_frame_phashes` (repo.py, PR-013), que
decodifica la columna bigint de `frames.phash`; un frame ausente del mapeo
contribuye 0 a la evidencia (sin penalizar el resto de señales).

La consistencia temporal (clips) queda diferida (Decisión D1, FR-011): el
diseño no lo impide (una señal temporal sería un cuarto término ponderado).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from xtrace_spike.hashing.phash import PHASH_BITS, hamming_distance
from xtrace_spike.search.image_search import ImageSearchResult


@dataclass(frozen=True)
class RankingWeights:
    """Pesos configurables del ranking (FR-013 · ADR-0005 · contracts §4).

    Pesos relativos y no negativos: se normalizan internamente a suma 1 para
    que `match_score` quede en [0, 1] (FR-012). Un peso a 0 desactiva su
    señal; un peso a 1 aísla la señal como único criterio (tests).
    """

    visual: float = 0.5
    frames: float = 0.3
    phash: float = 0.2

    def __post_init__(self) -> None:
        if self.visual < 0 or self.frames < 0 or self.phash < 0:
            raise ValueError(
                f"Los pesos del ranking deben ser >= 0 "
                f"(visual={self.visual}, frames={self.frames}, phash={self.phash})"
            )
        if self.visual + self.frames + self.phash <= 0:
            raise ValueError(
                f"La suma de los pesos del ranking debe ser > 0 "
                f"(visual={self.visual}, frames={self.frames}, phash={self.phash})"
            )


#: Pesos por defecto del spike (punto de partida de PR-013; el benchmark
#: PR-016 los calibrará contra SC-001/SC-002). La visual (ANN) es la señal
#: principal; frames y pHash son evidencia de apoyo (ADR-0005).
DEFAULT_WEIGHTS = RankingWeights(visual=0.5, frames=0.3, phash=0.2)

#: Umbral de match por defecto: 0.0 = sin rechazo (el ranking solo ordena).
#: El rechazo de consultas débiles (SC-002) lo configura el llamador
#: (CLI search de PR-014 / benchmark PR-016).
DEFAULT_MIN_SCORE = 0.0


@dataclass(frozen=True)
class RankedVideo:
    """Vídeo rankeado (FR-012 · contracts §1/§4).

    Atributos:
        video_id: vídeo candidato.
        match_score: score normalizado en [0, 1] (FR-012).
        match_timestamp_ms: timestamp del mejor frame coincidente; None si el
            frame no tiene timestamp (FR-012).
        matching_frames: nº de frames coincidentes del vídeo (evidencia).
        best_frame_id: id del mejor frame (el que fija distancia/timestamp).
        best_distance: distancia coseno del mejor frame (menor = más similar).
        visual_similarity: señal visual (1 − best_distance), en [0, 1].
        frames_score: señal de nº de frames normalizada, en [0, 1].
        phash_score: señal de evidencia pHash (1 − Hamming/64), en [0, 1];
            0.0 si el pHash del mejor frame no está disponible.
    """

    video_id: str
    match_score: float
    match_timestamp_ms: int | None
    matching_frames: int
    best_frame_id: str
    best_distance: float
    visual_similarity: float
    frames_score: float
    phash_score: float


def _clamp01(value: float) -> float:
    """Recorta a [0, 1] (defensivo; el contrato ya garantiza el rango)."""
    return max(0.0, min(1.0, value))


def rank_candidates(
    result: ImageSearchResult,
    *,
    frame_phashes: Mapping[str, int],
    weights: RankingWeights = DEFAULT_WEIGHTS,
    excluded_videos: Collection[str] = (),
    min_score: float = DEFAULT_MIN_SCORE,
) -> tuple[RankedVideo, ...]:
    """Rankea los candidatos de una búsqueda por imagen (FR-012/013/014).

    Función pura (sin DB ni torch): consume el `ImageSearchResult` de
    PR-012 (query_phash + candidates) y produce un `RankedVideo` por vídeo
    no excluido, ordenado por `match_score` descendente (desempates:
    mejor distancia, luego video_id — orden estable y determinista).

    Args:
        result: resultado de `ImageSearch.search_image` (PR-012).
        frame_phashes: mapeo frame_id -> pHash sin signo de 64 bits de los
            frames candidatos (ver módulo docstring; producción:
            `PgRepo.get_frame_phashes`). Un frame ausente contribuye 0.
        weights: pesos configurables de las tres señales (FR-013).
        excluded_videos: video_ids excluidos (FR-014); se ignoran aunque
            hubieran ganado. Mecanismo complementario al filtro por defecto
            de `VectorStore.ann_search` (exclude_videos=True, PR-007).
        min_score: umbral de match en [0, 1]; los resultados con
            `match_score < min_score` se descartan (SC-002).

    Raises:
        ValueError: si `min_score` está fuera de [0, 1] o `weights` no es
            válido (validado en el propio `RankingWeights`).

    Returns:
        Tupla de `RankedVideo` ordenada por `match_score` descendente
        (vacía si no quedan candidatos o todos quedan bajo `min_score`).
    """
    if not 0.0 <= min_score <= 1.0:
        raise ValueError(f"min_score debe estar en [0, 1] (recibido {min_score})")

    excluded = set(excluded_videos)
    candidates = [
        candidate for candidate in result.candidates if candidate.video_id not in excluded
    ]
    if not candidates:
        return ()

    # Normalización de la señal de frames: máximo entre los no excluidos
    # (todo candidato tiene >= 1 frame coincidente por construcción, PR-012).
    max_frames = max(candidate.matching_frames for candidate in candidates)
    weight_sum = weights.visual + weights.frames + weights.phash

    ranked: list[RankedVideo] = []
    for candidate in candidates:
        visual_similarity = _clamp01(1.0 - candidate.best_distance)
        frames_score = candidate.matching_frames / max_frames
        frame_phash = frame_phashes.get(candidate.best_frame_id)
        if frame_phash is None:
            phash_score = 0.0  # evidencia no disponible: contribución neutra
        else:
            phash_score = _clamp01(
                1.0 - hamming_distance(result.query_phash, frame_phash) / PHASH_BITS
            )
        match_score = (
            weights.visual * visual_similarity
            + weights.frames * frames_score
            + weights.phash * phash_score
        ) / weight_sum
        ranked.append(
            RankedVideo(
                video_id=candidate.video_id,
                match_score=match_score,
                match_timestamp_ms=candidate.best_timestamp_ms,
                matching_frames=candidate.matching_frames,
                best_frame_id=candidate.best_frame_id,
                best_distance=candidate.best_distance,
                visual_similarity=visual_similarity,
                frames_score=frames_score,
                phash_score=phash_score,
            )
        )

    ranked.sort(key=lambda item: (-item.match_score, item.best_distance, item.video_id))
    return tuple(item for item in ranked if item.match_score >= min_score)
