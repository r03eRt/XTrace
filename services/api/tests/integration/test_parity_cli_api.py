"""Paridad CLI-API (PR-055 · FR-005 · SC-001 · contracts §7.2).

La **misma imagen** por la cadena de la CLI (`xtrace-spike search` vía
Typer CliRunner) y por `POST /search` (FastAPI TestClient) sobre el **mismo
índice** devuelve los mismos vídeos, en el mismo orden y con los mismos
`match_score` (contracts §7.2: campos del contrato CLI; se ignora la
extensión MAY `title`/`page_url` de la API). Comparación con redondeo
estable (floats del ranking).

Batería: ≥ 5 imágenes representativas (SC-001) + 1 negativa no indexada, con
defaults (top_k=10, min_score=0.0) y un caso con opciones personalizadas
(top_k=3, min_score=0.5).

Backends (misma regla que la CLI): **in-memory** siempre (CI sin DB) y
**postgres** cuando Supabase local está disponible (skipif; truncate de las
tablas del test, convención del spike).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner
from xtrace_spike.cli import app as cli_app  # type: ignore[import-untyped]
from xtrace_spike.cli import build_backend  # type: ignore[import-untyped]
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider  # type: ignore[import-untyped]
from xtrace_spike.hashing.phash import compute_phash  # type: ignore[import-untyped]
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]
from xtrace_spike.vectorstore.base import FrameRecord  # type: ignore[import-untyped]

from tests.fixtures import EMBEDDING_DIMENSION, make_query_image, rgb_of
from xtrace_api.config import get_settings
from xtrace_api.main import app

#: Tamaños deterministas de las ≥ 5 imágenes representativas (SC-001).
_QUERY_SIZES: tuple[tuple[int, int], ...] = (
    (64, 48),
    (48, 64),
    (80, 60),
    (60, 80),
    (96, 72),
)


def _db_available() -> bool:
    """¿Supabase local alcanzable? (mismo patrón que spike/crawler)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


def _embedding_of(image: Path) -> list[float]:
    """Embedding determinista de la imagen (mismo proveedor que CLI/API)."""
    vector = FakeEmbeddingProvider(dimension=EMBEDDING_DIMENSION).embed_images([rgb_of(image)])[0]
    return [float(value) for value in vector]


def _seed_frame(image: Path, *, timestamp_ms: int) -> tuple[str, str]:
    """Siembra un frame idéntico a la imagen en el backend activo (in-memory o pg).

    El backend lo resuelve `build_backend()` (misma regla que CLI/API): con
    `SUPABASE_DB_URL` definida indexa en postgres (creando el vídeo por FK);
    sin ella, en memoria. Devuelve (video_id, frame_id).
    """
    backend = build_backend()
    video_id = str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    record = FrameRecord(
        frame_id=frame_id,
        video_id=video_id,
        timestamp_ms=timestamp_ms,
        phash=compute_phash(rgb_of(image)),
        embedding=_embedding_of(image),
    )
    asyncio.run(backend.store.upsert_frames([record]))
    return video_id, frame_id


def _cli_search(image: Path, *options: str) -> dict[str, Any]:
    """Ejecuta `xtrace-spike search` sobre la imagen (la CLI borra la media)."""
    result = CliRunner().invoke(cli_app, ["search", "--image", str(image), *options])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def _api_search(image: Path, **form: object) -> dict[str, Any]:
    """Ejecuta POST /search sobre la imagen (la media original no se toca)."""
    with TestClient(app) as client:
        with image.open("rb") as handle:
            response = client.post(
                "/search",
                files={"image": (image.name, handle, "image/png")},
                data=form,
            )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_same_results(cli_payload: dict[str, Any], api_payload: dict[str, Any]) -> None:
    """Compara los campos CLI de ambos JSON (contracts §7.2; redondeo estable)."""
    cli_results = cli_payload["results"]
    api_results = api_payload["results"]
    assert len(api_results) == len(cli_results), (cli_results, api_results)
    for cli_item, api_item in zip(cli_results, api_results, strict=True):
        assert api_item["video_id"] == cli_item["video_id"]
        assert api_item["local_ref"] == cli_item["local_ref"]
        assert round(api_item["match_score"], 6) == round(cli_item["match_score"], 6)
        assert api_item["matching_frames"] == cli_item["matching_frames"]
        assert api_item["match_timestamp_ms"] == cli_item["match_timestamp_ms"]
        assert round(api_item["evidence"]["visual"], 6) == round(cli_item["evidence"]["visual"], 6)
        assert round(api_item["evidence"]["phash"], 6) == round(cli_item["evidence"]["phash"], 6)


def _run_parity(work_dir: Path) -> None:
    """Batería SC-001: 6 consultas (5 indexadas + 1 negativa) por CLI y API."""
    images: list[Path] = []
    for index, size in enumerate(_QUERY_SIZES):
        image = make_query_image(work_dir / f"query-{index}.png", size=size)
        _seed_frame(image, timestamp_ms=94_000 + index * 1_000)
        images.append(image)
    negative = make_query_image(work_dir / "negative.png", size=(40, 40))

    for image in [*images, negative]:
        cli_image = work_dir / f"cli-{image.stem}.png"
        shutil.copyfile(image, cli_image)  # la CLI borra la media tras procesar
        cli_payload = _cli_search(cli_image)
        api_payload = _api_search(image)
        assert set(api_payload) == {"search_id", "processing_ms", "results"}
        _assert_same_results(cli_payload, api_payload)


def test_parity_cli_api_in_memory(api_env: Path) -> None:
    """SC-001 en CI (sin DB): backend in-memory compartido CLI-API."""
    _run_parity(api_env)


@pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): paridad postgres saltada",
)
def test_parity_cli_api_postgres(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SC-001 contra el índice real (backend postgres, env SUPABASE_DB_URL)."""
    monkeypatch.setenv("SUPABASE_DB_URL", resolve_dsn())
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", str(tmp_path / "work"))
    get_settings.cache_clear()
    build_backend.cache_clear()
    try:
        with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("truncate table public.frames, public.videos, public.searches cascade")
        _run_parity(tmp_path)
    finally:
        with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("truncate table public.frames, public.videos, public.searches cascade")
        get_settings.cache_clear()
        build_backend.cache_clear()


def test_parity_cli_api_custom_options(api_env: Path) -> None:
    """top_k/min_score personalizados se propagan igual en CLI y API."""
    image_a = make_query_image(api_env / "a.png")
    image_b = make_query_image(api_env / "b.png", size=(80, 60))
    _seed_frame(image_a, timestamp_ms=10_000)
    _seed_frame(image_b, timestamp_ms=20_000)

    # min_score 0.9: solo la coincidencia exacta (1.0) supera el umbral; la
    # segunda (comparte el patrón de píxeles → ~0.79) queda filtrada.
    options = ("--top-k", "3", "--min-score", "0.9")
    cli_image = api_env / "cli-a.png"
    shutil.copyfile(image_a, cli_image)
    cli_payload = _cli_search(cli_image, *options)
    api_payload = _api_search(image_a, top_k="3", min_score="0.9")
    assert len(api_payload["results"]) == 1  # solo la coincidencia exacta ≥ 0.9
    _assert_same_results(cli_payload, api_payload)
