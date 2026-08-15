"""Pipeline de búsqueda por imagen (PR-012 · FR-010 · FR-012 parcial · contracts §1).

Cadena de la consulta (FR-010):

    normalizar (RGB) -> pHash (PR-004) -> embedding (lote de 1, PR-002/005)
    -> VectorStore.ann_search (FR-006, ADR-0004) -> agrupar por video_id

Depende solo de las interfaces (ADR-0007): `VectorStore` y
`EmbeddingProvider` se inyectan por constructor. En tests se usan
`InMemoryVectorStore` + `FakeEmbeddingProvider` (deterministas, sin DB ni
Torch); en producción `PgVectorStore` + `SiglipLocalProvider`.

Decisiones de alcance (ver handoff PR-012):

- **Normalización mínima**: solo conversión a RGB. El resize al tamaño del
  modelo es responsabilidad del proveedor (`SiglipLocalProvider` aplica su
  propio preprocess de open_clip); aquí NO se duplica esa lógica (ADR-0007).
  Para `FakeEmbeddingProvider` el tamaño es irrelevante (hash de píxeles).
- **pHash de la consulta en el resultado**: se calcula siempre (FR-004) y
  viaja en `ImageSearchResult.query_phash`; el ranking de PR-013 lo usará
  como evidencia pHash. El pHash PERSISTIDO de los frames aún es centinela 0
  (gap conocido, lo corregirá el orquestador antes de PR-013): en este PR el
  ANN usa SOLO la señal del embedding.
- **Sin artefactos temporales (FR-018/ADR-0006)**: la búsqueda no escribe en
  disco y no persiste la media de consulta ni su embedding; el borrado del
  fichero de consulta (si existe) es responsabilidad del llamador (PR-014).
- **`k` configurable** (`top_k`): por constructor y por llamada; el resultado
  mantiene el orden por distancia (menor = más similar, contracts §2).
- **Exclusión de vídeos (FR-014)**: se delega en el default de
  `VectorStore.ann_search` (`exclude_videos=True`); el ranking de PR-013
  gestiona el umbral de match (SC-002).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image

from xtrace_spike.embeddings.provider import EmbeddingProvider
from xtrace_spike.hashing.phash import compute_phash
from xtrace_spike.vectorstore.base import FrameHit, VectorStore

#: `k` por defecto del ANN (contracts §1: `--top-k K=10`).
DEFAULT_TOP_K: int = 10


@dataclass(frozen=True)
class VideoCandidate:
    """Vídeo candidato agrupado desde los `FrameHit` del ANN (FR-010).

    Agrupa los frames coincidentes de un mismo vídeo conservando lo que el
    ranking de PR-013 necesita: nº de frames coincidentes, mejor distancia
    (menor = más similar, contracts §2) y el timestamp del mejor frame.
    El `match_score` final NO es de este PR (es PR-013).
    """

    video_id: str
    matching_frames: int
    best_distance: float
    best_frame_id: str
    best_timestamp_ms: int | None


@dataclass(frozen=True)
class ImageSearchResult:
    """Resultado de una búsqueda por imagen (FR-010 · FR-012 parcial).

    Atributos:
        query_phash: pHash de 64 bits de la consulta (FR-004); lo usará el
            ranking de PR-013 como evidencia pHash contra los frames.
        candidates: vídeos candidatos agrupados, ordenados por mejor
            distancia ascendente (el más similar primero).
        total_hits: nº de `FrameHit` devueltos por el ANN (antes de agrupar).
    """

    query_phash: int
    candidates: tuple[VideoCandidate, ...]
    total_hits: int


def normalize_query_image(image: Image.Image) -> Image.Image:
    """Normaliza la imagen de consulta: PIL -> RGB (FR-010).

    El resize al tamaño esperado por el modelo es responsabilidad del
    proveedor (`SiglipLocalProvider` aplica su preprocess; ADR-0007), así que
    aquí solo se garantiza el modo RGB. Para `FakeEmbeddingProvider` el
    tamaño no afecta (hash de píxeles), por lo que el resultado es idéntico.
    """
    return image.convert("RGB")


def group_hits_by_video(hits: Sequence[FrameHit]) -> tuple[VideoCandidate, ...]:
    """Agrupa los `FrameHit` del ANN por `video_id` (FR-010 · contracts §1).

    Por grupo se conserva: nº de frames coincidentes, mejor distancia
    (mínima; menor = más similar), id y timestamp del mejor frame.

    El orden del resultado sigue la distancia del mejor frame de cada vídeo
    (ascendente). Como `ann_search` ya devuelve los hits ordenados por
    distancia, el primer hit de cada vídeo suele ser su mejor frame; la
    ordenación final es defensiva y estable (tie-break por primer hit).

    Args:
        hits: frames candidatos devueltos por `VectorStore.ann_search`.

    Returns:
        Tupla de candidatos, uno por vídeo, ordenados por `best_distance`.
    """
    best_by_video: dict[str, FrameHit] = {}
    matching_by_video: dict[str, int] = {}
    for hit in hits:
        video_id = hit["video_id"]
        matching_by_video[video_id] = matching_by_video.get(video_id, 0) + 1
        previous = best_by_video.get(video_id)
        if previous is None or hit["distance"] < previous["distance"]:
            best_by_video[video_id] = hit

    candidates = [
        VideoCandidate(
            video_id=video_id,
            matching_frames=matching_by_video[video_id],
            best_distance=best["distance"],
            best_frame_id=best["frame_id"],
            best_timestamp_ms=best["timestamp_ms"],
        )
        for video_id, best in best_by_video.items()
    ]
    candidates.sort(key=lambda candidate: candidate.best_distance)
    return tuple(candidates)


class ImageSearch:
    """Búsqueda por imagen: normalizar → pHash → embed → ANN → agrupar (FR-010).

    Uso típico:

        searcher = ImageSearch(store=store, embeddings=embeddings, top_k=10)
        result = await searcher.search_image(query_image)

    Depende solo de las interfaces `VectorStore` y `EmbeddingProvider`
    (ADR-0007); la media de consulta no se persiste (FR-018 · ADR-0006).
    """

    def __init__(
        self,
        *,
        store: VectorStore,
        embeddings: EmbeddingProvider,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        if top_k <= 0:
            raise ValueError(f"top_k debe ser > 0 (recibido {top_k})")
        self._store = store
        self._embeddings = embeddings
        self._top_k = top_k

    @property
    def top_k(self) -> int:
        """Nº de frames candidatos del ANN por defecto (configurable por llamada)."""
        return self._top_k

    async def search_image(
        self,
        image: Image.Image,
        *,
        top_k: int | None = None,
    ) -> ImageSearchResult:
        """Ejecuta la búsqueda por imagen contra el índice (FR-010).

        Cadena: normalizar (RGB) → pHash de la consulta (FR-004, lo usará el
        ranking PR-013) → embedding (lote de 1, validando el contrato (N, D))
        → `VectorStore.ann_search(embedding, k)` → agrupar por `video_id`
        (un candidato por vídeo, ordenado por mejor distancia).

        Args:
            image: imagen de consulta (PIL; se normaliza a RGB internamente).
            top_k: override del nº de frames candidatos del ANN; None usa el
                valor del constructor.

        Raises:
            ValueError: si `top_k` <= 0.

        Returns:
            `ImageSearchResult` con el pHash de la consulta, los candidatos
            agrupados por vídeo y el nº total de hits del ANN.
        """
        k = self._top_k if top_k is None else top_k
        if k <= 0:
            raise ValueError(f"top_k debe ser > 0 (recibido {k})")

        query_image = normalize_query_image(image)
        query_phash = compute_phash(query_image)
        embedding = self._embed_query(query_image)
        hits = await self._store.ann_search(embedding, k)
        candidates = group_hits_by_video(hits)
        return ImageSearchResult(
            query_phash=query_phash,
            candidates=candidates,
            total_hits=len(hits),
        )

    def _embed_query(self, image: Image.Image) -> list[float]:
        """Embedding de la consulta: lote de 1 imagen, validando (N, D).

        El embedding solo vive en memoria durante la llamada (FR-018); se
        convierte a float nativo, serializable y compatible con el contrato
        `VectorStore.ann_search` (mismo criterio que la indexación, PR-010).
        """
        vectors = self._embeddings.embed_images([image])
        expected = (1, self._embeddings.dimension)
        if vectors.shape != expected:
            raise ValueError(
                f"EmbeddingProvider devolvió shape {vectors.shape}; se esperaba {expected}"
            )
        return [float(value) for value in vectors[0]]
