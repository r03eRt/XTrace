"""Deterministic visual evaluation for already materialized assets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import numpy as np
from PIL import Image
from xtrace_spike.embeddings.provider import EmbeddingProvider  # type: ignore[import-untyped]

from .assets import MaterializedAsset
from .models import ResultRefinementStatus


@dataclass(frozen=True)
class EvaluationResult:
    """Decision from one candidate's in-memory asset batch."""

    timestamp_ms: int | None
    selected_asset: Any | None
    similarity: float
    status: ResultRefinementStatus
    evaluated_count: int
    discarded_count: int


class TemporalRefinementEvaluator:
    """Compare query/assets without depending on or mutating a VectorStore."""

    def __init__(self, embeddings: EmbeddingProvider) -> None:
        self._embeddings = embeddings

    def evaluate(
        self,
        query_image: Image.Image,
        assets: list[MaterializedAsset] | tuple[MaterializedAsset, ...],
        *,
        base_timestamp_ms: int | None,
        base_visual_similarity: float,
        duration_ms: int | None,
    ) -> EvaluationResult:
        try:
            valid: list[MaterializedAsset] = []
            seen: set[tuple[str, int | None]] = set()
            discarded = 0
            for item in assets:
                asset = item.asset
                timestamp = asset.timestamp_ms
                if timestamp is None:
                    discarded += 1
                    continue
                if timestamp < 0 or (duration_ms is not None and timestamp >= duration_ms):
                    discarded += 1
                    continue
                key = (asset.url, timestamp)
                if key in seen:
                    discarded += 1
                    continue
                seen.add(key)
                valid.append(item)

            query_vector = _one_vector(self._embeddings.embed_images([query_image]))
            if not valid:
                return EvaluationResult(
                    timestamp_ms=base_timestamp_ms,
                    selected_asset=None,
                    similarity=base_visual_similarity,
                    status=ResultRefinementStatus.UNCHANGED,
                    evaluated_count=0,
                    discarded_count=discarded,
                )

            vectors = self._embeddings.embed_images([item.image for item in valid])
            if vectors.shape[0] != len(valid):
                raise ValueError(
                    "el proveedor devolvió un embedding por número incorrecto de assets"
                )
            scores = [_cosine_similarity(query_vector, vector) for vector in vectors]
            best_score = max(scores)
            best_indexes = [
                index for index, score in enumerate(scores) if abs(score - best_score) < 1e-9
            ]
            if len(best_indexes) != 1 or best_score <= base_visual_similarity:
                return EvaluationResult(
                    timestamp_ms=base_timestamp_ms,
                    selected_asset=None,
                    similarity=base_visual_similarity,
                    status=ResultRefinementStatus.UNCHANGED,
                    evaluated_count=len(valid),
                    discarded_count=discarded,
                )

            selected = valid[best_indexes[0]]
            if selected.asset.timestamp_ms == base_timestamp_ms:
                return EvaluationResult(
                    timestamp_ms=base_timestamp_ms,
                    selected_asset=None,
                    similarity=base_visual_similarity,
                    status=ResultRefinementStatus.UNCHANGED,
                    evaluated_count=len(valid),
                    discarded_count=discarded,
                )
            return EvaluationResult(
                timestamp_ms=selected.asset.timestamp_ms,
                selected_asset=selected.asset,
                similarity=best_score,
                status=ResultRefinementStatus.IMPROVED,
                evaluated_count=len(valid),
                discarded_count=discarded,
            )
        finally:
            for item in assets:
                item.close()


def _one_vector(values: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    if values.ndim != 2 or values.shape[0] != 1:
        raise ValueError("el proveedor debe devolver shape (1, D) para la consulta")
    return cast(np.ndarray[Any, Any], values[0])


def _cosine_similarity(left: np.ndarray[Any, Any], right: np.ndarray[Any, Any]) -> float:
    if left.shape != right.shape:
        raise ValueError("dimensiones de embedding incompatibles")
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("embedding degenerado")
    cosine = float(np.dot(left, right) / (left_norm * right_norm))
    return max(0.0, min(1.0, cosine))
