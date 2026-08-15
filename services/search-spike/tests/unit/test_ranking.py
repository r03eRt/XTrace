"""Tests unitarios del ranking configurable (PR-013 · FR-012/013/014 · SC-002
· ADR-0005 · contracts §4).

Criterios verificables (tasks.md PR-013 · spec 001):
- El ranking prioriza el vídeo correcto combinando, con **pesos
  configurables**, similitud visual (1 − distancia coseno), nº de frames
  coincidentes y evidencia pHash (FR-013 · ADR-0005).
- Cada resultado lleva `match_score` normalizado en [0, 1] y
  `match_timestamp_ms` del mejor frame (FR-012).
- Una consulta negativa no supera el umbral de match configurado
  (SC-002): el mejor `match_score` queda por debajo de `min_score`.
- Los vídeos excluidos no aparecen en los resultados (FR-014).

Tests puros en memoria: `rank_candidates` no toca DB ni torch; los
candidatos se construyen sintéticamente (VideoCandidate/ImageSearchResult de
PR-012) y los pHash son enteros deterministas de 64 bits (hashing.phash,
PR-004).
"""

from __future__ import annotations

import pytest

from xtrace_spike.search.image_search import ImageSearchResult, VideoCandidate
from xtrace_spike.search.ranking import (
    DEFAULT_MIN_SCORE,
    DEFAULT_WEIGHTS,
    RankedVideo,
    RankingWeights,
    rank_candidates,
)

#: pHash de la consulta (64 bits, patrón determinista; PR-004).
_QUERY_PHASH = 0xAAAAAAAAAAAAAAAA
#: Máscara de 64 bits para construir pHash a distancia de Hamming `n` bits.
_ALL_ONES = (1 << 64) - 1


def _candidate(
    video_id: str,
    *,
    matching_frames: int,
    best_distance: float,
    best_frame_id: str,
    best_timestamp_ms: int | None,
) -> VideoCandidate:
    """Vídeo candidato sintético (misma forma que group_hits_by_video, PR-012)."""
    return VideoCandidate(
        video_id=video_id,
        matching_frames=matching_frames,
        best_distance=best_distance,
        best_frame_id=best_frame_id,
        best_timestamp_ms=best_timestamp_ms,
    )


def _result(query_phash: int, *candidates: VideoCandidate) -> ImageSearchResult:
    """Resultado de búsqueda sintético con `total_hits` coherente (FR-010)."""
    return ImageSearchResult(
        query_phash=query_phash,
        candidates=candidates,
        total_hits=sum(candidate.matching_frames for candidate in candidates),
    )


def _phashes_at_distance(*distances: int) -> dict[str, int]:
    """frame_id -> pHash con la distancia de Hamming pedida frente a la consulta.

    `phash = _QUERY_PHASH ^ ((1 << n) - 1)` difiere de la consulta en
    exactamente `n` bits (para n = 64, la inversa completa).
    """
    return {f"frame-{n}": _QUERY_PHASH ^ ((1 << n) - 1) for n in distances}


# ---------------------------------------------------------------------------
# FR-013 · el ranking prioriza el vídeo correcto (pesos por defecto)
# ---------------------------------------------------------------------------


def test_default_weights_prioritize_correct_video() -> None:
    """FR-013/US2 esc. 1: el vídeo correcto (mejor visual+frames+pHash) gana.

    Correcto: distancia 0.0 (similitud visual 1.0), 3 frames, pHash idéntico
    a la consulta (Hamming 0). Señuelo: distancia 0.7, 1 frame, pHash a 48
    bits. Con los pesos por defecto el correcto debe quedar primero con un
    score alto y el señuelo muy por debajo.
    """
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "decoy",
            matching_frames=1,
            best_distance=0.7,
            best_frame_id="frame-48",
            best_timestamp_ms=9_000,
        ),
        _candidate(
            "correct",
            matching_frames=3,
            best_distance=0.0,
            best_frame_id="frame-0",
            best_timestamp_ms=12_000,
        ),
    )
    ranked = rank_candidates(result, frame_phashes=_phashes_at_distance(0, 48))

    assert [item.video_id for item in ranked] == ["correct", "decoy"]
    correct, decoy = ranked
    assert correct.match_score > 0.9  # señales dominantes -> cerca de 1.0
    assert correct.match_timestamp_ms == 12_000
    assert decoy.match_score < correct.match_score


