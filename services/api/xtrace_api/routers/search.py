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

import logging
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from xtrace_spike.security import QueryMediaContext  # type: ignore[import-untyped]

from xtrace_api.analytics import record_search
from xtrace_api.config import get_settings
from xtrace_api.media import (
    MediaValidationError,
    open_query_image_checked,
    save_upload_to_temp,
    validate_query_media,
)
from xtrace_api.schemas import Evidence, SearchResponse, SearchResultItem
from xtrace_api.search_service import VideoMetadata, run_image_search

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


def _to_result_item(item: Any, metadata: VideoMetadata | None) -> SearchResultItem:
    """Convierte un `RankedVideo` del spike en el item del contracts §1.

    La extensión MAY (`title`/`page_url`, FR-004) y `local_ref` (paridad CLI)
    quedan `null` cuando el backend no expone metadatos (in-memory) o el
    vídeo no se encontró en `videos`.
    """
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
    )


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
            outcome = run_image_search(
                query_image,
                top_k=top_k_value,
                min_score=min_score_value,
            )
            results = [
                _to_result_item(item, outcome.metadata.get(item.video_id))
                for item in outcome.ranked
            ]

        processing_ms = round((time.perf_counter() - started) * 1000)
        search_id = str(uuid.uuid4())
        # FR-012: analítica en `searches` — solo con BD real (backend postgres);
        # en modo in-memory (tests/dev sin DB) no hay tabla que registrar.
        if outcome.backend_label == "postgres":
            record_search(
                search_id=search_id,
                processing_ms=processing_ms,
                results_count=len(results),
            )
        logger.info(
            "search: search_id=%s processing_ms=%d results_count=%d status=ok",
            search_id,
            processing_ms,
            len(results),
        )
        return SearchResponse(search_id=search_id, processing_ms=processing_ms, results=results)
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
