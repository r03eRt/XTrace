"""POST /search (PR-055 · FR-001..005 · FR-011 (400/413/415) · FR-012/013
· SEC-002/003/005 · SC-006 · contracts §1/§5).

`multipart/form-data` con la parte de fichero **`image`** (JPEG/PNG/WebP,
≤ 10 MB) y los campos de formulario opcionales `top_k` (default 10) y
`min_score` (default 0.0) — mismos defaults que la CLI `search` (contracts
§1). Flujo:

1. parte `image` presente y con nombre → 400 `missing_file_part`;
2. `top_k`/`min_score` válidos → 400 `invalid_request`;
3. subida a temporal seguro con límite por streaming → 413 `media_too_large`;
4. validación del spike (firma MIME por cabecera) → 415 / 400;
5. decodificación forzada → 400 `media_corrupt` (sin ejecutar búsqueda);
6. **misma cadena que la CLI** (`search_service.run_image_search`), con la
   media bajo `QueryMediaContext` (borrado garantizado en `finally`,
   FR-003/SEC-003);
7. `finally` del handler borra el temporal de subida (la media rechazada
   tampoco deja restos — en la API el fichero es nuestro);
8. respuesta del contracts §1 con la extensión MAY `title`/`page_url`
   (FR-004) + registro analítico en `searches` (FR-012).

La media nunca se persiste ni se loguea (SEC-005): los logs solo llevan
`search_id`, `processing_ms`, `results_count` y `status`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from xtrace_spike.security import QueryMediaContext  # type: ignore[import-untyped]

from xtrace_api.analytics import record_refinement, record_search
from xtrace_api.config import get_settings
from xtrace_api.media import (
    MediaValidationError,
    open_query_image_checked,
    save_upload_to_temp,
    validate_query_media,
)
from xtrace_api.refinement.models import (
    RefinementOutcome,
    RefinementStatus,
    ResultRefinementStatus,
    TimestampOrigin,
)
from xtrace_api.refinement.models import (
    RefinementSummary as InternalRefinementSummary,
)
from xtrace_api.refinement.models import (
    TimestampProvenance as InternalTimestampProvenance,
)
from xtrace_api.refinement.policy import RefinementPolicy
from xtrace_api.schemas import (
    Evidence,
    SearchResponse,
    SearchResultItem,
)
from xtrace_api.schemas import (
    RefinementSummary as RefinementSummarySchema,
)
from xtrace_api.schemas import (
    TimestampProvenance as TimestampProvenanceSchema,
)
from xtrace_api.search_service import (
    VideoMetadata,
    build_refinement_orchestrator,
    run_image_search,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

#: Límite defensivo de `top_k` (el contrato §1 no lo acota; el default es 10).
#: Evita ANNs absurdos contra el índice real sin romper configuraciones
#: razonables de paridad con la CLI.
_MAX_TOP_K = 1000


def _parse_top_k(raw: str | None, default: int) -> int:
    """`top_k` del formulario: entero en [1, 1000] (400 invalid_request)."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise MediaValidationError(
            400, "invalid_request", "el campo 'top_k' debe ser un entero positivo"
        ) from None
    if not 1 <= value <= _MAX_TOP_K:
        raise MediaValidationError(
            400,
            "invalid_request",
            f"el campo 'top_k' debe estar entre 1 y {_MAX_TOP_K}",
        )
    return value