def test_match_score_formula_with_default_weights() -> None:
    """FR-012/FR-013: fórmula del score con los pesos por defecto.

    Señales (un único candidato, frames_score = 1.0 por normalización):
    - visual = 1 − 0.2 = 0.8
    - frames = 4 / 4 = 1.0
    - phash = 1 − 16/64 = 0.75
    score = (0.5·0.8 + 0.3·1.0 + 0.2·0.75) / (0.5 + 0.3 + 0.2) = 0.85
    """
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "video-a",
            matching_frames=4,
            best_distance=0.2,
            best_frame_id="frame-16",
            best_timestamp_ms=5_000,
        ),
    )
    (item,) = rank_candidates(result, frame_phashes=_phashes_at_distance(16))

    assert item.video_id == "video-a"
    assert item.visual_similarity == pytest.approx(0.8)
    assert item.frames_score == pytest.approx(1.0)
    assert item.phash_score == pytest.approx(0.75)
    assert item.match_score == pytest.approx(0.85)
    assert item.match_timestamp_ms == 5_000
    # los campos del candidato viajan al resultado (contracts §1)
    assert item.matching_frames == 4
    assert item.best_frame_id == "frame-16"
    assert item.best_distance == pytest.approx(0.2)
    assert 0.0 <= item.match_score <= 1.0  # FR-012: score normalizado


def test_frames_signal_normalized_by_max_across_candidates() -> None:
    """FR-013: nº de frames normalizado por el máximo entre candidatos."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "few",
            matching_frames=2,
            best_distance=0.1,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "many",
            matching_frames=4,
            best_distance=0.1,
            best_frame_id="frame-0",
            best_timestamp_ms=2_000,
        ),
    )
    ranked = rank_candidates(result, frame_phashes=_phashes_at_distance(0, 0))

    by_id = {item.video_id: item for item in ranked}
    assert by_id["few"].frames_score == pytest.approx(0.5)  # 2/4
    assert by_id["many"].frames_score == pytest.approx(1.0)  # 4/4
    # misma señal visual y pHash -> gana el de más frames
    assert ranked[0].video_id == "many"


# ---------------------------------------------------------------------------
# FR-013 · pesos configurables
# ---------------------------------------------------------------------------


def test_weights_are_configurable_per_signal() -> None:
    """FR-013: cada peso a 0 aísla su señal (visual vs nº de frames)."""
    a = _candidate(
        "visual-best",
        matching_frames=1,
        best_distance=0.1,  # visual 0.9
        best_frame_id="frame-0",
        best_timestamp_ms=1_000,
    )
    b = _candidate(
        "frames-best",
        matching_frames=5,  # 5/5 -> frames 1.0
        best_distance=0.3,  # visual 0.7
        best_frame_id="frame-0",
        best_timestamp_ms=2_000,
    )
    result = _result(_QUERY_PHASH, a, b)
    phashes = _phashes_at_distance(0, 0)

    only_visual = rank_candidates(
        result,
        frame_phashes=phashes,
        weights=RankingWeights(visual=1.0, frames=0.0, phash=0.0),
    )
    assert [item.video_id for item in only_visual] == ["visual-best", "frames-best"]
    assert only_visual[0].match_score == pytest.approx(0.9)
    assert only_visual[1].match_score == pytest.approx(0.7)

    only_frames = rank_candidates(
        result,
        frame_phashes=phashes,
        weights=RankingWeights(visual=0.0, frames=1.0, phash=0.0),
    )
    assert [item.video_id for item in only_frames] == ["frames-best", "visual-best"]
    assert only_frames[0].match_score == pytest.approx(1.0)


def test_weights_are_normalized_internally() -> None:
    """FR-013: los pesos son relativos; (2,2,2) == (1,1,1) en el score."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "video-a",
            matching_frames=2,
            best_distance=0.2,
            best_frame_id="frame-16",
            best_timestamp_ms=1_000,
        ),
    )
    phashes = _phashes_at_distance(16)
    base = rank_candidates(result, frame_phashes=phashes)
    scaled = rank_candidates(
        result,
        frame_phashes=phashes,
        weights=RankingWeights(visual=2.0, frames=2.0, phash=2.0),
    )
    assert scaled[0].match_score == pytest.approx(base[0].match_score)


def test_invalid_weights_raise() -> None:
    """FR-013: pesos negativos o todos a cero -> ValueError (score no definido)."""
    with pytest.raises(ValueError, match="peso"):
        RankingWeights(visual=-0.1, frames=0.5, phash=0.5)
    with pytest.raises(ValueError, match="suma"):
        RankingWeights(visual=0.0, frames=0.0, phash=0.0)


# ---------------------------------------------------------------------------
# FR-013 · evidencia pHash (ADR-0005) y valores ausentes
# ---------------------------------------------------------------------------


