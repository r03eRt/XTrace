"""Server-side telemetry writer for temporal refinement.

Only aggregate counters and a small, sanitised description of evaluated public
assets are persisted.  The query image, downloaded asset bytes and provider
payloads never cross this module's boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from xtrace_spike.repo import PgRepo, parse_uuid  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_STATUS_VALUES = frozenset({"completed", "disabled", "unavailable", "limited", "failed"})
_ASSET_KINDS = frozenset({"thumbnail", "storyboard"})
_COUNTER_NAMES = (
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
)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_MAX_TEXT_LENGTH = 255


def record_refinement(
    *,
    search_id: str,
    status: str,
    policy_version: str,
    candidates_requested: int,
    candidates_processed: int,
    assets_requested: int,
    assets_evaluated: int,
    assets_discarded: int,
    bytes_downloaded: int,
    embedding_count: int,
    embedding_elapsed_ms: int,
    errors_count: int,
    improved_count: int,
    unchanged_count: int,
    elapsed_ms: int,
    limit_reason: str | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    _repo_factory: Callable[[], Any] | None = None,
) -> None:
    """Persist one aggregate refinement result, best-effort and idempotently.

    Input validation deliberately happens before the best-effort database
    boundary.  Invalid counters/statuses therefore fail closed instead of
    reaching SQL, while a database outage remains non-fatal to ``POST /search``.
    ``_repo_factory`` is private dependency injection for server-side tests and
    the compatibility wrapper in :mod:`xtrace_api.analytics`.
    """

    summary = _validate_summary(
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
    )
    try:
        evidence_rows = _sanitise_evidence(summary["search_id"], evidence)
        asyncio.run(
            _insert_refinement(
                summary,
                evidence_rows,
                repo_factory=_repo_factory or PgRepo,
            )
        )
    except Exception:
        # Provider/DB exceptions must never turn a valid search into a 5xx.  Do
        # not include URLs, exception text or payloads in this log message.
        logger.warning(
            "no se pudo registrar la telemetría de refinamiento para search_id=%s",
            summary["search_id"],
            exc_info=True,
        )


async def _insert_refinement(
    summary: Mapping[str, Any],
    evidence_rows: Iterable[tuple[Any, ...]],
    *,
    repo_factory: Callable[[], Any],
) -> None:
    """Write summary and deduplicated evidence in one server-side session."""

    async with await repo_factory().connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                insert into public.search_refinements (
                  search_id, status, policy_version,
                  candidates_requested, candidates_processed,
                  assets_requested, assets_evaluated, assets_discarded,
                  bytes_downloaded, embedding_count, embedding_elapsed_ms,
                  errors_count, improved_count, unchanged_count, elapsed_ms,
                  limit_reason, finished_at
                ) values (
                  %s, %s, %s,
                  %s, %s,
                  %s, %s, %s,
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s
                )
                on conflict (search_id) do update set
                  status = excluded.status,
                  policy_version = excluded.policy_version,
                  candidates_requested = excluded.candidates_requested,
                  candidates_processed = excluded.candidates_processed,
                  assets_requested = excluded.assets_requested,
                  assets_evaluated = excluded.assets_evaluated,
                  assets_discarded = excluded.assets_discarded,
                  bytes_downloaded = excluded.bytes_downloaded,
                  embedding_count = excluded.embedding_count,
                  embedding_elapsed_ms = excluded.embedding_elapsed_ms,
                  errors_count = excluded.errors_count,
                  improved_count = excluded.improved_count,
                  unchanged_count = excluded.unchanged_count,
                  elapsed_ms = excluded.elapsed_ms,
                  limit_reason = excluded.limit_reason,
                  finished_at = now()
                """,
                (
                    summary["search_id"],
                    summary["status"],
                    summary["policy_version"],
                    summary["candidates_requested"],
                    summary["candidates_processed"],
                    summary["assets_requested"],
                    summary["assets_evaluated"],
                    summary["assets_discarded"],
                    summary["bytes_downloaded"],
                    summary["embedding_count"],
                    summary["embedding_elapsed_ms"],
                    summary["errors_count"],
                    summary["improved_count"],
                    summary["unchanged_count"],
                    summary["elapsed_ms"],
                    summary["limit_reason"],
                    summary["finished_at"],
                ),
            )
            for row in evidence_rows:
                await cur.execute(
                    """
                    insert into public.search_refinement_evidence (
                      search_id, video_id, source, candidate_rank, asset_kind,
                      asset_url, asset_url_hash, position, timestamp_ms,
                      similarity, selected, discarded_reason
                    ) values (
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s
                    )
                    on conflict do nothing
                    """,
                    row,
                )


