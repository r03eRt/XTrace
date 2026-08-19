"""In-process orchestration for the on-demand temporal refinement pass.

The orchestrator owns no index or network client. Callers inject the catalog
resolver and the already-gated asset resolver, which keeps the first-pass
ranking as the source of truth and makes the second pass easy to test with
in-memory fakes.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from PIL import Image
from xtrace_spike.embeddings.provider import EmbeddingProvider  # type: ignore[import-untyped]

from .assets import MaterializationResult, MaterializedAsset
from .catalog import is_refinable_candidate
from .evaluator import EvaluationResult, TemporalRefinementEvaluator
from .models import (
    AssetKind,
    RefinementCandidate,
    RefinementOutcome,
    RefinementStatus,
    RefinementSummary,
    ResultRefinementStatus,
    TimestampOrigin,
    TimestampProvenance,
)
from .policy import RefinementPolicy

if TYPE_CHECKING:
    from xtrace_spike.search.ranking import RankedVideo  # type: ignore[import-untyped]

    from xtrace_api.search_service import VideoMetadata


# Keep the runtime alias free of imports from the legacy spike; concrete names
# are only needed by static type checkers at the service boundary.
CandidateResolver = Callable[
    [Any, Mapping[str, Any]],
    RefinementCandidate | None | Awaitable[RefinementCandidate | None],
]
AssetResolverValue = Sequence[MaterializedAsset] | MaterializationResult
AssetResolver = Callable[[RefinementCandidate], Awaitable[AssetResolverValue]]
Cleanup = Callable[[], Awaitable[None]]
_BlockingResult = TypeVar("_BlockingResult")

# ``asyncio.run`` closes its *default* executor before returning.  Refinement
# deliberately uses bounded, process-scoped executors instead, so a blocking
# catalogue lookup or embedding call that outlives its budget cannot extend
# the HTTP response while it finishes in the background.  The worker owns the
# evaluator's ``finally`` cleanup; the bound prevents unbounded timed-out work.
_RESOLVER_EXECUTOR = ThreadPoolExecutor(max_workers=5, thread_name_prefix="xtrace-refine-resolver")
_EVALUATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=5, thread_name_prefix="xtrace-refine-evaluator"
)


class TemporalRefinementOrchestrator:
    """Run a bounded, fallback-preserving refinement over a base ranking."""

    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        candidate_resolver: CandidateResolver,
        asset_resolver: AssetResolver,
        cleanup: Cleanup | None = None,
    ) -> None:
        self._evaluator = TemporalRefinementEvaluator(embeddings)
        self._candidate_resolver = candidate_resolver
        self._asset_resolver = asset_resolver
        self._cleanup = cleanup
        self._cleaned = False

    async def aclose(self) -> None:
        """Release adapter/network resources owned by the composition root."""

        if self._cleaned or self._cleanup is None:
            return
        self._cleaned = True
        await self._cleanup()

    async def refine(
        self,
        query_image: Image.Image,
        ranked: Sequence[RankedVideo],
        metadata: Mapping[str, VideoMetadata],
        *,
        policy: RefinementPolicy,
    ) -> RefinementOutcome:
        """Refine only the configured prefix and preserve base order/scores.

        Resolver failures and invalid assets are converted to provenance on the
        base result. Cancellation is deliberately re-raised so request cleanup
        remains owned by the HTTP boundary.
        """

        started = time.perf_counter()
        base_ranked = tuple(ranked)
        requested_indexes, candidates_requested = _requested_candidate_indexes(
            base_ranked, metadata, policy
        )
        provenance: dict[str, TimestampProvenance] = {}
        refined_ranked = list(base_ranked)

        if not policy.enabled:
            for item in base_ranked:
                provenance[item.video_id] = TimestampProvenance(
                    origin=TimestampOrigin.BASE_INDEX,
                    status=ResultRefinementStatus.DISABLED,
                )
            return RefinementOutcome(
                ranked=base_ranked,
                provenance=provenance,
                summary=_summary(
                    status=RefinementStatus.DISABLED,
                    candidates_requested=candidates_requested,
                    candidates_processed=0,
                    elapsed_ms=_elapsed_ms(started),
                ),
            )

        deadline = asyncio.get_running_loop().time() + policy.search_timeout_ms / 1000
        candidates_processed = 0
        assets_evaluated = 0
        assets_discarded = 0
        errors_count = 0
        bytes_downloaded = 0
        embedding_count = 0
        embedding_elapsed_ms = 0
        improved_results = 0
        limited = len(requested_indexes) < len(base_ranked)
        saw_evaluable_assets = False

        for index, item in enumerate(base_ranked):
            if index not in requested_indexes:
                provenance[item.video_id] = _base_provenance(item, ResultRefinementStatus.LIMITED)
                continue

            loop = asyncio.get_running_loop()
            candidate_started = loop.time()
            candidate_deadline = min(
                deadline, candidate_started + policy.candidate_timeout_ms / 1000
            )
            if candidate_started >= candidate_deadline:
                limited = True
                provenance[item.video_id] = _base_provenance(item, ResultRefinementStatus.LIMITED)
                continue
            candidates_processed += 1

            try:
                candidate = await asyncio.wait_for(
                    _invoke_candidate_resolver(self._candidate_resolver, item, metadata),
                    max(0.0, candidate_deadline - loop.time()),
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                limited = True
                provenance[item.video_id] = _base_provenance(item, ResultRefinementStatus.LIMITED)
                continue
            except Exception:
                errors_count += 1
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.UNAVAILABLE
                )
                continue

            if candidate is None or not is_refinable_candidate(candidate):
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.UNAVAILABLE
                )
                continue

            source_policy = policy.for_source(candidate.source)
            if not source_policy.enabled:
                provenance[item.video_id] = _base_provenance(
                    item,
                    ResultRefinementStatus.DISABLED,
                    source=candidate.source,
                )
                continue

            candidate_deadline = min(
                deadline, candidate_started + source_policy.candidate_timeout_ms / 1000
            )
            remaining = candidate_deadline - loop.time()
            if remaining <= 0:
                limited = True
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.LIMITED, source=candidate.source
                )
                continue

            timeout = min(remaining, source_policy.candidate_timeout_ms / 1000)
            try:
                resolved = await asyncio.wait_for(self._asset_resolver(candidate), timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                limited = True
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.LIMITED, source=candidate.source
                )
                continue
            except Exception:
                errors_count += 1
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.UNAVAILABLE, source=candidate.source
                )
                continue

            materialized, materialized_discarded, downloaded = _normalise_assets(resolved)
            if len(materialized) > source_policy.max_assets_per_candidate:
                keep = materialized[: source_policy.max_assets_per_candidate]
                for discarded_asset in materialized[source_policy.max_assets_per_candidate :]:
                    discarded_asset.close()
                materialized_discarded += len(materialized) - len(keep)
                materialized = keep
            assets_discarded += materialized_discarded
            bytes_downloaded += downloaded
            evaluation_started = time.perf_counter()
            remaining = candidate_deadline - loop.time()
            if remaining <= 0:
                limited = True
                for asset in materialized:
                    asset.close()
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.LIMITED, source=candidate.source
                )
                continue
            evaluation_task = asyncio.create_task(
                _run_bounded_blocking(
                    _EVALUATION_EXECUTOR,
                    partial(
                        self._evaluator.evaluate,
                        query_image,
                        materialized,
                        base_timestamp_ms=item.match_timestamp_ms,
                        base_visual_similarity=item.visual_similarity,
                        duration_ms=candidate.duration_ms,
                    ),
                )
            )
            evaluation_task.add_done_callback(_consume_task_result)
            try:
                # ``evaluate`` is synchronous because the embedding provider is
                # CPU-bound.  Run it in a worker and shield the task so a
                # timeout/cancellation cannot interrupt Pillow's finally block;
                # the evaluator owns and closes every materialized image.
                evaluation = await asyncio.wait_for(asyncio.shield(evaluation_task), remaining)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                limited = True
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.LIMITED, source=candidate.source
                )
                continue
            except Exception:
                errors_count += 1
                provenance[item.video_id] = _base_provenance(
                    item, ResultRefinementStatus.UNAVAILABLE, source=candidate.source
                )
                continue
            finally:
                embedding_elapsed_ms += _elapsed_ms(evaluation_started)

            assets_evaluated += evaluation.evaluated_count
            assets_discarded += evaluation.discarded_count
            embedding_count += 1 + evaluation.evaluated_count
            if evaluation.evaluated_count:
                saw_evaluable_assets = True

            if evaluation.status == ResultRefinementStatus.IMPROVED:
                refined_ranked[index] = replace(
                    item,
                    match_timestamp_ms=evaluation.timestamp_ms,
                )
                improved_results += 1
                provenance[item.video_id] = _refined_provenance(candidate, evaluation)
            else:
                result_status = (
                    ResultRefinementStatus.UNCHANGED
                    if evaluation.evaluated_count
                    else ResultRefinementStatus.UNAVAILABLE
                )
                provenance[item.video_id] = _base_provenance(
                    item,
                    result_status,
                    source=candidate.source,
                )

        if not base_ranked:
            summary_status = RefinementStatus.UNAVAILABLE
        elif limited:
            summary_status = RefinementStatus.LIMITED
        elif errors_count and not saw_evaluable_assets:
            summary_status = RefinementStatus.FAILED
        elif saw_evaluable_assets:
            summary_status = RefinementStatus.COMPLETED
        else:
            summary_status = RefinementStatus.UNAVAILABLE

        return RefinementOutcome(
            ranked=tuple(refined_ranked),
            provenance=provenance,
            summary=_summary(
                status=summary_status,
                candidates_requested=candidates_requested,
                candidates_processed=candidates_processed,
                assets_evaluated=assets_evaluated,
                assets_discarded=assets_discarded,
                errors_count=errors_count,
                bytes_downloaded=bytes_downloaded,
                embedding_count=embedding_count,
                embedding_elapsed_ms=embedding_elapsed_ms,
                improved_results=improved_results,
                elapsed_ms=_elapsed_ms(started),
            ),
        )


def _normalise_assets(
    resolved: AssetResolverValue | None,
) -> tuple[tuple[MaterializedAsset, ...], int, int]:
    if resolved is None:
        return (), 0, 0
    if isinstance(resolved, MaterializationResult):
        return resolved.assets, resolved.discarded_count, resolved.bytes_downloaded
    return tuple(resolved), 0, 0


def _requested_candidate_indexes(
    ranked: Sequence[Any],
    metadata: Mapping[str, VideoMetadata],
    policy: RefinementPolicy,
) -> tuple[set[int], int]:
    """Select the bounded prefix while honouring per-source candidate caps."""

    selected: set[int] = set()
    counts: dict[str, int] = {}
    for index, item in enumerate(ranked):
        if len(selected) >= policy.candidate_limit:
            break
        source = _metadata_source(item, metadata)
        key = source or "__default__"
        source_policy = policy.for_source(source)
        if counts.get(key, 0) >= source_policy.candidate_limit:
            continue
        selected.add(index)
        counts[key] = counts.get(key, 0) + 1
    return selected, len(selected)


def _metadata_source(item: Any, metadata: Mapping[str, VideoMetadata]) -> str | None:
    record = metadata.get(item.video_id)
    source = getattr(record, "source", None)
    return source.strip().lower() if isinstance(source, str) and source.strip() else None


async def _invoke_candidate_resolver(
    resolver: CandidateResolver,
    item: Any,
    metadata: Mapping[str, VideoMetadata],
) -> RefinementCandidate | None:
    """Invoke sync or async candidate resolvers under ``wait_for``.

    The resolver is called in a worker even when it is synchronous, so a slow
    catalog lookup cannot block the event loop and defeat the global deadline.
    If a wrapper returns an awaitable, it is awaited after the worker returns.
    """

    candidate = await _run_bounded_blocking(
        _RESOLVER_EXECUTOR,
        partial(resolver, item, metadata),
    )
    if inspect.isawaitable(candidate):
        candidate = await candidate
    return candidate


async def _run_bounded_blocking(
    executor: ThreadPoolExecutor,
    function: Callable[[], _BlockingResult],
) -> _BlockingResult:
    """Await bounded blocking work without default-executor shutdown waits.

    ``asyncio.wrap_future`` registers a loop callback that can fire after
    ``asyncio.run`` has closed the loop when a timed-out worker finishes.  A
    small guarded hand-off avoids that race while preserving cancellation of
    the awaiter; the worker itself remains responsible for its own cleanup.
    """

    loop = asyncio.get_running_loop()
    result: asyncio.Future[_BlockingResult] = loop.create_future()
    submitted = executor.submit(function)

    def transfer() -> None:
        if result.done():
            return
        try:
            result.set_result(submitted.result())
        except BaseException as exc:
            result.set_exception(exc)

    def completed(_source: Any) -> None:
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(transfer)
        except RuntimeError:
            # The loop may close between the check and the callback enqueue.
            return

    submitted.add_done_callback(completed)
    try:
        return await result
    except asyncio.CancelledError:
        submitted.cancel()
        raise


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    """Drain a shielded evaluator task after a timeout/cancellation."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        # The outer request already converted the failure to fallback; this
        # callback only prevents an unhandled task warning.
        return