def _parse_min_score(raw: str | None, default: float) -> float:
    """`min_score` del formulario: número en [0, 1] (400 invalid_request)."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise MediaValidationError(
            400, "invalid_request", "el campo 'min_score' debe ser un número en [0, 1]"
        ) from None
    if not 0.0 <= value <= 1.0:
        raise MediaValidationError(
            400, "invalid_request", "el campo 'min_score' debe estar en [0, 1]"
        )
    return value


def _to_result_item(
    item: Any,
    metadata: VideoMetadata | None,
    provenance: InternalTimestampProvenance | None,
) -> SearchResultItem:
    """Convierte un `RankedVideo` del spike en el item del contracts §1.

    La extensión MAY (`title`/`page_url`, FR-004) y `local_ref` (paridad CLI)
    quedan `null` cuando el backend no expone metadatos (in-memory) o el
    vídeo no se encontró en `videos`.
    """
    rest_provenance = (
        TimestampProvenanceSchema(
            origin=provenance.origin.value,
            status=provenance.status.value,
            source=provenance.source,
            asset_kind=provenance.asset_kind.value if provenance.asset_kind else None,
            asset_url=provenance.asset_url,
            asset_position=provenance.asset_position,
        )
        if provenance is not None
        else None
    )
    return SearchResultItem(
        video_id=item.video_id,
        local_ref=metadata.local_ref if metadata else None,
        title=metadata.title if metadata else None,
        page_url=metadata.page_url if metadata else None,
        match_score=item.match_score,
        matching_frames=item.matching_frames,
        match_timestamp_ms=item.match_timestamp_ms,
        evidence=Evidence(
            visual=item.visual_similarity,
            phash=item.phash_score,
        ),
        timestamp_provenance=rest_provenance,
    )


def _to_refinement_summary(outcome: RefinementOutcome) -> RefinementSummarySchema:
    """Convert the internal immutable summary to the REST model."""

    return RefinementSummarySchema(**outcome.summary.__dict__)


def _refinement_evidence(outcome: RefinementOutcome) -> tuple[dict[str, Any], ...]:
    """Build metadata-only evidence rows from refined provenance.

    The evaluator intentionally exposes only the selected public asset in its
    provenance.  The writer re-validates/sanitises the URL and computes its
    hash before persistence; this mapping never contains query or media bytes.
    """

    rows: list[dict[str, Any]] = []
    for candidate_rank, item in enumerate(outcome.ranked, start=1):
        provenance = outcome.provenance.get(item.video_id)
        if provenance is None:
            continue
        if (
            provenance.origin != TimestampOrigin.REFINED_ASSET
            or provenance.asset_kind is None
            or provenance.asset_url is None
        ):
            continue
        rows.append(
            {
                "video_id": item.video_id,
                "source": provenance.source,
                "candidate_rank": candidate_rank,
                "asset_kind": provenance.asset_kind.value,
                "asset_url": provenance.asset_url,
                "position": provenance.asset_position,
                "timestamp_ms": item.match_timestamp_ms,
                "similarity": item.visual_similarity,
                "selected": provenance.status == ResultRefinementStatus.IMPROVED,
                "discarded_reason": None,
            }
        )
    return tuple(rows)


def _refinement_analytics_values(
    outcome: RefinementOutcome,
    *,
    policy: RefinementPolicy,
) -> dict[str, Any]:
    """Map the REST-safe outcome to the wider server-side telemetry shape."""

    summary = outcome.summary
    unchanged_count = sum(
        provenance.status == ResultRefinementStatus.UNCHANGED
        for provenance in outcome.provenance.values()
    )
    limit_reason: str | None = None
    if summary.status is RefinementStatus.LIMITED:
        if len(outcome.ranked) > summary.candidates_requested:
            limit_reason = "candidate_limit"
        elif summary.elapsed_ms >= policy.search_timeout_ms:
            limit_reason = "search_timeout"
        else:
            limit_reason = "budget_exhausted"

    # The orchestrator reports accepted and discarded assets, which is the
    # exact number of asset observations available to this API boundary.  It
    # avoids inventing a request count when a source returns no manifest.
    assets_requested = summary.assets_evaluated + summary.assets_discarded
    return {
        "status": summary.status.value,
        "policy_version": policy.policy_version,
        "candidates_requested": summary.candidates_requested,
        "candidates_processed": summary.candidates_processed,
        "assets_requested": assets_requested,
        "assets_evaluated": summary.assets_evaluated,
        "assets_discarded": summary.assets_discarded,
        "bytes_downloaded": summary.bytes_downloaded,
        "embedding_count": summary.embedding_count,
        "embedding_elapsed_ms": summary.embedding_elapsed_ms,
        "errors_count": summary.errors_count,
        "improved_count": summary.improved_results,
        "unchanged_count": unchanged_count,
        "elapsed_ms": summary.elapsed_ms,
        "limit_reason": limit_reason,
        "evidence": _refinement_evidence(outcome),
    }


def _record_refinement_best_effort(
    *,
    search_id: str,
    outcome: RefinementOutcome,
    policy: RefinementPolicy,
) -> None:
    """Write refinement telemetry without changing the search response path."""

    try:
        result = record_refinement(
            search_id=search_id,
            **_refinement_analytics_values(outcome, policy=policy),
        )
        if inspect.iscoroutine(result):
            asyncio.run(result)
    except Exception:
        # Validation and database failures are telemetry failures only.  Keep
        # logs bounded: never include provider errors, URLs or media payloads.
        logger.warning(
            "refinement: no se pudo persistir telemetría para search_id=%s",
            search_id,
        )


def _fallback_refinement(
    outcome: Any,
    *,
    policy: RefinementPolicy,
) -> RefinementOutcome:
    """Return a safe base-only outcome if composition or refinement fails."""

    provenance = {
        item.video_id: InternalTimestampProvenance(
            origin=TimestampOrigin.BASE_INDEX,
            status=(
                ResultRefinementStatus.DISABLED
                if not policy.enabled
                else ResultRefinementStatus.UNAVAILABLE
            ),
        )
        for item in outcome.ranked
    }
    return RefinementOutcome(
        ranked=tuple(outcome.ranked),
        provenance=provenance,
        summary=InternalRefinementSummary(
            status=(RefinementStatus.DISABLED if not policy.enabled else RefinementStatus.FAILED),
            candidates_requested=min(len(outcome.ranked), policy.candidate_limit),
            candidates_processed=0,
            assets_evaluated=0,
            assets_discarded=0,
            errors_count=0 if not policy.enabled else 1,
            bytes_downloaded=0,
            embedding_count=0,
            embedding_elapsed_ms=0,
            improved_results=0,
            elapsed_ms=0,
        ),
    )


def _run_refinement(
    query_image: Any,
    outcome: Any,
    policy: RefinementPolicy,
) -> RefinementOutcome:
    """Run the async second pass from the sync FastAPI handler."""

    async def run_and_close() -> RefinementOutcome:
        orchestrator = build_refinement_orchestrator(outcome, policy=policy)
        try:
            return await orchestrator.refine(
                query_image,
                outcome.ranked,
                outcome.metadata,
                policy=policy,
            )
        finally:
            close = getattr(orchestrator, "aclose", None)
            if callable(close):
                await close()

    try:
        return asyncio.run(run_and_close())
    except Exception:
        # Do not log remote exception text or tracebacks: adapters may include
        # URLs/response bodies.  The API contract already exposes a bounded
        # failed/unavailable summary while preserving the base ranking.
        logger.warning("refinement: fallo controlado; se conserva el primer pase")
        return _fallback_refinement(outcome, policy=policy)


@router.post("/search", response_model=SearchResponse)
def search(
    image: Annotated[UploadFile | None, File()] = None,
    top_k: Annotated[str | None, Form()] = None,
    min_score: Annotated[str | None, Form()] = None,
) -> SearchResponse:
    """Busca por imagen y devuelve los vídeos rankeados (contracts §1, FR-001).

    Handler **sync** (FastAPI lo ejecuta en su threadpool): la cadena async
    del spike se ejecuta con `asyncio.run` (mismo patrón que la CLI; plan
    §Scale/Scope). `processing_ms` mide la petición completa (paridad con el
    timing de la CLI `search`).
    """
    started = time.perf_counter()
    settings = get_settings()

    # Parte `image` obligatoria y con nombre (contracts §5: 400 missing_file_part).
    if image is None or not image.filename:
        raise MediaValidationError(
            400,
            "missing_file_part",
            "la petición debe incluir la parte 'image' con un nombre de fichero",
        )

    top_k_value = _parse_top_k(top_k, settings.search_default_top_k)
    min_score_value = _parse_min_score(min_score, settings.search_default_min_score)

    temp: Path | None = None
    try:
        temp = save_upload_to_temp(image, settings.work_root)
        validate_query_media(temp)
        with QueryMediaContext.from_file(temp, work_root=settings.work_root) as media:
            assert media.secure_copy is not None
            query_image = open_query_image_checked(media.secure_copy)
            search_id = str(uuid.uuid4())
            outcome = run_image_search(
                query_image,
                top_k=top_k_value,
                min_score=min_score_value,
            )
            try:
                refinement_policy = RefinementPolicy.from_env()
            except ValueError:
                logger.exception("refinement: configuración inválida; se desactiva")
                refinement_policy = RefinementPolicy(enabled=False)
            refinement = _run_refinement(query_image, outcome, refinement_policy)
            results = [
                _to_result_item(
                    item,
                    outcome.metadata.get(item.video_id),
                    refinement.provenance.get(item.video_id),
                )
                for item in refinement.ranked
            ]

        processing_ms = round((time.perf_counter() - started) * 1000)
        # FR-012: analítica en `searches` — solo con BD real (backend postgres);
        # en modo in-memory (tests/dev sin DB) no hay tabla que registrar.
        if outcome.backend_label == "postgres":
            record_search(
                search_id=search_id,
                processing_ms=processing_ms,
                results_count=len(results),
            )
            # The refinement FK points to ``searches``; persist its aggregate
            # only after the parent identity has been created.  The writer is
            # best-effort and receives metadata/provenance only.
            _record_refinement_best_effort(
                search_id=search_id,
                outcome=refinement,
                policy=refinement_policy,
            )
        logger.info(
            "search: search_id=%s processing_ms=%d results_count=%d status=ok",
            search_id,
            processing_ms,
            len(results),
        )
        return SearchResponse(
            search_id=search_id,
            processing_ms=processing_ms,
            refinement=_to_refinement_summary(refinement),
            results=results,
        )
    finally:
        # SEC-003/FR-003: el temporal de subida se borra SIEMPRE (éxito, error
        # de validación o fallo del pipeline); `QueryMediaContext` ya borró la
        # copia y el original al salir del bloque de procesado. Un fallo de
        # borrado se registra como warning sin enmascarar el resultado.
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                logger.warning("no se pudo borrar el temporal de la media de consulta")