def _validate_summary(
    *,
    search_id: str,
    status: str,
    policy_version: str,
    candidates_requested: int,
    candidates_processed: int,
    assets_requested: int,
    assets_evaluated: int,
    assets_discarded: int,
    bytes_downloaded: int,
    embedding_count: int,
    embedding_elapsed_ms: int,
    errors_count: int,
    improved_count: int,
    unchanged_count: int,
    elapsed_ms: int,
    limit_reason: str | None,
) -> dict[str, Any]:
    """Validate and normalise the SQL summary parameters before persistence."""

    parsed_search_id = _coerce_uuid(search_id, "search_id")
    status_value = _text_value(status, "status")
    if status_value not in _STATUS_VALUES:
        raise ValueError(f"status no permitido: {status_value!r}")
    policy_value = _text_value(policy_version, "policy_version")
    raw_counters = {
        "candidates_requested": candidates_requested,
        "candidates_processed": candidates_processed,
        "assets_requested": assets_requested,
        "assets_evaluated": assets_evaluated,
        "assets_discarded": assets_discarded,
        "bytes_downloaded": bytes_downloaded,
        "embedding_count": embedding_count,
        "embedding_elapsed_ms": embedding_elapsed_ms,
        "errors_count": errors_count,
        "improved_count": improved_count,
        "unchanged_count": unchanged_count,
        "elapsed_ms": elapsed_ms,
    }
    counters = {name: _nonnegative_int(raw_counters[name], name) for name in _COUNTER_NAMES}
    if counters["candidates_processed"] > counters["candidates_requested"]:
        raise ValueError("candidates_processed no puede superar candidates_requested")
    if counters["assets_evaluated"] + counters["assets_discarded"] > counters["assets_requested"]:
        raise ValueError("assets_evaluated + assets_discarded no puede superar assets_requested")
    if counters["improved_count"] + counters["unchanged_count"] > counters["candidates_processed"]:
        raise ValueError("improved_count + unchanged_count no puede superar candidates_processed")
    reason = _optional_code(limit_reason, "limit_reason")
    return {
        "search_id": parsed_search_id,
        "status": status_value,
        "policy_version": policy_value,
        **counters,
        "limit_reason": reason,
        "finished_at": datetime.now(UTC),
    }


def _sanitise_evidence(
    search_id: UUID,
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Any, ...], ...]:
    """Convert public evidence to metadata-only, deduplicated SQL rows.

    Malformed evidence is dropped rather than allowed to invalidate the base
    search.  The selected URL is canonicalised without credentials, query or
    fragment, and its hash is always derived here instead of trusting a caller
    supplied digest.
    """

    rows: list[tuple[Any, ...]] = []
    seen: set[tuple[UUID, str, int | None]] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        try:
            video_id = _coerce_uuid(item.get("video_id"), "video_id")
            source = _text_value(item.get("source"), "source").lower()
            asset_kind = _text_value(item.get("asset_kind"), "asset_kind").lower()
            if asset_kind not in _ASSET_KINDS:
                continue
            asset_url = _sanitise_public_url(item.get("asset_url"))
            if asset_url is None:
                continue
            candidate_rank = _positive_int(item.get("candidate_rank"), "candidate_rank")
            position = _optional_nonnegative_int(item.get("position"), "position")
            timestamp_ms = _optional_nonnegative_int(item.get("timestamp_ms"), "timestamp_ms")
            similarity = _similarity(item.get("similarity"))
            selected = item.get("selected", False)
            if not isinstance(selected, bool) or (selected and timestamp_ms is None):
                continue
            discarded_reason = _optional_code(item.get("discarded_reason"), "discarded_reason")
        except (TypeError, ValueError):
            continue

        key = (video_id, _hash_public_url(asset_url), timestamp_ms)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            (
                search_id,
                video_id,
                source,
                candidate_rank,
                asset_kind,
                asset_url,
                key[1],
                position,
                timestamp_ms,
                similarity,
                selected,
                discarded_reason,
            )
        )
    return tuple(rows)


def _coerce_uuid(value: Any, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} debe ser un UUID")
    return cast(UUID, parse_uuid(value, field))


def _text_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} debe ser texto no vacío")
    value = value.strip()
    if len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{field} supera el límite de longitud")
    return cast(str, value)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} debe ser entero no negativo")
    return cast(int, value)


def _positive_int(value: Any, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result < 1:
        raise ValueError(f"{field} debe ser >= 1")
    return result


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field)


def _similarity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("similarity debe ser numérica")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError("similarity debe estar en [0,1]")
    return result


def _optional_code(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = _text_value(value, field)
    if not _SAFE_CODE.fullmatch(text):
        raise ValueError(f"{field} contiene caracteres no permitidos")
    return text


def _sanitise_public_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        # Accessing ``port`` validates malformed ports before constructing the
        # canonical netloc; no network lookup is performed here.
        port = parsed.port
    except ValueError:
        return None

    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _hash_public_url(url: str) -> str:
    return "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