def _refined_provenance(
    candidate: RefinementCandidate,
    evaluation: EvaluationResult,
) -> TimestampProvenance:
    selected = evaluation.selected_asset
    if selected is None:
        return _base_provenance(
            None,
            ResultRefinementStatus.UNCHANGED,
            source=candidate.source,
        )
    return TimestampProvenance(
        origin=TimestampOrigin.REFINED_ASSET,
        status=ResultRefinementStatus.IMPROVED,
        source=candidate.source,
        asset_kind=AssetKind(selected.kind),
        asset_url=sanitise_public_url(selected.url),
        asset_position=selected.position,
    )


def _base_provenance(
    item: Any,
    status: ResultRefinementStatus,
    *,
    source: str | None = None,
) -> TimestampProvenance:
    return TimestampProvenance(
        origin=TimestampOrigin.BASE_INDEX,
        status=status,
        source=source,
    )


def sanitise_public_url(url: str | None) -> str | None:
    """Return a public HTTP(S) URL without credentials/query/fragment."""

    if not url:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _summary(
    *,
    status: RefinementStatus,
    candidates_requested: int,
    candidates_processed: int,
    assets_evaluated: int = 0,
    assets_discarded: int = 0,
    errors_count: int = 0,
    bytes_downloaded: int = 0,
    embedding_count: int = 0,
    embedding_elapsed_ms: int = 0,
    improved_results: int = 0,
    elapsed_ms: int = 0,
) -> RefinementSummary:
    return RefinementSummary(
        status=status,
        candidates_requested=candidates_requested,
        candidates_processed=candidates_processed,
        assets_evaluated=assets_evaluated,
        assets_discarded=assets_discarded,
        errors_count=errors_count,
        bytes_downloaded=bytes_downloaded,
        embedding_count=embedding_count,
        embedding_elapsed_ms=embedding_elapsed_ms,
        improved_results=improved_results,
        elapsed_ms=elapsed_ms,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
