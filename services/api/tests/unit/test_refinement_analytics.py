"""Tests first for temporal-refinement analytics (TASK-006-T021).

The analytics writer is intentionally loaded lazily.  T021 owns the contract
tests, while T023 will provide the implementation; until then each test fails
with an actionable message instead of making test collection fail with an
opaque import error.

The contract exercised here is deliberately server-side and aggregate-only:
``record_refinement`` writes one summary row per ``search_id`` and optional
sanitised evidence rows.  It never receives query/media bytes and must use an
upsert so retrying the same refinement remains idempotent (FR-011/013,
DATA-002/003, SEC-005).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

_SEARCH_ID = "00000000-0000-0000-0000-000000000621"
_VIDEO_ID = "00000000-0000-0000-0000-000000000622"
_STATUS_VALUES = ("completed", "disabled", "unavailable", "limited", "failed")


@dataclass(frozen=True)
class _Statement:
    sql: str
    params: tuple[Any, ...]


class _FakeCursor:
    """Async psycopg-shaped cursor that records SQL without a database."""

    def __init__(self) -> None:
        self.statements: list[_Statement] = []
        self.rowcount = 1

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self.statements.append(_Statement(sql, tuple(params or ())))

    async def executemany(self, sql: str, params_seq: Iterable[Sequence[Any]]) -> None:
        for params in params_seq:
            await self.execute(sql, params)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj


class _FakeRepo:
    def __init__(self) -> None:
        self.connection = _FakeConnection()

    async def connect(self) -> _FakeConnection:
        return self.connection


def _analytics_module() -> Any:
    """Return the module implementing ``record_refinement``.

    T023 may keep the existing public analytics module or put the refinement
    writer next to the refinement package.  Both locations are allowed by the
    task contract, so tests accept either while still failing explicitly when
    neither implementation exists.
    """

    module_names = ("xtrace_api.refinement.analytics", "xtrace_api.analytics")
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        if callable(getattr(module, "record_refinement", None)):
            return module
    pytest.fail(
        "record_refinement todavía no existe; implementar TASK-006-T023 "
        "antes de cerrar los tests de TASK-006-T021"
    )


def _invoke_record_refinement(
    module: Any,
    *,
    status: str,
    policy_version: str = "temporal-refinement-v1",
    candidates_requested: int = 3,
    candidates_processed: int = 2,
    assets_requested: int = 4,
    assets_evaluated: int = 3,
    assets_discarded: int = 1,
    bytes_downloaded: int = 184_320,
    embedding_count: int = 3,
    embedding_elapsed_ms: int = 72,
    errors_count: int = 0,
    improved_count: int = 1,
    unchanged_count: int = 1,
    elapsed_ms: int = 940,
    limit_reason: str | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Call the public sync/async contract and support either implementation."""

    record_refinement = module.record_refinement
    result = record_refinement(
        search_id=_SEARCH_ID,
        status=status,
        policy_version=policy_version,
        candidates_requested=candidates_requested,
        candidates_processed=candidates_processed,
        assets_requested=assets_requested,
        assets_evaluated=assets_evaluated,
        assets_discarded=assets_discarded,
        bytes_downloaded=bytes_downloaded,
        embedding_count=embedding_count,
        embedding_elapsed_ms=embedding_elapsed_ms,
        errors_count=errors_count,
        improved_count=improved_count,
        unchanged_count=unchanged_count,
        elapsed_ms=elapsed_ms,
        limit_reason=limit_reason,
        evidence=evidence,
    )
    if inspect.isawaitable(result):
        asyncio.run(_await_result(result))


async def _await_result(result: Awaitable[Any]) -> Any:
    return await result