def test_phash_evidence_breaks_visual_tie() -> None:
    """FR-013/ADR-0005: a igual visual y frames, gana la evidencia pHash."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "near-exact",
            matching_frames=2,
            best_distance=0.1,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "semantic-only",
            matching_frames=2,
            best_distance=0.1,
            best_frame_id="frame-64",
            best_timestamp_ms=2_000,
        ),
    )
    ranked = rank_candidates(result, frame_phashes=_phashes_at_distance(0, 64))

    assert ranked[0].video_id == "near-exact"
    assert ranked[0].phash_score == pytest.approx(1.0)  # Hamming 0
    assert ranked[1].phash_score == pytest.approx(0.0)  # Hamming 64

    # sin peso pHash -> empate exacto, resuelto de forma determinista
    no_phash = rank_candidates(
        result,
        frame_phashes=_phashes_at_distance(0, 64),
        weights=RankingWeights(visual=1.0, frames=0.0, phash=0.0),
    )
    assert [item.video_id for item in no_phash] == ["near-exact", "semantic-only"]
    assert no_phash[0].match_score == no_phash[1].match_score


def test_missing_frame_phash_contributes_zero() -> None:
    """FR-013: frame sin pHash en el mapeo -> evidencia 0 (sin penalizar otras)."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "with-phash",
            matching_frames=2,
            best_distance=0.1,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "without-phash",
            matching_frames=2,
            best_distance=0.1,
            best_frame_id="frame-unknown",
            best_timestamp_ms=2_000,
        ),
    )
    ranked = rank_candidates(result, frame_phashes=_phashes_at_distance(0))

    by_id = {item.video_id: item for item in ranked}
    assert by_id["with-phash"].phash_score == pytest.approx(1.0)
    assert by_id["without-phash"].phash_score == pytest.approx(0.0)
    # con pesos por defecto (phash = 0.2), el que tiene evidencia gana
    assert ranked[0].video_id == "with-phash"


# ---------------------------------------------------------------------------
# SC-002 · una consulta negativa no supera el umbral
# ---------------------------------------------------------------------------


def test_negative_query_stays_below_threshold() -> None:
    """SC-002: consulta ajena al índice -> mejor score por debajo del umbral.

    Escenario negativo: todos los candidatos con distancia alta (similitud
    visual ~0.0), pHash lejano (Hamming >= 30) y frames repartidos. Con
    `min_score=0.5` el resultado debe quedar vacío.
    """
    candidates = [
        _candidate(
            f"video-{i}",
            matching_frames=1 + (i % 3),
            best_distance=0.85 + 0.01 * i,
            best_frame_id=f"frame-{30 + i}",
            best_timestamp_ms=i * 1_000,
        )
        for i in range(3)
    ]
    result = _result(_QUERY_PHASH, *candidates)
    phashes = {f"frame-{30 + i}": _QUERY_PHASH ^ ((1 << (30 + i)) - 1) for i in range(3)}

    ranked = rank_candidates(result, frame_phashes=phashes)
    assert ranked  # el ranking en sí devuelve candidatos (orden por score)
    assert all(item.match_score < 0.5 for item in ranked)  # bajo el umbral

    # con el umbral configurado, la negativa queda rechazada (SC-002)
    filtered = rank_candidates(result, frame_phashes=phashes, min_score=0.5)
    assert filtered == ()


# ---------------------------------------------------------------------------
# FR-012 · match_timestamp_ms
# ---------------------------------------------------------------------------


def test_match_timestamp_ms_none_when_best_frame_has_no_timestamp() -> None:
    """FR-012: mejor frame sin timestamp -> match_timestamp_ms None (no falla)."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "video-a",
            matching_frames=2,
            best_distance=0.1,
            best_frame_id="frame-0",
            best_timestamp_ms=None,
        ),
    )
    (item,) = rank_candidates(result, frame_phashes=_phashes_at_distance(0))
    assert item.match_timestamp_ms is None


# ---------------------------------------------------------------------------
# FR-014 · exclusión de vídeos
# ---------------------------------------------------------------------------


def test_excluded_videos_are_hidden() -> None:
    """FR-014: un vídeo excluido no aparece, aunque hubiera ganado."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "winner-excluded",
            matching_frames=3,
            best_distance=0.0,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "loser-kept",
            matching_frames=1,
            best_distance=0.6,
            best_frame_id="frame-64",
            best_timestamp_ms=2_000,
        ),
    )
    ranked = rank_candidates(
        result,
        frame_phashes=_phashes_at_distance(0, 64),
        excluded_videos={"winner-excluded"},
    )
    assert [item.video_id for item in ranked] == ["loser-kept"]


