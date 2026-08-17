"""Tests de `GET /videos/{id}` (PR-056 · FR-008/011 · SEC-004 · contracts §4/§5).

Dos niveles:
- **Sin BD** (siempre): TestClient con la capa de datos **falsa**
  (monkeypatch de `_fetch_video_card`): ficha 200 con el ejemplo del contracts
  §4, **400** `invalid_uuid` (validación sin tocar la BD, paridad SC-006) y
  **404** `video_not_found`.
- **Con BD** (skipif sin Supabase local): ficha completa con el join a
  `sources` (contracts §4: `source` = `sources.name`), campos nullables para
  un vídeo local, y 404 real. Acceso service-side con RLS deny-by-default
  intacta (SEC-004).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from xtrace_spike.cli import build_backend  # type: ignore[import-untyped]
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]

from xtrace_api.config import get_settings
from xtrace_api.main import app
from xtrace_api.schemas import VideoCard

CONTRACT_CARD = VideoCard(
    video_id="1a2b3c4d-0000-0000-0000-000000000001",
    local_ref="MAYO 2026 (386).mp4",
    title="Video de ejemplo del corpus",
    page_url="https://www.xvideos.com/video.abc123/ejemplo",
    source="xvideos",
    status="indexed",
    duration_ms=483_000,
    frame_count=30,
    tags=["buttfucking"],
    published_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    thumbnail_url="https://thumbs.example.com/t.jpg",
    excluded=False,
)


def _db_available() -> bool:
    """¿Supabase local alcanzable? (mismo patrón que spike/crawler/PR-055)."""
    try:
        conn = psycopg.connect(resolve_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


DB_SKIP = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): integración de /videos saltada",
)


@pytest.fixture
def _postgres_api_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Backend postgres determinista por test: env, work_root y cachés reset."""
    monkeypatch.setenv("SUPABASE_DB_URL", resolve_dsn())
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", str(tmp_path / "work"))
    get_settings.cache_clear()
    build_backend.cache_clear()
    yield tmp_path / "work"
    get_settings.cache_clear()
    build_backend.cache_clear()


@pytest.fixture
def _clean_tables() -> Iterator[None]:
    """Estado DB limpio por test (misma convención que el spike: truncate)."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "truncate table public.frames, public.videos, public.searches, "
                "public.sources cascade"
            )
    yield
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "truncate table public.frames, public.videos, public.searches, "
                "public.sources cascade"
            )


def _seed_source(name: str) -> str:
    """Fuente (`sources`) para el join de la ficha; devuelve su id."""
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.sources (name, adapter, manifest) "
                "values (%s, 'mock', '{}'::jsonb) returning id::text",
                (name,),
            )
            row = cur.fetchone()
    assert row is not None
    return row[0]


def _seed_full_video() -> tuple[str, str]:
    """Vídeo web completo + fuente; devuelve (video_id, source_name)."""
    source_id = _seed_source("xvideos")
    video_id = str(uuid.uuid4())
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.videos "
                "(id, local_ref, title, page_url, source_id, external_id, status, "
                " duration_ms, frame_count, tags, published_at, thumbnail_url, excluded) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    video_id,
                    "MAYO 2026 (386).mp4",
                    "Vídeo web del corpus",
                    "https://www.xvideos.com/video.abc123/ejemplo",
                    source_id,
                    "video.abc123",
                    "indexed",
                    483_000,
                    30,
                    ["buttfucking"],
                    datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
                    "https://thumbs.example.com/t.jpg",
                    False,
                ),
            )
    return video_id, "xvideos"


def _seed_local_video() -> str:
    """Vídeo local mínimo (sin fuente ni metadatos); devuelve su id."""
    video_id = str(uuid.uuid4())
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.videos (id, local_ref) values (%s, %s)",
                (video_id, "MAYO 2026 (386).mp4"),
            )
    return video_id


# ---------------------------------------------------------------------------
# Sin BD: TestClient con la capa de datos falsa (contracts §4/§5)
# ---------------------------------------------------------------------------


def test_video_card_200_contract_example(api_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ficha 200 con el ejemplo íntegro del contracts §4 (FR-008)."""
    monkeypatch.setattr("xtrace_api.routers.videos._fetch_video_card", lambda _: CONTRACT_CARD)

    with TestClient(app) as client:
        response = client.get(f"/videos/{CONTRACT_CARD.video_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "video_id": "1a2b3c4d-0000-0000-0000-000000000001",
        "local_ref": "MAYO 2026 (386).mp4",
        "title": "Video de ejemplo del corpus",
        "page_url": "https://www.xvideos.com/video.abc123/ejemplo",
        "source": "xvideos",
        "status": "indexed",
        "duration_ms": 483_000,
        "frame_count": 30,
        "tags": ["buttfucking"],
        "published_at": "2026-08-10T12:00:00Z",
        "thumbnail_url": "https://thumbs.example.com/t.jpg",
        "excluded": False,
    }


