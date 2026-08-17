"""Tests unitarios de la validación y ciclo de vida de la media (PR-055 · FR-002/003
· SEC-002/003/005 · SC-006 · contracts §5).

Criterios verificables (tasks.md PR-055):
- Mapeo validación → HTTP reutilizando la lógica del spike (sin duplicar
  reglas): 413 por tamaño (streaming, sin procesar), 415 por firma MIME,
  400 por contenido corrupto/parte ausente/nombre vacío.
- La media rechazada no deja restos en `work_root` (SC-003: en la API el
  fichero es del sistema, a diferencia de la CLI donde el original del
  operador no se toca).
- SC-006 vía HTTP: los 4xx se devuelven **sin ejecutar la búsqueda** (espía
  sobre `run_image_search`).
- Los mensajes de error van en español y sin rutas ni nombres de fichero
  (UX-001 · SEC-005).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from starlette.datastructures import UploadFile
from xtrace_spike.security import MAX_QUERY_IMAGE_BYTES  # type: ignore[import-untyped]

import xtrace_api.routers.search as search_router
from tests.fixtures import make_bogus_file, make_query_image
from xtrace_api.main import app
from xtrace_api.media import (
    MediaValidationError,
    open_query_image_checked,
    save_upload_to_temp,
    validate_query_media,
)


@pytest.fixture
def search_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Sustituye `run_image_search` por un espía que falla si se invoca (SC-006).

    Los tests de 4xx verifican que la lista de llamadas queda vacía: la
    validación rechaza **sin ejecutar la búsqueda** (contracts §5).
    """
    calls: list[tuple[object, ...]] = []

    def spy(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("SC-006: la búsqueda no debe ejecutarse en un 4xx")

    monkeypatch.setattr(search_router, "run_image_search", spy)
    return calls


def _upload(content: bytes, *, filename: str = "query.png") -> UploadFile:
    """UploadFile de Starlette con el contenido dado (sin pasar por HTTP)."""
    return UploadFile(file=io.BytesIO(content), filename=filename)


def _assert_error_body(response: Response, status: int, error_type: str) -> None:
    """Cuerpo del contracts §5: {"error" (español), "error_type"} + código."""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "error_type"}
    assert body["error_type"] == error_type
    assert isinstance(body["error"], str) and body["error"]


# ---------------------------------------------------------------------------
# save_upload_to_temp: temporal seguro + límite por streaming (413)
# ---------------------------------------------------------------------------


def test_save_upload_creates_secure_temp_in_work_root(tmp_path: Path) -> None:
    """mkstemp 0600 en work_root con el contenido íntegro (ADR-0006 · SEC-005)."""
    work = tmp_path / "work"
    work.mkdir()
    source = make_query_image(tmp_path / "query.png")
    target = save_upload_to_temp(_upload(source.read_bytes()), work)

    try:
        assert target.parent == work
        assert target.name.startswith("xtrace-api-upload-")
        assert target.stat().st_mode & 0o777 == 0o600  # solo el propietario
        assert target.read_bytes() == source.read_bytes()
    finally:
        target.unlink(missing_ok=True)


def test_save_upload_rejects_oversize_with_413_and_no_leftovers(tmp_path: Path) -> None:
    """Media > 10 MB → 413 media_too_large sin procesar y sin temporales (FR-002/SC-006)."""
    work = tmp_path / "work"
    work.mkdir()
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_QUERY_IMAGE_BYTES + 1)

    with pytest.raises(MediaValidationError) as excinfo:
        save_upload_to_temp(_upload(oversize), work)

    assert excinfo.value.status_code == 413
    assert excinfo.value.error_type == "media_too_large"
    assert "10 MB" in excinfo.value.message
    assert list(work.iterdir()) == []  # el parcial se borra (SC-003)


# ---------------------------------------------------------------------------
# validate_query_media: 415 por firma, 400 en el resto (reutiliza el spike)
# ---------------------------------------------------------------------------


def test_validate_query_media_rejects_unknown_signature_with_415(tmp_path: Path) -> None:
    """Firma MIME no soportada → 415 media_type_not_supported (FR-002/ADR-0008)."""
    bogus = make_bogus_file(tmp_path / "fake.png")

    with pytest.raises(MediaValidationError) as excinfo:
        validate_query_media(bogus)

    assert excinfo.value.status_code == 415
    assert excinfo.value.error_type == "media_type_not_supported"
    assert any(word in excinfo.value.message for word in ("JPEG", "PNG", "WebP"))


def test_validate_query_media_accepts_valid_png(tmp_path: Path) -> None:
    """PNG con firma válida pasa la validación (la firma se comprueba por cabecera)."""
    query = make_query_image(tmp_path / "query.png")
    validate_query_media(query)  # no debe lanzar