def test_exclude_hides_when_only_candidate() -> None:
    """FR-014: único candidato excluido -> sin resultados (no se filtra antes)."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "solo",
            matching_frames=2,
            best_distance=0.0,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
    )
    assert (
        rank_candidates(
            result,
            frame_phashes=_phashes_at_distance(0),
            excluded_videos={"solo"},
        )
        == ()
    )


def test_frames_max_normalization_excludes_ignored_videos() -> None:
    """FR-014: el máximo de frames se calcula sobre los candidatos no excluidos."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "excluded-many",
            matching_frames=10,
            best_distance=0.0,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "kept-two",
            matching_frames=2,
            best_distance=0.5,
            best_frame_id="frame-64",
            best_timestamp_ms=2_000,
        ),
        _candidate(
            "kept-four",
            matching_frames=4,
            best_distance=0.5,
            best_frame_id="frame-0",
            best_timestamp_ms=3_000,
        ),
    )
    ranked = rank_candidates(
        result,
        frame_phashes=_phashes_at_distance(0, 64, 0),
        excluded_videos={"excluded-many"},
    )
    by_id = {item.video_id: item for item in ranked}
    assert "excluded-many" not in by_id
    assert by_id["kept-two"].frames_score == pytest.approx(0.5)  # 2/4
    assert by_id["kept-four"].frames_score == pytest.approx(1.0)  # 4/4


# ---------------------------------------------------------------------------
# FR-012 · min_score y robustez
# ---------------------------------------------------------------------------


def test_min_score_filters_results() -> None:
    """FR-012: `min_score` descarta resultados por debajo del umbral."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "strong",
            matching_frames=3,
            best_distance=0.0,
            best_frame_id="frame-0",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "weak",
            matching_frames=1,
            best_distance=0.9,
            best_frame_id="frame-64",
            best_timestamp_ms=2_000,
        ),
    )
    phashes = _phashes_at_distance(0, 64)
    all_ranked = rank_candidates(result, frame_phashes=phashes)
    assert {item.video_id for item in all_ranked} == {"strong", "weak"}

    strong_only = rank_candidates(result, frame_phashes=phashes, min_score=0.8)
    assert [item.video_id for item in strong_only] == ["strong"]


def test_min_score_validated() -> None:
    """FR-012: `min_score` fuera de [0, 1] -> ValueError."""
    result = _result(
        _QUERY_PHASH,
        _candidate(
            "video-a",
            matching_frames=1,
            best_distance=0.1,
            best_frame_id="frame-0",
            best_timestamp_ms=None,
        ),
    )
    with pytest.raises(ValueError, match="min_score"):
        rank_candidates(result, frame_phashes=_phashes_at_distance(0), min_score=-0.1)
    with pytest.raises(ValueError, match="min_score"):
        rank_candidates(result, frame_phashes=_phashes_at_distance(0), min_score=1.1)


def test_rank_empty_and_deterministic_tie() -> None:
    """FR-013: sin candidatos -> () y empates resueltos de forma determinista."""
    assert rank_candidates(_result(_QUERY_PHASH), frame_phashes={}) == ()

    result = _result(
        _QUERY_PHASH,
        _candidate(
            "b-video",
            matching_frames=2,
            best_distance=0.2,
            best_frame_id="frame-16",
            best_timestamp_ms=1_000,
        ),
        _candidate(
            "a-video",
            matching_frames=2,
            best_distance=0.2,
            best_frame_id="frame-16",
            best_timestamp_ms=2_000,
        ),
    )
    ranked = rank_candidates(
        result,
        frame_phashes=_phashes_at_distance(16, 16),
        weights=RankingWeights(visual=0.0, frames=1.0, phash=0.0),
    )
    # mismo score: tie-break por video_id (orden estable y determinista)
    assert [item.video_id for item in ranked] == ["a-video", "b-video"]


# ---------------------------------------------------------------------------
# Contrato de constantes
# ---------------------------------------------------------------------------


def test_default_constants_are_sane() -> None:
    """FR-013: pesos por defecto positivos que suman 1 y umbral por defecto 0."""
    assert DEFAULT_WEIGHTS.visual >= 0.0
    assert DEFAULT_WEIGHTS.frames >= 0.0
    assert DEFAULT_WEIGHTS.phash >= 0.0
    assert DEFAULT_WEIGHTS.visual + DEFAULT_WEIGHTS.frames + DEFAULT_WEIGHTS.phash == pytest.approx(
        1.0
    )
    assert DEFAULT_MIN_SCORE == 0.0
    # el resultado rankeado es un RankedVideo con el contrato de FR-012
    assert RankedVideo.__annotations__["match_timestamp_ms"] == "int | None"