def test_video_card_200_nullable_fields(api_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vídeo local sin fuente/metadatos: los campos nullable van en null (§4)."""
    local = VideoCard(
        video_id="1a2b3c4d-0000-0000-0000-000000000002",
        local_ref="MAYO 2026 (386).mp4",
        status="indexed",
        frame_count=0,
        excluded=False,
    )
    monkeypatch.setattr("xtrace_api.routers.videos._fetch_video_card", lambda _: local)

    with TestClient(app) as client:
        response = client.get(f"/videos/{local.video_id}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] is None
    assert payload["title"] is None
    assert payload["page_url"] is None
    assert payload["duration_ms"] is None
    assert payload["tags"] is None
    assert payload["published_at"] is None
    assert payload["thumbnail_url"] is None
    assert payload["status"] == "indexed"
    assert payload["frame_count"] == 0
    assert payload["excluded"] is False


def test_video_card_400_invalid_uuid_without_db(
    api_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """400 `invalid_uuid` (contracts §5) sin tocar la BD (paridad SC-006)."""
    fetched: list[str] = []

    def never_called(video_id: uuid.UUID) -> VideoCard | None:
        fetched.append(str(video_id))
        return None

    monkeypatch.setattr("xtrace_api.routers.videos._fetch_video_card", never_called)

    with TestClient(app) as client:
        response = client.get("/videos/no-es-un-uuid")

    assert response.status_code == 400
    assert response.json() == {
        "error": "el id del vídeo debe ser un UUID válido",
        "error_type": "invalid_uuid",
    }
    assert fetched == []  # la validación ocurre antes de la consulta


def test_video_card_404_not_found(api_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """404 `video_not_found` con el cuerpo del contracts §5 (FR-008/011)."""
    monkeypatch.setattr("xtrace_api.routers.videos._fetch_video_card", lambda _: None)

    with TestClient(app) as client:
        response = client.get("/videos/1a2b3c4d-0000-0000-0000-000000000099")

    assert response.status_code == 404
    assert response.json() == {
        "error": "el vídeo no existe",
        "error_type": "video_not_found",
    }


def test_video_card_503_without_db(api_env: Path) -> None:
    """Sin BD (backend in-memory): 503 `index_unavailable` sin conectar al DSN.

    Mismo criterio que `record_search` (PR-055): la ficha solo existe en el
    índice postgres; en modo in-memory se responde 503 sin intentar tocar la
    BD por defecto (evita colgarse cuando no hay stack — contracts §5).
    """
    with TestClient(app) as client:
        response = client.get("/videos/1a2b3c4d-0000-0000-0000-000000000001")

    assert response.status_code == 503
    assert response.json() == {
        "error": "la ficha de vídeo requiere la BD del índice (backend in-memory)",
        "error_type": "index_unavailable",
    }


# ---------------------------------------------------------------------------
# Con BD (skipif): ficha real sobre `public.videos` + join `sources`
# ---------------------------------------------------------------------------


@DB_SKIP
def test_video_card_full_record_pg(_postgres_api_env: Path, _clean_tables: Iterator[None]) -> None:
    """Ficha completa contra la BD: metadatos + `source` del join (FR-008 · §4)."""
    video_id, _ = _seed_full_video()

    with TestClient(app) as client:
        response = client.get(f"/videos/{video_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "video_id": video_id,
        "local_ref": "MAYO 2026 (386).mp4",
        "title": "Vídeo web del corpus",
        "page_url": "https://www.xvideos.com/video.abc123/ejemplo",
        "source": "xvideos",
        "status": "indexed",
        "duration_ms": 483_000,
        "frame_count": 30,
        "tags": ["buttfucking"],
        "published_at": "2026-08-10T12:00:00Z",
        "thumbnail_url": "https://thumbs.example.com/t.jpg",
        "excluded": False,
    }


@DB_SKIP
def test_video_card_local_video_nullable_pg(
    _postgres_api_env: Path, _clean_tables: Iterator[None]
) -> None:
    """Vídeo local (sin fuente): `source` y metadatos nullable en null (§4)."""
    video_id = _seed_local_video()

    with TestClient(app) as client:
        response = client.get(f"/videos/{video_id}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["video_id"] == video_id
    assert payload["local_ref"] == "MAYO 2026 (386).mp4"
    assert payload["source"] is None
    assert payload["title"] is None
    assert payload["page_url"] is None
    assert payload["duration_ms"] is None
    assert payload["tags"] is None
    assert payload["published_at"] is None
    assert payload["thumbnail_url"] is None
    assert payload["status"] == "discovered"
    assert payload["frame_count"] == 0
    assert payload["excluded"] is False


@DB_SKIP
def test_video_card_404_pg(_postgres_api_env: Path, _clean_tables: Iterator[None]) -> None:
    """404 real: el id no existe en `public.videos` (FR-008/011)."""
    missing = str(uuid.uuid4())

    with TestClient(app) as client:
        response = client.get(f"/videos/{missing}")

    assert response.status_code == 404
    assert response.json() == {
        "error": "el vídeo no existe",
        "error_type": "video_not_found",
    }