# ---------------------------------------------------------------------------
# open_query_image_checked: contenido corrupto → 400 media_corrupt
# ---------------------------------------------------------------------------


def test_open_query_image_checked_rejects_corrupt_content_with_400(tmp_path: Path) -> None:
    """Firma válida pero contenido ilegible → 400 media_corrupt (contracts §5)."""
    corrupt = make_bogus_file(tmp_path / "corrupt.png", with_png_signature=True)

    with pytest.raises(MediaValidationError) as excinfo:
        open_query_image_checked(corrupt)

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_type == "media_corrupt"


def test_open_query_image_checked_decodes_valid_image(tmp_path: Path) -> None:
    """Una imagen válida se decodifica (load() forzado) y devuelve la imagen."""
    query = make_query_image(tmp_path / "query.png")
    image = open_query_image_checked(query)
    assert image.size == (64, 48)


# ---------------------------------------------------------------------------
# SC-006 vía HTTP: 400/413/415 sin ejecutar la búsqueda y sin restos (SC-003)
# ---------------------------------------------------------------------------


def test_post_search_missing_part_returns_400(api_env: Path, search_spy: list) -> None:
    """Petición sin parte `image` → 400 missing_file_part, sin búsqueda (SC-006)."""
    with TestClient(app) as client:
        response = client.post("/search")
    _assert_error_body(response, 400, "missing_file_part")
    assert search_spy == []
    assert list(api_env.iterdir()) == []  # SC-003: sin restos


def test_post_search_empty_filename_returns_400(
    api_env: Path, search_spy: list, tmp_path: Path
) -> None:
    """Parte `image` con nombre vacío → 400 missing_file_part (contracts §5)."""
    content = make_query_image(tmp_path / "query.png").read_bytes()
    with TestClient(app) as client:
        response = client.post(
            "/search",
            files={"image": ("", io.BytesIO(content), "image/png")},
        )
    _assert_error_body(response, 400, "missing_file_part")
    assert search_spy == []
    assert list(api_env.iterdir()) == []


def test_post_search_oversize_returns_413(api_env: Path, search_spy: list) -> None:
    """Media > 10 MB → 413 media_too_large, sin búsqueda y sin restos (SC-003/006)."""
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_QUERY_IMAGE_BYTES + 1)
    with TestClient(app) as client:
        response = client.post(
            "/search",
            files={"image": ("big.png", io.BytesIO(oversize), "image/png")},
        )
    _assert_error_body(response, 413, "media_too_large")
    assert search_spy == []
    assert list(api_env.iterdir()) == []


def test_post_search_unknown_signature_returns_415(
    api_env: Path, search_spy: list, tmp_path: Path
) -> None:
    """Firma MIME no soportada → 415, sin búsqueda y sin restos (SC-003/006)."""
    bogus = make_bogus_file(tmp_path / "fake.png")
    with TestClient(app) as client:
        with bogus.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": ("fake.png", handle, "image/png")},
            )
    _assert_error_body(response, 415, "media_type_not_supported")
    assert search_spy == []
    assert list(api_env.iterdir()) == []


def test_post_search_corrupt_returns_400(api_env: Path, search_spy: list, tmp_path: Path) -> None:
    """Firma válida + contenido corrupto → 400 media_corrupt (contracts §5)."""
    corrupt = make_bogus_file(tmp_path / "corrupt.png", with_png_signature=True)
    with TestClient(app) as client:
        with corrupt.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": ("corrupt.png", handle, "image/png")},
            )
    _assert_error_body(response, 400, "media_corrupt")
    assert search_spy == []
    assert list(api_env.iterdir()) == []


def test_post_search_invalid_form_values_returns_400(
    api_env: Path, search_spy: list, tmp_path: Path
) -> None:
    """top_k/min_score inválidos → 400 invalid_request, sin búsqueda (SC-006)."""
    query = make_query_image(tmp_path / "query.png")
    with TestClient(app) as client:
        for form in (
            {"top_k": "abc"},
            {"top_k": "0"},
            {"top_k": "1001"},
            {"min_score": "oops"},
            {"min_score": "1.5"},
        ):
            with query.open("rb") as handle:
                response = client.post(
                    "/search",
                    files={"image": ("query.png", handle, "image/png")},
                    data=form,
                )
            _assert_error_body(response, 400, "invalid_request")
    assert search_spy == []
    assert list(api_env.iterdir()) == []


def test_post_search_non_multipart_body_returns_400(api_env: Path, search_spy: list) -> None:
    """Body que no es multipart (p. ej. JSON) → 400 missing_file_part (contracts §5)."""
    with TestClient(app) as client:
        response = client.post("/search", json={"top_k": 10})
    _assert_error_body(response, 400, "missing_file_part")
    assert search_spy == []
    assert list(api_env.iterdir()) == []
