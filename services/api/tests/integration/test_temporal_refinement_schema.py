"""Postgres contract tests for temporal-refinement telemetry (TASK-006-T021).

These tests use only UUIDs created by the test and skip when Supabase local is
not reachable.  They exercise the server-side connection positively and the
``anon``/``authenticated`` roles negatively; no client role receives access to
the telemetry tables (DATA-002/003, SEC-005).

The retention assertion mirrors the existing ``searches`` TTL policy: purging
an expired search must cascade through its summary and evidence while a recent
search remains.  The test also asserts that query/media byte columns do not
exist; ``bytes_downloaded`` is an aggregate asset metric, not persisted media.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import uuid
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from xtrace_spike.repo import resolve_dsn  # type: ignore[import-untyped]


def _db_available() -> bool:
    try:
        with psycopg.connect(resolve_dsn(), connect_timeout=2) as conn:
            conn.execute("select 1")
        return True
    except Exception:
        return False


DB_SKIP = pytest.mark.skipif(
    not _db_available(),
    reason="Supabase local no alcanzable (CI sin DB): esquema de refinamiento saltado",
)


@dataclass
class _OwnedRows:
    search_ids: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)


@pytest.fixture
def _owned_rows() -> Iterator[_OwnedRows]:
    """Remove only rows created by this test, including cascade children."""

    owned = _OwnedRows()
    yield owned
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            for search_id in owned.search_ids:
                cur.execute("delete from public.searches where id = %s", (search_id,))
            for video_id in owned.video_ids:
                cur.execute("delete from public.videos where id = %s", (video_id,))


def _analytics_module() -> Any:
    """Load either allowed location for the T023 ``record_refinement`` writer."""

    for module_name in ("xtrace_api.refinement.analytics", "xtrace_api.analytics"):
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


def _record_refinement(
    module: Any,
    *,
    search_id: str,
    status: str,
    evidence: Sequence[Mapping[str, Any]] = (),
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
) -> None:
    result = module.record_refinement(
        search_id=search_id,
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


def _seed_search_and_video(owned: _OwnedRows, *, created_at_sql: str = "now()") -> tuple[str, str]:
    search_id = str(uuid.uuid4())
    video_id = str(uuid.uuid4())
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into public.searches "
                "(id, search_type, processing_ms, results_count, created_at) "
                f"values (%s, 'image', 10, 1, {created_at_sql})",
                (search_id,),
            )
            cur.execute(
                "insert into public.videos (id, local_ref, duration_ms) values (%s, %s, %s)",
                (video_id, f"task-006-t021-{video_id}.mp4", 600_000),
            )
    owned.search_ids.append(search_id)
    owned.video_ids.append(video_id)
    return search_id, video_id


def _evidence(
    video_id: str, *, asset_hash: str = "sha256:t021-frame-12"
) -> tuple[dict[str, Any], ...]:
    return (
        {
            "video_id": video_id,
            "source": "xvideos",
            "candidate_rank": 1,
            "asset_kind": "thumbnail",
            "asset_url": "https://cdn.example/t021/frame-12.jpg",
            "asset_url_hash": asset_hash,
            "position": 12,
            "timestamp_ms": 454_000,
            "similarity": 0.99,
            "selected": True,
            "discarded_reason": None,
        },
    )


@DB_SKIP
def test_record_refinement_is_idempotent_and_keeps_aggregate_counters(
    _owned_rows: _OwnedRows,
) -> None:
    """FR-011/FR-013: one search identity, upserted summary, no duplicate evidence."""

    module = _analytics_module()
    search_id, video_id = _seed_search_and_video(_owned_rows)
    evidence = _evidence(video_id)

    _record_refinement(
        module,
        search_id=search_id,
        status="completed",
        evidence=evidence,
        candidates_requested=3,
        candidates_processed=2,
        assets_requested=4,
        assets_evaluated=3,
        assets_discarded=1,
        bytes_downloaded=4096,
        embedding_count=3,
        embedding_elapsed_ms=72,
        improved_count=1,
        unchanged_count=1,
    )
    # A retry may finish with a bounded/limited status; the same search must
    # update the summary rather than create a second row.
    _record_refinement(
        module,
        search_id=search_id,
        status="limited",
        evidence=evidence,
        candidates_requested=3,
        candidates_processed=2,
        assets_requested=4,
        assets_evaluated=3,
        assets_discarded=1,
        bytes_downloaded=4096,
        embedding_count=3,
        embedding_elapsed_ms=72,
        errors_count=1,
        improved_count=0,
        unchanged_count=1,
        limit_reason="search_timeout",
    )

    with psycopg.connect(resolve_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select status, candidates_requested, candidates_processed, "
                "assets_requested, assets_evaluated, assets_discarded, "
                "bytes_downloaded, embedding_count, embedding_elapsed_ms, errors_count, "
                "improved_count, unchanged_count, limit_reason "
                "from public.search_refinements where search_id = %s",
                (search_id,),
            )
            rows = cur.fetchall()
            cur.execute(
                "select count(*) from public.search_refinement_evidence where search_id = %s",
                (search_id,),
            )
            evidence_count = cur.fetchone()

    assert len(rows) == 1
    assert rows[0] == (
        "limited",
        3,
        2,
        4,
        3,
        1,
        4096,
        3,
        72,
        1,
        0,
        1,
        "search_timeout",
    )
    assert evidence_count is not None and evidence_count[0] == 1


@DB_SKIP
def test_schema_has_no_query_or_media_bytes_and_enforces_statuses_and_rls(
    _owned_rows: _OwnedRows,
) -> None:
    """DATA-002/003/SEC-005: checks, no media persistence, and deny-by-default RLS."""

    required_summary_columns = {
        "search_id",
        "status",
        "policy_version",
        "candidates_requested",
        "candidates_processed",
        "assets_requested",
        "assets_evaluated",
        "assets_discarded",
        "bytes_downloaded",
        "embedding_count",
        "embedding_elapsed_ms",
        "errors_count",
        "improved_count",
        "unchanged_count",
        "elapsed_ms",
        "limit_reason",
        "created_at",
        "finished_at",
    }
    forbidden_columns = {
        "query_bytes",
        "media_bytes",
        "query_image",
        "query_payload",
        "media_blob",
        "video_bytes",
    }
    with psycopg.connect(resolve_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select table_name, column_name from information_schema.columns "
                "where table_schema = 'public' and table_name in "
                "('search_refinements', 'search_refinement_evidence')"
            )
            columns = {(table, column) for table, column in cur.fetchall()}
            summary_columns = {column for table, column in columns if table == "search_refinements"}
            assert required_summary_columns <= summary_columns
            assert not forbidden_columns & {column for _, column in columns}

            cur.execute(
                "select c.relname, c.relrowsecurity "
                "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relname in "
                "('search_refinements', 'search_refinement_evidence') "
                "order by c.relname"
            )
            assert cur.fetchall() == [
                ("search_refinement_evidence", True),
                ("search_refinements", True),
            ]
            cur.execute(
                "select tablename, count(*) from pg_policies "
                "where schemaname = 'public' and tablename in "
                "('search_refinements', 'search_refinement_evidence') "
                "group by tablename"
            )
            assert cur.fetchall() == []
            cur.execute(
                "select has_table_privilege('anon', 'public.search_refinements', 'SELECT'), "
                "has_table_privilege("
                "'authenticated', 'public.search_refinement_evidence', 'SELECT')"
            )
            assert cur.fetchone() == (False, False)

    # Server-side connection is the positive path: it can seed a valid row.
    search_id, video_id = _seed_search_and_video(_owned_rows)
    module = _analytics_module()
    _record_refinement(
        module,
        search_id=search_id,
        status="completed",
        evidence=_evidence(video_id),
    )
    with psycopg.connect(resolve_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select status, bytes_downloaded from public.search_refinements "
                "where search_id = %s",
                (search_id,),
            )
            assert cur.fetchone() == ("completed", 184_320)

    # Client roles are negative paths even when the server connection owns the
    # fixture rows.  ``set local role`` keeps the role change transaction-local.
    for role in ("anon", "authenticated"):
        # Run each denied statement in its own transaction: PostgreSQL marks
        # a transaction failed after the first privilege error, so a second
        # assertion must not reuse that aborted transaction.
        for statement, params in (
            ("select count(*) from public.search_refinements", ()),
            (
                "insert into public.search_refinements (search_id, policy_version) "
                "values (%s, 'client')",
                (search_id,),
            ),
        ):
            with psycopg.connect(resolve_dsn()) as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(f"set local role {role}")
                        with pytest.raises(psycopg.errors.InsufficientPrivilege):
                            cur.execute(statement, params)


@DB_SKIP
def test_expired_search_retention_cascades_summary_and_evidence(
    _owned_rows: _OwnedRows,
) -> None:
    """SEC-005: TTL purge removes expired telemetry but retains a recent search."""

    module = _analytics_module()
    expired_search, expired_video = _seed_search_and_video(
        _owned_rows, created_at_sql="now() - interval '31 days'"
    )
    recent_search, recent_video = _seed_search_and_video(_owned_rows)
    _record_refinement(
        module,
        search_id=expired_search,
        status="completed",
        evidence=_evidence(expired_video, asset_hash="sha256:expired"),
    )
    _record_refinement(
        module,
        search_id=recent_search,
        status="completed",
        evidence=_evidence(recent_video, asset_hash="sha256:recent"),
    )

    # This is the SQL predicate used by the existing searches TTL worker.  The
    # FK cascade must make the telemetry disappear with its parent search.
    with psycopg.connect(resolve_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "delete from public.searches "
                "where id = %s and created_at < now() - interval '30 days'",
                (expired_search,),
            )
            cur.execute(
                "select count(*) from public.search_refinements where search_id = %s",
                (expired_search,),
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "select count(*) from public.search_refinement_evidence where search_id = %s",
                (expired_search,),
            )
            assert cur.fetchone() == (0,)
            cur.execute(
                "select count(*) from public.search_refinements where search_id = %s",
                (recent_search,),
            )
            assert cur.fetchone() == (1,)
            cur.execute(
                "select count(*) from public.search_refinement_evidence where search_id = %s",
                (recent_search,),
            )
            assert cur.fetchone() == (1,)
