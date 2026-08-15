"""Runner del benchmark de búsqueda visual (PR-016 · FR-016 · SC-001/002/003/007
· contracts §1).

Ejecuta los casos de un dataset de benchmark (PR-015: `BenchmarkCase` +
`load_manifest`) contra el índice y produce el informe JSON del contrato CLI
§1: top1/top5/top10 sobre los casos positivos, FPR de las negativas con el
umbral `min_score` (PR-013), latencia p50/p95, frames/vídeo y tamaño del
índice desde `VectorStore.stats()`, y throughput de embeddings medido durante
el run.

Reutiliza la cadena de búsqueda real (PR-012/013/014), nunca una copia:
`ImageSearch.search_image` (normalizar → pHash → embed → ANN → agrupar) +
`rank_candidates` (match_score, evidencia pHash; vídeos excluidos fuera,
FR-014), con el mismo criterio de resolución de pHash de frames que la CLI
search (in-memory: `get_frame`; postgres: `PgRepo.get_frame_phashes`).

Semántica de las métricas (documentada para la evaluación de las puertas):

- **top1/top5/top10** (SC-001): fracción de casos POSITIVOS
  (expected_video_ref != None) cuyo vídeo esperado aparece en la posición 1
  / dentro de los 5 / 10 primeros resultados rankeados. El vídeo esperado
  se identifica por `video_id_for(expected_video_ref)` (FR-008: el id de
  vídeo es un uuid5 determinista del local_ref, el mismo que usa el pipeline
  de indexación PR-010).
- **false_positive_rate_negatives** (SC-002): fracción de casos NEGATIVOS
  cuyo mejor resultado supera el umbral `min_score` (es decir, que devuelven
  al menos un vídeo con `match_score >= min_score`, PR-013).
- **latency_ms.p50/p95** (SC-003): percentiles del tiempo de procesamiento
  por caso (búsqueda + ranking), en milisegundos enteros.
- **frames_per_video_avg** e **index_size_bytes**: de `VectorStore.stats()`
  (frames/vídeos; el tamaño solo si el store lo expone en stats — hoy
  ninguno lo expone y se reporta 0, documentado).
- **embedding_throughput_fps**: imágenes de consulta embebidas por segundo,
  medido durante el run con un proxy sobre el proveedor (tiempo real de
  `embed_images` acumulado).

Reproducibilidad (SC-007): el informe no contiene timestamps ni
aleatoriedad; top1/top5/top10, FPR, frames/vídeo y tamaño son deterministas
(mismos casos + mismo índice → mismos valores). Latencia y throughput
dependen del reloj y fluctúan entre ejecuciones: se reportan con precisión
estable (enteros ms; 2 decimales) y la tolerancia se documenta en el handoff.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

from xtrace_spike.benchmark.dataset import BenchmarkCase, BenchmarkError
from xtrace_spike.embeddings.provider import EmbeddingProvider
from xtrace_spike.indexing import video_id_for
from xtrace_spike.repo import PgRepo
from xtrace_spike.search import DEFAULT_TOP_K, ImageSearch, ImageSearchResult
from xtrace_spike.search.ranking import (
    DEFAULT_MIN_SCORE,
    DEFAULT_WEIGHTS,
    RankedVideo,
    RankingWeights,
    rank_candidates,
)
from xtrace_spike.vectorstore.base import VectorStore, VectorStoreStats
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore
from xtrace_spike.vectorstore.pgvector import PgVectorStore

#: Puerta de decisión SC-001 (spec 001): el vídeo correcto en Top-5 en ≥ 80%
#: de los casos positivos.
SC001_TOP5_GATE: float = 0.8

#: Puerta SC-002 (spec 001): ≥ 90% de las negativas quedan bajo el umbral
#: (equivale a FPR ≤ 10%).
SC002_NEGATIVE_GATE: float = 0.9

#: Precisión estable de las métricas del informe (SC-007).
_RATIO_PRECISION: int = 4
_FLOAT_PRECISION: int = 2


def _percentile(values: Sequence[float], p: float) -> float:
    """Percentil p (en 0..100) por el método nearest-rank (determinista).

    Con la lista ordenada de N valores, el percentil p es el valor en la
    posición ceil(p/100 * N) (1-based). Vacía -> 0.0 (informe sin casos).
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(p / 100.0 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def _open_case_image(path: Path) -> Image.Image:
    """Abre y decodifica la imagen de consulta de un caso (Pillow).

    Las consultas del benchmark son ficheros del dataset generado (PR-015),
    no media del operador: NO aplica el ciclo de borrado inmediato de la
    media de consulta (FR-018 · ADR-0006, garantía de la CLI search PR-014).
    Una imagen ilegible o ausente es un error del dataset (BenchmarkError).
    """
    try:
        with Image.open(path) as image:
            image.load()
            return image
    except (OSError, UnidentifiedImageError) as exc:
        raise BenchmarkError(
            f"no se puede decodificar la imagen de consulta {path}: {exc}"
        ) from exc


class _MeasuredEmbeddings:
    """Proxy de medida sobre `EmbeddingProvider` (FR-016 · throughput).

    Envuelve el proveedor real sin alterar su comportamiento (estructura
    compatible con el Protocol `EmbeddingProvider`, ADR-0007): cuenta las
    imágenes procesadas y acumula el tiempo real de `embed_images`. El runner
    deriva `embedding_throughput_fps = imágenes / segundos` al final del run.
    """

    def __init__(self, inner: EmbeddingProvider) -> None:
        self._inner = inner
        # Atributos planos (no read-only): el Protocol EmbeddingProvider declara
        # model_id/dimension como variables asignables (contracts §3).
        self.model_id = inner.model_id
        self.dimension = inner.dimension
        self._images: int = 0
        self._seconds: float = 0.0

    def embed_images(self, images: Sequence[Image.Image]) -> np.ndarray[Any, Any]:
        """Embedding del lote, midiendo tiempo e imágenes (FR-005/FR-016)."""
        started = time.perf_counter()
        vectors = self._inner.embed_images(images)
        self._images += len(images)
        self._seconds += time.perf_counter() - started
        return vectors

    @property
    def images(self) -> int:
        """Nº total de imágenes embebidas durante el run."""
        return self._images

    @property
    def seconds(self) -> float:
        """Tiempo total acumulado en `embed_images` durante el run."""
        return self._seconds


@dataclass(frozen=True)
class BenchmarkReport:
    """Informe de benchmark del contrato CLI §1 (FR-016).

    Campos idénticos a las claves del JSON del contrato (orden estable en
    `to_dict`). top1/top5/top10 y false_positive_rate_negatives son
    fracciones en [0, 1] (SC-001: Top-5 ≥ 0.8 == 80%). latency_ms son
    enteros (ms). frames_per_video_avg y embedding_throughput_fps llevan
    precisión estable (2 decimales).
    """

    cases: int
    top1: float
    top5: float
    top10: float
    false_positive_rate_negatives: float
    latency_ms: dict[str, int]  # {"p50": ms, "p95": ms}
    frames_per_video_avg: float
    index_size_bytes: int
    embedding_throughput_fps: float

    def to_dict(self) -> dict[str, object]:
        """Payload JSON del contrato CLI §1, en el orden del contrato."""
        return {
            "cases": self.cases,
            "top1": self.top1,
            "top5": self.top5,
            "top10": self.top10,
            "false_positive_rate_negatives": self.false_positive_rate_negatives,
            "latency_ms": {"p50": self.latency_ms["p50"], "p95": self.latency_ms["p95"]},
            "frames_per_video_avg": self.frames_per_video_avg,
            "index_size_bytes": self.index_size_bytes,
            "embedding_throughput_fps": self.embedding_throughput_fps,
        }

    def meets_sc001_gate(self) -> bool:
        """Puerta SC-001: el vídeo correcto en Top-5 en ≥ 80% de los positivos."""
        return self.top5 >= SC001_TOP5_GATE

    def meets_sc002_gate(self) -> bool:
        """Puerta SC-002: ≥ 90% de las negativas quedan bajo el umbral (FPR ≤ 10%)."""
        return self.false_positive_rate_negatives <= 1.0 - SC002_NEGATIVE_GATE


class BenchmarkRunner:
    """Ejecuta los casos del dataset de benchmark contra el índice (FR-016).

    Uso típico (paridad con la CLI, PR-011/PR-016):

        runner = BenchmarkRunner(store=backend.store, embeddings=embeddings, top_k=10)
        report = await runner.run(load_manifest(manifest_path))

    Depende solo de las interfaces (ADR-0007) `VectorStore` y
    `EmbeddingProvider`; el backend concreto lo resuelve el llamador
    (build_backend + resolve_embedding_provider en la CLI).
    """

    def __init__(
        self,
        *,
        store: VectorStore,
        embeddings: EmbeddingProvider,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        weights: RankingWeights = DEFAULT_WEIGHTS,
    ) -> None:
        if top_k <= 0:
            raise ValueError(f"top_k debe ser > 0 (recibido {top_k})")
        if not 0.0 <= min_score <= 1.0:
            raise ValueError(f"min_score debe estar en [0, 1] (recibido {min_score})")
        self._store = store
        self._measured = _MeasuredEmbeddings(embeddings)
        self._searcher = ImageSearch(store=store, embeddings=self._measured, top_k=top_k)
        self._min_score = min_score
        self._weights = weights

    async def run(self, cases: Sequence[BenchmarkCase]) -> BenchmarkReport:
        """Ejecuta todos los casos y produce el informe del contrato CLI §1.

        Por caso: abre la imagen de consulta, ejecuta la búsqueda real
        (PR-012) y el ranking (PR-013) con el `min_score` configurado, y
        clasifica el resultado (positivo: posición del vídeo esperado;
        negativo: ¿supera el umbral?). Mide la latencia por caso con
        `time.perf_counter` (SC-003) y el throughput de embeddings con el
        proxy (FR-016).

        Raises:
            BenchmarkError: si una imagen de consulta del dataset no se
                puede decodificar (dataset corrupto).
        """
        stats = await self._store.stats()
        positives = 0
        top1_hits = 0
        top5_hits = 0
        top10_hits = 0
        negatives = 0
        false_positives = 0
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            ranked = await self._rank_case(case)
            latencies.append(time.perf_counter() - started)
            if case.expected_video_ref is not None:
                positives += 1
                expected_id = video_id_for(case.expected_video_ref)
                video_ids = [item.video_id for item in ranked]
                if video_ids and video_ids[0] == expected_id:
                    top1_hits += 1
                if expected_id in video_ids[:5]:
                    top5_hits += 1
                if expected_id in video_ids[:10]:
                    top10_hits += 1
            else:
                negatives += 1
                # rank_candidates con min_score ya descartó los resultados
                # débiles: lista no vacía == el mejor score supera el umbral.
                if ranked:
                    false_positives += 1
        return self._build_report(
            cases=len(cases),
            positives=positives,
            top1_hits=top1_hits,
            top5_hits=top5_hits,
            top10_hits=top10_hits,
            negatives=negatives,
            false_positives=false_positives,
            latencies=latencies,
            stats=stats,
        )

    async def _rank_case(self, case: BenchmarkCase) -> tuple[RankedVideo, ...]:
        """Búsqueda + ranking de un caso (cadena real PR-012/013/014)."""
        query_image = _open_case_image(case.query_image_path)
        result = await self._searcher.search_image(query_image)
        frame_phashes = await self._resolve_frame_phashes(result)
        return rank_candidates(
            result,
            frame_phashes=frame_phashes,
            weights=self._weights,
            min_score=self._min_score,
        )

    async def _resolve_frame_phashes(self, result: ImageSearchResult) -> dict[str, int]:
        """pHash de los mejores frames para la evidencia pHash del ranking (FR-013).

        Paridad con la CLI search (PR-014): postgres lee el pHash persistido
        vía `PgRepo.get_frame_phashes`; in-memory lo expone `get_frame`
        (PR-003 · FIX-phash). Cualquier otro backend (inexistente hoy)
        devuelve {} → evidencia pHash neutra (PR-013).
        """
        frame_ids = [candidate.best_frame_id for candidate in result.candidates]
        if isinstance(self._store, InMemoryVectorStore):
            phashes: dict[str, int] = {}
            for frame_id in frame_ids:
                record = await self._store.get_frame(frame_id)
                if record is not None:
                    phashes[frame_id] = record["phash"]
            return phashes
        if isinstance(self._store, PgVectorStore):
            return await PgRepo().get_frame_phashes(frame_ids)
        return {}

    def _build_report(
        self,
        *,
        cases: int,
        positives: int,
        top1_hits: int,
        top5_hits: int,
        top10_hits: int,
        negatives: int,
        false_positives: int,
        latencies: Sequence[float],
        stats: VectorStoreStats,
    ) -> BenchmarkReport:
        """Agrega los contadores del run al informe del contrato (FR-016).

        Guardas de división: sin positivos -> top-K 0.0; sin negativas ->
        FPR 0.0; sin vídeos -> frames/vídeo 0.0; sin tiempo de embedding ->
        throughput 0.0. El tamaño del índice solo se reporta si el store lo
        expone en stats (hoy ninguno: 0, documentado).
        """
        top1 = top1_hits / positives if positives else 0.0
        top5 = top5_hits / positives if positives else 0.0
        top10 = top10_hits / positives if positives else 0.0
        fpr = false_positives / negatives if negatives else 0.0
        videos = stats["videos"]
        frames = stats["frames"]
        frames_per_video = frames / videos if videos else 0.0
        # El tamaño del índice no está en el contrato VectorStoreStats
        # (videos/frames/vectors) y hoy ningún store lo expone: se reporta 0
        # (documentado en el docstring del módulo). Si un store lo añade,
        # primero se amplía el contrato tipado (spec first).
        index_size_bytes = 0
        measured_seconds = self._measured.seconds
        throughput = self._measured.images / measured_seconds if measured_seconds > 0 else 0.0
        return BenchmarkReport(
            cases=cases,
            top1=round(top1, _RATIO_PRECISION),
            top5=round(top5, _RATIO_PRECISION),
            top10=round(top10, _RATIO_PRECISION),
            false_positive_rate_negatives=round(fpr, _RATIO_PRECISION),
            latency_ms={
                "p50": round(_percentile(latencies, 50)),
                "p95": round(_percentile(latencies, 95)),
            },
            frames_per_video_avg=round(frames_per_video, _FLOAT_PRECISION),
            index_size_bytes=index_size_bytes,
            embedding_throughput_fps=round(throughput, _FLOAT_PRECISION),
        )