def _patch_repo(module: Any, repo: _FakeRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake server-side repository used by the unit tests."""

    monkeypatch.setattr(module, "PgRepo", lambda: repo, raising=False)


def _summary_statement(cursor: _FakeCursor) -> _Statement:
    summary = [
        statement
        for statement in cursor.statements
        if "search_refinements" in statement.sql.lower()
        and "search_refinement_evidence" not in statement.sql.lower()
    ]
    assert summary, "record_refinement debe escribir search_refinements"
    return summary[0]


def _insert_columns(statement: _Statement) -> list[str]:
    """Extract positional INSERT columns for stable assertions on SQL shape."""

    match = re.search(
        r"insert\s+into\s+[^()]+\((?P<columns>[^)]+)\)\s*values",
        statement.sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match, f"no se pudo leer el INSERT de analítica: {statement.sql!r}"
    return [column.strip().strip('"').lower() for column in match.group("columns").split(",")]


def _column_values(statement: _Statement) -> dict[str, Any]:
    columns = _insert_columns(statement)
    assert len(columns) == len(statement.params), (
        f"columnas y parámetros desalineados: {columns!r} / {statement.params!r}"
    )
    return dict(zip(columns, statement.params, strict=True))


def test_record_refinement_writes_all_counters_and_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-011/DATA-002: counters and every summary status are persisted."""

    module = _analytics_module()
    for status in _STATUS_VALUES:
        repo = _FakeRepo()
        _patch_repo(module, repo, monkeypatch)

        _invoke_record_refinement(
            module,
            status=status,
            candidates_requested=5,
            candidates_processed=3,
            assets_requested=12,
            assets_evaluated=8,
            assets_discarded=4,
            bytes_downloaded=4096,
            embedding_count=8,
            embedding_elapsed_ms=71,
            errors_count=2,
            improved_count=1,
            unchanged_count=2,
            elapsed_ms=933,
            limit_reason="candidate_timeout" if status == "limited" else None,
        )

        values = _column_values(_summary_statement(repo.connection.cursor_obj))
        assert values["search_id"] == _SEARCH_ID or str(values["search_id"]) == _SEARCH_ID
        assert values["status"] == status
        assert values["policy_version"] == "temporal-refinement-v1"
        assert values["candidates_requested"] == 5
        assert values["candidates_processed"] == 3
        assert values["assets_requested"] == 12
        assert values["assets_evaluated"] == 8
        assert values["assets_discarded"] == 4
        assert values["bytes_downloaded"] == 4096
        assert values["embedding_count"] == 8
        assert values["embedding_elapsed_ms"] == 71
        assert values["errors_count"] == 2
        assert values["improved_count"] == 1
        assert values["unchanged_count"] == 2
        assert values["elapsed_ms"] == 933


def test_record_refinement_uses_idempotent_upsert_for_same_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-013: retries use one summary row and update the same search identity."""

    module = _analytics_module()
    repo = _FakeRepo()
    _patch_repo(module, repo, monkeypatch)

    _invoke_record_refinement(module, status="completed", improved_count=1)
    _invoke_record_refinement(
        module,
        status="limited",
        improved_count=0,
        limit_reason="search_timeout",
    )

    summary_statements = [
        statement
        for statement in repo.connection.cursor_obj.statements
        if "search_refinements" in statement.sql.lower()
        and "search_refinement_evidence" not in statement.sql.lower()
    ]
    assert len(summary_statements) == 2
    assert all(
        "on conflict" in statement.sql.lower() and "search_id" in statement.sql.lower()
        for statement in summary_statements
    ), "el summary debe usar ON CONFLICT(search_id) para ser idempotente"
    first = _column_values(summary_statements[0])
    second = _column_values(summary_statements[1])
    assert str(first["search_id"]) == _SEARCH_ID
    assert str(second["search_id"]) == _SEARCH_ID
    assert first["status"] == "completed"
    assert second["status"] == "limited"


def test_record_refinement_persists_sanitised_evidence_without_query_or_media_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DATA-003/SEC-005: evidence is metadata only, never query/media bytes."""

    module = _analytics_module()
    repo = _FakeRepo()
    _patch_repo(module, repo, monkeypatch)
    evidence = (
        {
            "video_id": _VIDEO_ID,
            "source": "xvideos",
            "candidate_rank": 1,
            "asset_kind": "thumbnail",
            "asset_url": "https://cdn.example/assets/frame-12.jpg",
            "asset_url_hash": "sha256:frame-12",
            "position": 12,
            "timestamp_ms": 454_000,
            "similarity": 0.99,
            "selected": True,
            "discarded_reason": None,
        },
    )

    _invoke_record_refinement(module, status="completed", evidence=evidence)

    evidence_statements = [
        statement
        for statement in repo.connection.cursor_obj.statements
        if "search_refinement_evidence" in statement.sql.lower()
    ]
    assert evidence_statements, "record_refinement debe persistir la evidencia resumida"
    for statement in repo.connection.cursor_obj.statements:
        sql = statement.sql.lower()
        assert "query_bytes" not in sql
        assert "media_bytes" not in sql
        assert "query_image" not in sql
        assert "media_blob" not in sql
        assert all(
            not isinstance(value, (bytes, bytearray, memoryview)) for value in statement.params
        )

    evidence_values = _column_values(evidence_statements[0])
    assert (
        evidence_values["search_id"] == _SEARCH_ID
        or str(evidence_values["search_id"]) == _SEARCH_ID
    )
    assert evidence_values["video_id"] == _VIDEO_ID or str(evidence_values["video_id"]) == _VIDEO_ID
    assert evidence_values["asset_kind"] == "thumbnail"
    assert evidence_values["timestamp_ms"] == 454_000
    assert evidence_values["selected"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidates_requested", -1),
        ("candidates_processed", -1),
        ("assets_requested", -1),
        ("assets_evaluated", -1),
        ("assets_discarded", -1),
        ("bytes_downloaded", -1),
        ("embedding_count", -1),
        ("embedding_elapsed_ms", -1),
        ("errors_count", -1),
        ("improved_count", -1),
        ("unchanged_count", -1),
        ("elapsed_ms", -1),
    ],
)
def test_record_refinement_rejects_negative_counter(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    """DATA-002: invalid counters fail closed before they reach persistence."""

    module = _analytics_module()
    repo = _FakeRepo()
    _patch_repo(module, repo, monkeypatch)

    with pytest.raises((ValueError, TypeError)):
        overrides: dict[str, Any] = {"status": "completed", field: value}
        _invoke_record_refinement(module, **overrides)
