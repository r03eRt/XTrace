"""Smoke tests del bootstrap del servicio API (PR-054 · FR-006 · SEC-001/006 · ADR-0012).

Validan el scaffolding y la toolchain: paquete importable, dependencia
editable al spike resoluble (ADR-0011/0012), contrato de `GET /health`
(contracts §2, FR-006 — no depende de la BD) y defaults de configuración
base por env (SEC-006, NFR-003).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import xtrace_spike
from fastapi.testclient import TestClient

import xtrace_api
from xtrace_api.config import Settings, get_settings
from xtrace_api.main import app


def test_package_importable() -> None:
    """El paquete `xtrace_api` importa y expone versión (FR-006, base)."""
    assert isinstance(xtrace_api.__version__, str)
    assert xtrace_api.__version__


def test_spike_editable_dependency_resolvable() -> None:
    """`xtrace_spike` se resuelve desde la API (ADR-0011/0012, NFR-003)."""
    assert isinstance(xtrace_spike.__version__, str)
    assert xtrace_spike.__version__


def test_health_returns_contract() -> None:
    """`GET /health` responde 200 con el contrato §2 estable (FR-006).

    No depende de la BD: se verifica con `SUPABASE_DB_URL` vacía.
    """
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "xtrace-api",
        "version": xtrace_api.__version__,
    }
    assert response.headers["content-type"].startswith("application/json")


def test_config_defaults() -> None:
    """Defaults base de la configuración por env (SEC-006, NFR-003)."""
    settings = Settings()
    assert settings.host == "127.0.0.1"  # SEC-001: bind solo local
    assert settings.port == 8000
    assert settings.supabase_db_url == ""
    assert settings.embedding_provider == "fake"
    assert settings.work_root == Path(tempfile.gettempdir()) / "xtrace-api"
    assert settings.cors_origins == ["http://localhost:3000"]


def test_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env `XTRACE_API_*` y convenios compartidos del repo se aplican (SEC-006)."""
    monkeypatch.setenv("XTRACE_API_HOST", "127.0.0.2")
    monkeypatch.setenv("XTRACE_API_PORT", "9000")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://127.0.0.1:55322/xtrace")
    monkeypatch.setenv("XTRACE_EMBEDDING_PROVIDER", "siglip")
    monkeypatch.setenv("XTRACE_API_WORK_ROOT", "/tmp/xtrace-api-test")
    monkeypatch.setenv("XTRACE_API_CORS_ORIGINS", '["http://localhost:3001"]')

    settings = Settings()
    assert settings.host == "127.0.0.2"
    assert settings.port == 9000
    assert settings.supabase_db_url == "postgresql://127.0.0.1:55322/xtrace"
    assert settings.embedding_provider == "siglip"
    assert settings.work_root == Path("/tmp/xtrace-api-test")
    assert settings.cors_origins == ["http://localhost:3001"]


def test_get_settings_is_cached() -> None:
    """`get_settings()` devuelve una única instancia por proceso (patrón del repo)."""
    assert get_settings() is get_settings()
