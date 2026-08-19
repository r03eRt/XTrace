"""Tests del contrato de respuesta (PR-055 · FR-004 · contracts §1/§5).

Criterios verificables (tasks.md PR-055):
- `SearchResponse` reutiliza el JSON de la CLI `search` (spec 001 contracts
  §1) y lo amplía con la extensión MAY `title`/`page_url` (FR-004) sin
  alterar los campos existentes.
- Campos nullables: `local_ref`, `title`, `page_url` y `match_timestamp_ms`
  (contracts §1: pueden ser null; la serialización los conserva como null).
- `ApiError` es el cuerpo del contracts §5 (error en español + error_type).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xtrace_api.schemas import (
    ApiError,
    Evidence,
    RefinementSummary,
    SearchResponse,
    SearchResultItem,
    TimestampProvenance,
)


def test_search_response_parses_contract_example() -> None:
    """El ejemplo del contracts §1 (con la extensión MAY) se valida íntegro."""
    example = {
        "search_id": "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
        "processing_ms": 4123,
        "results": [
            {
                "video_id": "1a2b3c4d-0000-0000-0000-000000000001",
                "local_ref": "MAYO 2026 (386).mp4",
                "title": "Video de ejemplo del corpus",
                "page_url": "https://www.xvideos.com/video.abc123/ejemplo",
                "match_score": 0.938,
                "matching_frames": 2,
                "match_timestamp_ms": 51000,
                "evidence": {"visual": 0.95, "phash": 0.84},
            }
        ],
    }
    parsed = SearchResponse.model_validate(example)
    assert parsed.search_id == example["search_id"]
    assert parsed.processing_ms == 4123
    assert len(parsed.results) == 1
    top = parsed.results[0]
    assert top.video_id == example["results"][0]["video_id"]
    assert top.local_ref == "MAYO 2026 (386).mp4"
    assert top.title == "Video de ejemplo del corpus"
    assert top.page_url == "https://www.xvideos.com/video.abc123/ejemplo"
    assert top.match_score == 0.938
    assert top.matching_frames == 2
    assert top.match_timestamp_ms == 51000
    assert top.evidence == Evidence(visual=0.95, phash=0.84)


def test_search_response_keeps_nullable_fields_as_null_in_json() -> None:
    """local_ref/title/page_url/match_timestamp_ms null → null explícito en JSON."""
    response = SearchResponse(
        search_id="3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
        processing_ms=0,
        results=[
            SearchResultItem(
                video_id="1a2b3c4d-0000-0000-0000-000000000001",
                match_score=0.0,
                matching_frames=1,
                evidence=Evidence(visual=0.0, phash=0.0),
            )
        ],
    )
    dumped = response.model_dump(mode="json")
    top = dumped["results"][0]
    assert top["local_ref"] is None
    assert top["title"] is None
    assert top["page_url"] is None
    assert top["match_timestamp_ms"] is None
    assert top["timestamp_provenance"] is None
    assert dumped["refinement"] is None


def test_search_response_requires_cli_fields() -> None:
    """Los campos del contrato CLI son obligatorios (paridad FR-004)."""
    with pytest.raises(ValidationError):
        SearchResultItem(  # falta evidence
            video_id="1a2b3c4d-0000-0000-0000-000000000001",
            match_score=0.5,
            matching_frames=1,
        )
    with pytest.raises(ValidationError):
        SearchResponse(search_id="abc", processing_ms=10)  # falta results


def test_search_response_rejects_negative_processing_ms() -> None:
    """processing_ms no puede ser negativo (contracts §1: milisegundos reales)."""
    with pytest.raises(ValidationError):
        SearchResponse(search_id="abc", processing_ms=-1, results=[])


def test_api_error_contract_shape() -> None:
    """Cuerpo de error del contracts §5: error (español) + error_type estable."""
    error = ApiError(
        error="la imagen de consulta supera el límite de 10 MB",
        error_type="media_too_large",
    )
    assert error.model_dump() == {
        "error": "la imagen de consulta supera el límite de 10 MB",
        "error_type": "media_too_large",
    }


def test_search_response_parses_refinement_summary_and_provenance() -> None:
    response = SearchResponse.model_validate(
        {
            "search_id": "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
            "processing_ms": 1234,
            "refinement": {
                "status": "completed",
                "candidates_requested": 3,
                "candidates_processed": 2,
                "assets_evaluated": 18,
                "assets_discarded": 1,
                "errors_count": 0,
                "bytes_downloaded": 184320,
                "embedding_count": 18,
                "embedding_elapsed_ms": 72,
                "improved_results": 1,
                "elapsed_ms": 940,
            },
            "results": [
                {
                    "video_id": "1a2b3c4d-0000-0000-0000-000000000001",
                    "local_ref": None,
                    "match_score": 0.938,
                    "matching_frames": 1,
                    "match_timestamp_ms": 454000,
                    "evidence": {"visual": 0.99, "phash": 0.91},
                    "timestamp_provenance": {
                        "origin": "refined_asset",
                        "status": "improved",
                        "source": "xvideos",
                        "asset_kind": "thumbnail",
                        "asset_url": "https://thumb-cdn77.xvideos-cdn.com/xv_12_t.jpg",
                        "asset_position": 12,
                    },
                }
            ],
        }
    )

    assert response.refinement == RefinementSummary(
        status="completed",
        candidates_requested=3,
        candidates_processed=2,
        assets_evaluated=18,
        assets_discarded=1,
        errors_count=0,
        bytes_downloaded=184320,
        embedding_count=18,
        embedding_elapsed_ms=72,
        improved_results=1,
        elapsed_ms=940,
    )
    assert response.results[0].timestamp_provenance == TimestampProvenance(
        origin="refined_asset",
        status="improved",
        source="xvideos",
        asset_kind="thumbnail",
        asset_url="https://thumb-cdn77.xvideos-cdn.com/xv_12_t.jpg",
        asset_position=12,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "unknown"},
        {"status": "completed", "candidates_requested": -1},
    ],
)
def test_search_response_rejects_invalid_refinement_summary(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchResponse.model_validate(
            {
                "search_id": "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
                "processing_ms": 1,
                "refinement": payload,
                "results": [],
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "invented"),
        ("status", "invented"),
        ("asset_kind", "preview"),
    ],
)
def test_search_response_rejects_invalid_timestamp_provenance(
    field: str, value: str
) -> None:
    provenance = {
        "origin": "base_index",
        "status": "unchanged",
        "source": None,
        "asset_kind": None,
        "asset_url": None,
        "asset_position": None,
    }
    provenance[field] = value
    with pytest.raises(ValidationError):
        SearchResponse.model_validate(
            {
                "search_id": "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
                "processing_ms": 1,
                "results": [
                    {
                        "video_id": "1a2b3c4d-0000-0000-0000-000000000001",
                        "match_score": 0.5,
                        "matching_frames": 1,
                        "match_timestamp_ms": 1,
                        "evidence": {"visual": 0.5, "phash": 0.5},
                        "timestamp_provenance": provenance,
                    }
                ],
            }
        )
