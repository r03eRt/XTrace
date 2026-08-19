"""Orquesta la cadena de búsqueda del spike para la API (PR-055 · FR-001/004/005
· NFR-002 · SC-001 · contracts §1).

**Misma cadena que la CLI `search`** (paridad por construcción, FR-005):
`ImageSearch` (normalizar → pHash → embed → ANN → agrupar) + `rank_candidates`
(match_score, timestamp, evidencia visual/phash) con los mismos defaults
(top_k=10, min_score=0.0, `DEFAULT_WEIGHTS`). Se ejecuta con `asyncio.run`
desde el handler sync (patrón de la CLI; plan §Scale/Scope).

Enriquecimiento de resultados (FR-004 MAY): `local_ref` (paridad CLI) +
`title`/`page_url` (extensión MAY del contracts §1) desde `public.videos`
vía `PgRepo` (mismo patrón que `cli._fetch_local_refs`); en backend
in-memory los tres quedan `null` (paridad PR-014 del spike).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PIL import Image
from xtrace_spike.cli import CliBackend  # type: ignore[import-untyped]
from xtrace_spike.embeddings.provider import EmbeddingProvider  # type: ignore[import-untyped]
from xtrace_spike.repo import PgRepo, parse_uuid  # type: ignore[import-untyped]
from xtrace_spike.search import ImageSearch, ImageSearchResult  # type: ignore[import-untyped]
from xtrace_spike.search.ranking import rank_candidates  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.pgvector import PgVectorStore  # type: ignore[import-untyped]

from xtrace_api.deps import get_search_components
from xtrace_api.refinement.adapters import RefinementAdapterBridge
from xtrace_api.refinement.assets import AssetMaterializer
from xtrace_api.refinement.catalog import candidate_from_record
from xtrace_api.refinement.models import RefinementCandidate
from xtrace_api.refinement.policy import RefinementPolicy
from xtrace_api.refinement.service import TemporalRefinementOrchestrator


@dataclass(frozen=True)
class VideoMetadata:
    """Metadatos de un vídeo del índice para el enriquecimiento (FR-004 MAY)."""

    local_ref: str | None
    title: str | None
    page_url: str | None
    source: str | None = None
    adapter: str | None = None
    external_id: str | None = None
    duration_ms: int | None = None
    source_enabled: bool = False


@dataclass(frozen=True)
class SearchOutcome:
    """Resultado de la cadena: vídeos rankeados + metadatos por vídeo.

    `ranked` son los `RankedVideo` del spike (orden por `match_score`
    descendente; los vídeos excluidos nunca aparecen — FR-014 vía
    `rank_candidates` y el filtro por defecto del ANN).
    `backend_label` es la etiqueta estable del backend (`postgres` |
    `in-memory`): la analítica de `searches` (FR-012) solo aplica con BD.
    """

    ranked: tuple[Any, ...]
    metadata: dict[str, VideoMetadata]
    backend_label: str
    embeddings: EmbeddingProvider


def run_image_search(
    image: Image.Image,
    *,
    top_k: int,
    min_score: float,
) -> SearchOutcome:
    """Ejecuta la cadena de búsqueda del spike (misma que la CLI `search`).

    `ImageSearch.search_image` + evidencia pHash de los mejores frames +
    `rank_candidates` (mismos pesos y umbral que la CLI) + metadatos.

    Raises:
        psycopg.Error: índice/BD no disponible (el handler lo traduce a
            `503 index_unavailable` — contracts §5).
    """
    components = get_search_components()
    searcher = ImageSearch(
        store=components.backend.store,
        embeddings=components.embeddings,
        top_k=top_k,
    )
    result = asyncio.run(searcher.search_image(image))
    frame_phashes = _resolve_frame_phashes(components.backend, result)
    ranked = rank_candidates(result, frame_phashes=frame_phashes, min_score=min_score)
    metadata = _resolve_metadata(components.backend, [item.video_id for item in ranked])
    return SearchOutcome(
        ranked=ranked,
        metadata=metadata,
        backend_label=components.backend.label,
        embeddings=components.embeddings,
    )


def build_refinement_orchestrator(
    outcome: SearchOutcome,
    *,
    policy: RefinementPolicy,
) -> TemporalRefinementOrchestrator:
    """Compose the approved crawler boundary for one search request.

    The registry is loaded at the composition root, not by the refinement
    value objects. Local/in-memory results have no source metadata and therefore
    fail closed without making a network request.
    """

    from xtrace_crawler.cli import _default_registry  # type: ignore[import-untyped]

    bridge = RefinementAdapterBridge(_default_registry())

    def resolve_candidate(
        item: Any, metadata: Mapping[str, VideoMetadata]
    ) -> RefinementCandidate | None:
        record = metadata.get(item.video_id)
        if record is None or record.source is None or record.adapter is None:
            return None
        if record.external_id is None:
            return None
        return candidate_from_record(
            {
                "video_id": item.video_id,
                "source": record.source,
                "adapter": record.adapter,
                "external_id": record.external_id,
                "page_url": record.page_url,
                "duration_ms": record.duration_ms,
                "base_timestamp_ms": item.match_timestamp_ms,
                "base_visual_similarity": item.visual_similarity,
            }
        )

    async def resolve_assets(candidate: Any) -> Any:
        record = outcome.metadata.get(candidate.video_id)
        if record is None or not record.source_enabled:
            return ()
        adapter_name = candidate.adapter or candidate.source
        if adapter_name is None:
            return ()
        adapter = bridge.resolve(adapter_name, enabled_in_db=record.source_enabled)
        video = await bridge.get_video(
            adapter_name,
            candidate.external_id,
            page_url=candidate.page_url,
            enabled_in_db=record.source_enabled,
        )
        if video is None:
            return ()
        assets = await bridge.get_visual_assets(adapter, video)
        effective_policy = policy.for_source(candidate.source)

        async def fetch_bytes(asset: Any) -> bytes:
            return await bridge.fetch_asset_bytes(
                adapter,
                asset,
                max_bytes=effective_policy.max_asset_bytes,
            )

        return await AssetMaterializer(fetch_bytes).materialize(
            assets,
            max_assets=effective_policy.max_assets_per_candidate,
            max_bytes=effective_policy.max_asset_bytes,
        )

    return TemporalRefinementOrchestrator(
        embeddings=outcome.embeddings,
        candidate_resolver=resolve_candidate,
        asset_resolver=resolve_assets,
        cleanup=bridge.aclose,
    )


def _resolve_frame_phashes(backend: CliBackend, result: ImageSearchResult) -> dict[str, int]:
    """pHash de los mejores frames para la evidencia pHash (paridad CLI).

    Postgres: `PgRepo.get_frame_phashes` (pHash persistido en `frames.phash`).
    In-memory: `InMemoryVectorStore.get_frame` expone el pHash real del frame.
    Cualquier otro backend devuelve {} → evidencia pHash neutra (PR-013).
    """
    frame_ids = [candidate.best_frame_id for candidate in result.candidates]
    if isinstance(backend.store, PgVectorStore):
        return asyncio.run(PgRepo().get_frame_phashes(frame_ids))
    if isinstance(backend.store, InMemoryVectorStore):
        phashes: dict[str, int] = {}
        for frame_id in frame_ids:
            record = asyncio.run(backend.store.get_frame(frame_id))
            if record is not None:
                phashes[frame_id] = record["phash"]
        return phashes
    return {}


def _resolve_metadata(backend: CliBackend, video_ids: Sequence[str]) -> dict[str, VideoMetadata]:
    """Metadatos por vídeo: `local_ref` (paridad CLI) + `title`/`page_url` (MAY).

    Postgres: consulta a `public.videos` vía `PgRepo` (mismo patrón que la
    CLI `_fetch_local_refs`). In-memory: vacío → los tres campos quedan null
    (paridad PR-014 del spike, documentada en su handoff).
    """
    if not isinstance(backend.store, PgVectorStore):
        return {}
    return asyncio.run(_fetch_video_metadata(video_ids))


async def _fetch_video_metadata(video_ids: Sequence[str]) -> dict[str, VideoMetadata]:
    """`local_ref`/`title`/`page_url` de `public.videos` por `video_id` (FR-004 MAY).

    Los `video_id` provienen de los resultados rankeados (sus vídeos existen
    por FK, PR-007); un vídeo ausente simplemente no aparece en el mapeo (los
    campos quedan null en la respuesta).
    """
    video_uuids = [parse_uuid(video_id, "video_id") for video_id in video_ids]
    if not video_uuids:
        return {}
    async with await PgRepo().connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "select v.id::text, v.local_ref, v.title, v.page_url, "
                "s.name, s.adapter, v.external_id, v.duration_ms, "
                "coalesce(s.enabled, false) "
                "from public.videos v left join public.sources s on s.id = v.source_id "
                "where v.id = any(%s::uuid[])",
                ([str(video_uuid) for video_uuid in video_uuids],),
            )
            rows = await cur.fetchall()
    return {
        str(row[0]): VideoMetadata(
            local_ref=str(row[1]) if row[1] is not None else None,
            title=row[2],
            page_url=row[3],
            source=row[4],
            adapter=row[5],
            external_id=row[6],
            duration_ms=row[7],
            source_enabled=bool(row[8]),
        )
        for row in rows
    }
