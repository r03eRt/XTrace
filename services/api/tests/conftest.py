"""Fixtures compartidas de los tests del servicio API (PR-055).

Aislamiento por test (constitución §6): sin `SUPABASE_DB_URL` el backend del
índice es in-memory (no toca DB) y sin `XTRACE_EMBEDDING_PROVIDER` el
proveedor es Fake (no toca Torch); `work_root` por test (verificable para
SC-003). Se resetean las cachés de configuración (`get_settings`) y del
backend del spike (`build_backend`), la misma práctica que los tests de la
CLI (`build_backend.cache_clear()`).

La fixture no es autouse: los tests del bootstrap (PR-054) verifican los
defaults de configuración sin env y no deben verse afectados.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from xtrace_spike.cli import build_backend  # type: ignore[import-untyped]

from xtrace_api.config import get_settings


@pytest.fixture
def api_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Entorno determinista por test: env limpio, work_root por test, cachés reset.

    Devuelve el `work_root` del test (para verificar SC-003: sin restos de
    media tras las búsquedas).
    """
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", str(tmp_path / "work"))
    get_settings.cache_clear()
    build_backend.cache_clear()
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)  # los helpers de fixture escriben aquí
    yield work
    get_settings.cache_clear()
    build_backend.cache_clear()
