"""Tests del TTL de `searches` (PR-056 · FR-012 · DATA-001 · data-model.md).

El TTL se implementa **sin migración** como cleanup por `created_at`:
`delete_expired_searches` borra solo las filas vencidas
(`created_at < now() - make_interval(days => <ttl_days>)`) y conserva las
recientes — el SQL del data-model.md se verifica con un **repo fake** (la
frontera de BD inyectable) y la semántica real (filas viejas borradas,
recientes intactas) en la integración con BD (`tests/integration/test_stats.py`).

Cobertura:
- `delete_expired_searches` con repo fake: SQL con la condición TTL, el
  `ttl_days` como parámetro y el `rowcount` devuelto.
- `searches_ttl_round`: best-effort — un fallo de BD se loguea (warning) y
  no lanza (el loop del lifespan nunca muere).
- Límites de configuración (`searches_ttl_days`, `searches_ttl_cleanup_min`):
  defaults del data-model.md y rechazo de valores fuera de rango (pydantic).
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from xtrace_api.analytics import delete_expired_searches, searches_ttl_round
from xtrace_api.config import Settings


class _FakeCursor:
    """Cursor fake: captura el SQL y los parámetros del cleanup (sin BD)."""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.sql: str | None = None
        self.params: tuple[Any, ...] | None = None

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.sql = sql
        self.params = params


class _FakeConn:
    """Conexión fake con un cursor que captura la sentencia ejecutada."""

    def __init__(self, rowcount: int) -> None:
        self.cursor_obj = _FakeCursor(rowcount)

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:  # noqa: D401 - `cursor()` es síncrono en psycopg
        return self.cursor_obj


class _FakeRepo:
    """Repo fake: `connect()` devuelve una conexión que captura el SQL."""

    def __init__(self, rowcount: int = 7) -> None:
        self.conn = _FakeConn(rowcount)

    async def connect(self) -> _FakeConn:
        return self.conn


def test_delete_expired_searches_uses_ttl_sql_with_fake_repo() -> None:
    """El cleanup borra solo vencidas: SQL del data-model.md con `ttl_days` (FR-012).

    La condición `created_at < now() - make_interval(days => %s)` es la que
    garantiza que las filas recientes se conservan (la semántica real se
    verifica en la integración con BD).
    """
    repo = _FakeRepo(rowcount=3)

    deleted = asyncio.run(delete_expired_searches(ttl_days=30, repo=repo))

    assert deleted == 3  # rowcount devuelto (filas borradas)
    assert repo.conn.cursor_obj.sql == (
        "delete from public.searches where created_at < now() - make_interval(days => %s)"
    )
    assert repo.conn.cursor_obj.params == (30,)


def test_delete_expired_searches_no_expired_rows_returns_zero() -> None:
    """Sin filas vencidas → el cleanup devuelve 0 (y no toca las recientes)."""
    repo = _FakeRepo(rowcount=0)

    deleted = asyncio.run(delete_expired_searches(ttl_days=30, repo=repo))

    assert deleted == 0


def test_searches_ttl_round_survives_db_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Best-effort: fallo de BD → warning y sin excepción (el loop no muere)."""

    async def boom(ttl_days: int, *, repo: object = None) -> int:
        raise psycopg.OperationalError("BD caída")

    monkeypatch.setattr("xtrace_api.analytics.delete_expired_searches", boom)
    settings = Settings()

    with caplog.at_level("WARNING", logger="xtrace_api.analytics"):
        asyncio.run(searches_ttl_round(settings))  # no debe lanzar

    assert any("TTL de searches" in record.message for record in caplog.records)


def test_searches_ttl_round_passes_configured_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una iteración aplica el TTL configurado (`searches_ttl_days`)."""
    seen: list[int] = []

    async def fake_delete(ttl_days: int, *, repo: object = None) -> int:
        seen.append(ttl_days)
        return 1

    monkeypatch.setattr("xtrace_api.analytics.delete_expired_searches", fake_delete)
    settings = Settings(searches_ttl_days=45)

    asyncio.run(searches_ttl_round(settings))

    assert seen == [45]


def test_ttl_config_defaults() -> None:
    """Defaults del data-model.md: TTL 30 días, cleanup cada 60 minutos."""
    settings = Settings()
    assert settings.searches_ttl_days == 30
    assert settings.searches_ttl_cleanup_min == 60


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"searches_ttl_days": 0}, "searches_ttl_days"),
        ({"searches_ttl_days": -1}, "searches_ttl_days"),
        ({"searches_ttl_cleanup_min": 0}, "searches_ttl_cleanup_min"),
        ({"searches_ttl_cleanup_min": -60}, "searches_ttl_cleanup_min"),
    ],
)
def test_ttl_config_rejects_out_of_range(kwargs: dict[str, int], field: str) -> None:
    """Límites del TTL: valores fuera de rango se rechazan (pydantic)."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(**kwargs)
    assert field in str(exc_info.value)


def test_ttl_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env `XTRACE_API_SEARCHES_TTL_*` sobreescribe los defaults (data-model.md)."""
    monkeypatch.setenv("XTRACE_API_SEARCHES_TTL_DAYS", "45")
    monkeypatch.setenv("XTRACE_API_SEARCHES_TTL_CLEANUP_MIN", "120")

    settings = Settings()

    assert settings.searches_ttl_days == 45
    assert settings.searches_ttl_cleanup_min == 120
