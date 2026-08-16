"""CLI operativa del servicio crawler (PR-032 · FR-006/FR-007/FR-013/FR-014 · SEC-002 · NFR-004
· contracts §5).

Comandos Typer (`xtrace-crawler`):

- `sources [--json]` — lista las fuentes registradas en BD (`sources`, DATA-001)
  con su manifest de compliance y `enabled` (lectura vía `CrawlerRepo`).
- `backfill --source <name> [--limit N] [--max-videos N] [--incremental]
  [--section <path>]` — valida que la fuente exista (registry + BD) y esté
  habilitada (gate SEC-002) y **encola el job DISCOVER inicial** con el
  payload del contrato de PR-030 + cota global PR-036 + `section` PR-049
  (`{"source", "cursor": None, "limit", "mode", "max_videos", "section"}`)
  vía `JobsRepo.enqueue` (FR-006/FR-007). Salida JSON
  `{job_id, source, mode, max_videos}` (+ `section` cuando se da).
  `--max-videos` es la cota GLOBAL del backfill (SC-002, analyze hallazgo 2):
  el pipeline corta la cadena de paginación al alcanzarla; es **obligatoria**
  para el backfill real de xvideos (default: `XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS`).
  **`--section <path>` (PR-049 · FR-007 · pruebas del operador)**: ruta de
  sección del sitio (categoría/tag, p. ej. `/tags/xxx`) que **debe empezar
  por '/'** (si no, error de uso exit 2): el DISCOVER arranca en
  `https://www.xvideos.com<section>` en vez de la home; el payload lleva
  `section` (null sin flag) y el dedupe distingue la cadena.
- `run-worker [--concurrency N] [--once]` — arranca el `JobWorker` (PR-027) con
  los **handlers del pipeline** (PR-030) y el registry (SEC-002: cada job resuelve
  su adapter con el gate y el `enabled` de BD); `--once` procesa una pasada de
  jobs elegibles y termina con `{"processed": N}`; sin `--once` corre hasta
  Ctrl+C/SIGTERM (logs a stderr).
- `stats [--json]` — estadísticas básicas del crawler (FR-014): jobs por
  estado/fuente, vídeos por estado y errores recientes con causa
  (`CrawlerRepo.stats`). Desde PR-035 incluye la sección `rate_limits` por
  fuente (requests/rate_limit_waits/total_wait_ms acumulados del `RateLimiter`,
  SC-005 · NFR-004) aportada por el provider del contexto (en el proceso que
  ejecutó el pipeline); `run-worker` deja además el resumen en logs.
- `check-availability --source <name> [--limit N]` — encola jobs
  `CHECK_AVAILABILITY` para los vídeos web de la fuente (FR-013) y devuelve
  `{source, limit, enqueued, job_ids}`.

**Salida**: los datos van SIEMPRE como **JSON estable por stdout** (orden de
claves canónico con `sort_keys`, UTF-8; tests/observabilidad — contracts §5);
los **logs y errores van a stderr**. Los errores de usuario (fuente desconocida,
fuente no habilitada por SEC-002, límite inválido) imprimen un mensaje claro en
stderr y terminan con **exit code != 0** (1 = error de dominio, 2 = error de
uso del CLI).

**Inyección de dependencias (tests sin red y sin BD, NFR-003)**: cada comando
lee su `CliContext` de `ctx.obj`. El callback construye el contexto por defecto
(repos reales + registry con `MockAdapter` —exento del gate, FR-003— y
`XvideosAdapter` —real, manifest **revisado** 2026-08-16 (SEC-002, PR-042):
la habilitación efectiva vive en `sources.enabled=true` en BD—); los tests
inyectan un contexto con fakes vía `CliRunner.invoke(..., obj=...)`. El worker
del `run-worker` también es inyectable (`CliContext.worker`) para probar la
pasada sin cablear el pipeline.

**PR-050 (FR-011 · ADR-0011 · contracts §6)**: el contexto por defecto
construye el proveedor de embeddings según la env `XTRACE_CRAWLER_EMBEDDINGS`
(`fake` por defecto → `CliContext.embeddings=None`: el pipeline usa el
`FakeEmbeddingProvider` determinista de PR-030, sin torch; `siglip` →
`SiglipLocalProvider` REAL de `xtrace_spike.embeddings.siglip_local` para las
validaciones reales del operador — import dinámico, mismo patrón que el
adapter xvideos; el constructor no carga torch, PR-005). **PR-051 (1a
ejecución real con SigLIP, 2026-08-16)**: con `siglip`, el contexto por
defecto **exige el extra `siglip` en el arranque** (fail-fast
`_require_siglip_extra`: el proveedor del spike importa torch/open_clip de
forma lazy y, sin la comprobación, el `run-worker` fallaría job a job con
`ModuleNotFoundError` opaco); si falta, el CLI falla con un error claro y
accionable (`uv sync --extra siglip` en `services/crawler`), **sin fallback
silencioso a fake**. El switch solo se evalúa en el contexto por defecto real:
los tests inyectan `deps.embeddings` y nunca pasan por aquí (sin torch en CI).

`config.py` (PR-032) aporta los defaults del worker y de los límites:
`worker_concurrency`, `job_lease_timeout_seconds`, `backfill_default_limit`,
`check_availability_default_limit` (override por env `XTRACE_CRAWLER_*`).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from importlib.util import find_spec
from typing import Annotated, Any, NoReturn, Protocol, cast

import typer
from xtrace_spike.vectorstore.base import VectorStore  # type: ignore[import-untyped]

from xtrace_crawler.adapters.base import SourceAdapter
from xtrace_crawler.adapters.mock import MockAdapter
from xtrace_crawler.adapters.registry import AdapterNotEnabledError, AdapterRegistry
from xtrace_crawler.config import Settings
from xtrace_crawler.crawling.ratelimit import RateLimiter
from xtrace_crawler.jobs.repo import JobsRepo
from xtrace_crawler.jobs.types import Job, JobType
from xtrace_crawler.jobs.worker import JobWorker
from xtrace_crawler.pipeline import CrawlerPipeline, CrawlerRepoProtocol, EmbeddingProviderProtocol
from xtrace_crawler.repo import (
    DEFAULT_RECENT_ERRORS_LIMIT,
    CrawlerRepo,
    CrawlerStats,
    RateLimitStatsRecord,
    SourceRecord,
    parse_uuid,
    rate_limit_stats_record,
)

logger = logging.getLogger(__name__)


class CliUserError(Exception):
    """Error de uso del CLI (fuente desconocida, no habilitada, …): mensaje + exit 1.

    El worker del CLI lo captura por comando y lo convierte en un mensaje claro
    en stderr con exit code != 0 (contracts §5).
    """


class CliRepoProtocol(CrawlerRepoProtocol, Protocol):
    """Contrato de repo que el CLI necesita: el del pipeline + listado y stats.

    Satisfecho por `CrawlerRepo` (PR-028); los tests lo cumplen con un fake en
    memoria (NFR-003).
    """

    async def list_sources(self) -> list[SourceRecord]: ...

    async def stats(
        self,
        *,
        recent_errors_limit: int = DEFAULT_RECENT_ERRORS_LIMIT,
        rate_limits: dict[str, RateLimitStatsRecord] | None = None,
    ) -> CrawlerStats: ...


class CliJobsProtocol(Protocol):
    """Contrato de la cola que el CLI necesita: encolado (PR-030) + ciclo de vida del worker.

    Satisfecho por `JobsRepo` (PR-026); los tests lo cumplen con un fake en
    memoria (NFR-003).
    """

    async def enqueue(
        self,
        job_type: JobType,
        *,
        source_id: uuid.UUID | None = None,
        video_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
        not_before: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> Job: ...

    async def claim_next(self, worker_id: str) -> Job | None: ...

    async def complete(self, job_id: uuid.UUID) -> Job: ...

    async def fail(self, job_id: uuid.UUID, error: str, *, terminal: bool = False) -> Job: ...

    async def unavailable(self, job_id: uuid.UUID, error: str | None = None) -> Job: ...

    async def reset_stale_leases(self, timeout_seconds: float) -> int: ...


@dataclass
class CliContext:
    """Dependencias de los comandos; inyectables en tests (NFR-003, sin red/BD).

    `list_source_videos` lista los `external_id` de los vídeos web de una fuente
    (paginado por `limit`): el repo (PR-028) no expone listado de vídeos, así que
    el contexto por defecto lo resuelve con SQL directo sobre la conexión del
    repo (ver `_list_source_videos_for`); los tests inyectan un callable fake.

    `worker`/`store`/`embeddings` permiten inyectar el worker (tests) o el
    índice/proveedor reales (p. ej. SigLIP para ejecución local, PR-030).

    `limiter_factory`/`rate_limits_provider` (PR-035 · SC-005 · NFR-004):
    el factory registra en un registro compartido los `RateLimiter` que crea el
    pipeline y el provider expone su contabilidad acumulada como sección
    `rate_limits` de `stats` (y como resumen en logs del `run-worker`); en
    tests se inyectan fakes acelerados.
    """

    settings: Settings
    registry: AdapterRegistry
    repo: CliRepoProtocol
    jobs: CliJobsProtocol
    list_source_videos: Callable[[str, int], Awaitable[list[str]]]
    worker: JobWorker | None = None
    store: VectorStore | None = None
    embeddings: EmbeddingProviderProtocol | None = None
    limiter_factory: Callable[[SourceAdapter], RateLimiter] | None = None
    rate_limits_provider: Callable[[], dict[str, RateLimitStatsRecord]] | None = None


app = typer.Typer(
    name="xtrace-crawler",
    help=(
        "Servicio crawler de XTrace: ingesta de fuentes web al índice visual "
        "(spec 002 · FR-003). Operación: sources | backfill | run-worker | "
        "stats | check-availability (PR-032 · contracts §5)."
    ),
    no_args_is_help=True,
)


@app.callback()
def main(ctx: typer.Context) -> None:
    """Servicio crawler de XTrace: ingesta de fuentes web al índice visual (FR-003)."""
    if ctx.obj is None:
        try:
            ctx.obj = _default_context()
        except CliUserError as error:
            # PR-051: el contexto por defecto puede fallar al construir el
            # proveedor siglip (extra no instalado) → mensaje claro + exit 1
            # (contracts §5), sin traceback.
            raise _fail(error) from None
    _setup_logging(ctx.obj.settings)


# -- Comandos (contracts §5) ---------------------------------------------------


@app.command("sources")
def sources_cmd(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Salida JSON estable (contracts §5)")
    ] = False,
) -> None:
    """Lista las fuentes registradas en BD con su manifest y `enabled` (DATA-001)."""
    deps = _deps(ctx)
    records = asyncio.run(deps.repo.list_sources())
    if json_output:
        typer.echo(
            json.dumps(
                [_source_json(record) for record in records], sort_keys=True, ensure_ascii=False
            )
        )
    else:
        _print_sources_human(records)


@app.command("backfill")
def backfill_cmd(
    ctx: typer.Context,
    source: Annotated[
        str, typer.Option("--source", help="Nombre canónico de la fuente (adapters/registry.py)")
    ],
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help="Vídeos por página de DISCOVER (default: XTRACE_CRAWLER_BACKFILL_DEFAULT_LIMIT)",
        ),
    ] = None,
    max_videos: Annotated[
        int | None,
        typer.Option(
            "--max-videos",
            min=1,
            help=(
                "Cota global de vídeos del backfill (PR-036 · SC-002); el DISCOVER "
                "se detiene al alcanzarla. Default: XTRACE_CRAWLER_BACKFILL_MAX_VIDEOS; "
                "OBLIGATORIA para el backfill real de xvideos (contracts §5)"
            ),
        ),
    ] = None,
    incremental: Annotated[
        bool,
        typer.Option("--incremental", help="Modo INCREMENTAL: solo IDs nuevos (FR-007 · SC-003)"),
    ] = False,
    section: Annotated[
        str | None,
        typer.Option(
            "--section",
            callback=_validate_section_path,
            help=(
                "Sección del sitio (categoría/tag, p. ej. /tags/xxx): el DISCOVER "
                "arranca en https://www.xvideos.com<section> en vez de la home "
                "(PR-049 · FR-007 · pruebas del operador)"
            ),
        ),
    ] = None,
) -> None:
    """Valida la fuente (registry + BD + SEC-002) y encola el job DISCOVER inicial (FR-006/007)."""
    deps = _deps(ctx)
    try:
        result = asyncio.run(
            _backfill(
                deps,
                source=source,
                limit=limit,
                max_videos=max_videos,
                incremental=incremental,
                section=section,
            )
        )
    except CliUserError as error:
        raise _fail(error) from None
    typer.echo(json.dumps(result, sort_keys=True, ensure_ascii=False))


@app.command("run-worker")
def run_worker_cmd(
    ctx: typer.Context,
    concurrency: Annotated[
        int | None,
        typer.Option(
            "--concurrency",
            min=1,
            help="Tareas de proceso concurrentes (default: XTRACE_CRAWLER_WORKER_CONCURRENCY)",
        ),
    ] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Procesa una pasada de jobs elegibles y termina")
    ] = False,
) -> None:
    """Bucle de jobs con los handlers del pipeline (PR-030) y el registry (SEC-002)."""
    deps = _deps(ctx)
    result = asyncio.run(_run_worker(deps, concurrency=concurrency, once=once))
    typer.echo(json.dumps(result, sort_keys=True, ensure_ascii=False))


@app.command("stats")
def stats_cmd(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Salida JSON estable (contracts §5)")
    ] = False,
) -> None:
    """Estadísticas del crawler: jobs por estado/fuente, vídeos, errores recientes y
    contabilidad de rate limits por fuente (FR-014 · SC-005 · NFR-004)."""
    deps = _deps(ctx)
    stats = asyncio.run(deps.repo.stats(rate_limits=_rate_limits(deps)))
    if json_output:
        typer.echo(json.dumps(_stats_json(stats), sort_keys=True, ensure_ascii=False))
    else:
        _print_stats_human(stats)


@app.command("check-availability")
def check_availability_cmd(
    ctx: typer.Context,
    source: Annotated[
        str, typer.Option("--source", help="Nombre canónico de la fuente (adapters/registry.py)")
    ],
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help=(
                "Máx. vídeos a comprobar (default: XTRACE_CRAWLER_CHECK_AVAILABILITY_DEFAULT_LIMIT)"
            ),
        ),
    ] = None,
) -> None:
    """Encola jobs CHECK_AVAILABILITY para los vídeos web de la fuente (FR-013 · contracts §5)."""
    deps = _deps(ctx)
    try:
        result = asyncio.run(_check_availability(deps, source=source, limit=limit))
    except CliUserError as error:
        raise _fail(error) from None
    typer.echo(json.dumps(result, sort_keys=True, ensure_ascii=False))


# -- Lógica async de los comandos ----------------------------------------------


def _validate_section_path(value: str | None) -> str | None:
    """Valida `--section` (PR-049 · FR-007 · contracts §5): ruta de sección del
    sitio que SIEMPRE empieza por '/' (p. ej. `/tags/xxx`).

    Sin la barra inicial (o vacía) → `typer.BadParameter` → error de uso
    (exit code 2, contracts §5: 2 = error de uso del CLI).
    """
    if value is not None and not value.startswith("/"):
        raise typer.BadParameter(
            "debe empezar por '/' (ruta de sección del sitio, p. ej. /tags/xxx)"
        )
    return value


async def _backfill(
    deps: CliContext,
    *,
    source: str,
    limit: int | None,
    max_videos: int | None,
    incremental: bool,
    section: str | None,
) -> dict[str, Any]:
    """Encola el job DISCOVER inicial tras validar la fuente (FR-006/FR-007 · SEC-002).

    Payload del contrato de PR-030 + cota global PR-036 + sección PR-049:
    `{"source", "cursor": None, "limit", "mode", "max_videos", "section"}`; el
    `dedupe_key` (`discover:<source>:None:<mode>`; con `--section`, se añade
    la sección al final para no colisionar cadenas de secciones distintas)
    evita duplicar el DISCOVER inicial mientras esté activo (JobsRepo PR-026).
    La cota `--max-videos` (o el default de config) es la cota GLOBAL del
    backfill: el pipeline corta la cadena de paginación al alcanzarla (analyze
    hallazgo 2 · SC-002); es **obligatoria** para el backfill real de xvideos
    (contracts §5). La sección `--section` (validada por el callback del
    flag: empieza por '/') acota el discover a la categoría/tag (PR-049 ·
    FR-007 · pruebas del operador): el JSON de salida incluye `section` solo
    cuando se da.
    """
    record = await _validate_source(deps, source)
    mode = "incremental" if incremental else "backfill"
    effective_limit = limit if limit is not None else deps.settings.backfill_default_limit
    effective_max_videos = (
        max_videos if max_videos is not None else deps.settings.backfill_max_videos
    )
    dedupe_key = f"discover:{source}:None:{mode}"
    if section is not None:
        dedupe_key = f"{dedupe_key}:{section}"
    job = await deps.jobs.enqueue(
        JobType.DISCOVER,
        source_id=parse_uuid(record.id, "source_id"),
        payload={
            "source": source,
            "cursor": None,
            "limit": effective_limit,
            "mode": mode,
            "max_videos": effective_max_videos,
            "section": section,  # PR-049: null sin --section (contracts §5)
        },
        dedupe_key=dedupe_key,
    )
    logger.info(
        "backfill %s encolado: job %s (limit=%d, max_videos=%d, section=%s)",
        mode,
        job.id,
        effective_limit,
        effective_max_videos,
        section,
    )
    result = {
        "job_id": str(job.id),
        "source": source,
        "mode": mode,
        "max_videos": effective_max_videos,
    }
    if section is not None:
        result["section"] = section  # PR-049: solo cuando se da
    return result


async def _check_availability(
    deps: CliContext, *, source: str, limit: int | None
) -> dict[str, Any]:
    """Encola un CHECK_AVAILABILITY por vídeo web de la fuente (FR-013 · contracts §5).

    Sin vídeos en la fuente devuelve `enqueued: 0` (el JSON muestra el estado;
    el handler de PR-030 aplica `unavailable`/`removed` + exclusión del índice).
    """
    await _validate_source(deps, source)
    effective_limit = limit if limit is not None else deps.settings.check_availability_default_limit
    external_ids = await deps.list_source_videos(source, effective_limit)
    job_ids: list[str] = []
    for external_id in external_ids:
        job = await deps.jobs.enqueue(
            JobType.CHECK_AVAILABILITY,
            payload={"source": source, "external_id": external_id},
            dedupe_key=f"check_availability:{source}:{external_id}",
        )
        job_ids.append(str(job.id))
    logger.info("check-availability %s: %d vídeo(s) encolado(s)", source, len(job_ids))
    return {
        "source": source,
        "limit": effective_limit,
        "enqueued": len(job_ids),
        "job_ids": job_ids,
    }


async def _run_worker(deps: CliContext, *, concurrency: int | None, once: bool) -> dict[str, Any]:
    """Ejecuta el worker: una pasada (`--once`) o el bucle hasta Ctrl+C/SIGTERM.

    Con `deps.worker` inyectado (tests) se usa tal cual (el `--concurrency` de
    esa instancia es el que manda); si no, se construye el worker real
    (pipeline PR-030 + registry) con `concurrency` o el default de
    `Settings.worker_concurrency`.
    """
    worker = (
        deps.worker
        if deps.worker is not None
        else await _build_worker(deps, concurrency=concurrency)
    )
    if once:
        processed = await worker.run_once()
        _log_rate_limits(deps)
        return {"processed": processed}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            # Plataformas sin signal handlers en el event loop (p. ej. Windows).
            pass
    try:
        await worker.run_forever(stop)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, ValueError):
                pass
    return {"stopped": True}


async def _build_worker(deps: CliContext, *, concurrency: int | None) -> JobWorker:
    """Worker real: `JobWorker` (PR-027) + handlers del pipeline (PR-030) + registry.

    La resolución de adapters por job aplica el gate SEC-002 con el flag
    `enabled` de `sources` en un **snapshot tomado al arrancar** (el resolver
    del pipeline es síncrono; los cambios de `sources.enabled` requieren
    reiniciar el worker).
    """
    effective = concurrency if concurrency is not None else deps.settings.worker_concurrency
    enabled = await _enabled_snapshot(deps)

    def adapter_for(job: Job) -> SourceAdapter:
        name = job.payload.get("source")
        if not isinstance(name, str) or not name:
            raise ValueError(
                "payload['source'] requerido (str no vacío) para resolver el adapter; "
                f"recibido {job.payload!r}"
            )
        return deps.registry.get_enabled(name, enabled_in_db=enabled.get(name, False))

    pipeline = CrawlerPipeline(
        repo=deps.repo,
        jobs=deps.jobs,
        adapter_for=adapter_for,
        store=deps.store,
        embeddings=deps.embeddings,
        settings=deps.settings,
        limiter_factory=deps.limiter_factory,
    )
    worker = JobWorker(
        deps.jobs,
        concurrency=effective,
        lease_timeout_seconds=deps.settings.job_lease_timeout_seconds,
    )
    pipeline.register_handlers(worker)
    return worker


async def _enabled_snapshot(deps: CliContext) -> dict[str, bool]:
    """`sources.enabled` por adapter al arrancar el worker (gate SEC-002)."""
    snapshot: dict[str, bool] = {}
    for name in deps.registry.names():
        record = await deps.repo.get_source(name)
        snapshot[name] = record.enabled if record is not None else False
    return snapshot


async def _validate_source(deps: CliContext, name: str) -> SourceRecord:
    """Valida que la fuente exista (registry + BD) y esté habilitada (SEC-002).

    Raises:
        CliUserError: fuente desconocida (registry o BD) o no habilitable por el
            gate SEC-002 (manifest sin revisión legal y/o `sources.enabled=false`),
            con mensaje claro para el operador (contracts §5: exit code != 0).
    """
    if not deps.registry.is_registered(name):
        known = ", ".join(deps.registry.names()) or "(ninguna)"
        raise CliUserError(f"fuente desconocida: {name!r} (adapters registrados: {known})")
    record = await deps.repo.get_source(name)
    if record is None:
        raise CliUserError(
            f"fuente {name!r} no registrada en la BD (tabla sources); "
            "regístrala antes de encolar (la habilitación humana se hace con "
            "sources.enabled — SEC-002)"
        )
    try:
        deps.registry.get_enabled(name, enabled_in_db=record.enabled)
    except AdapterNotEnabledError as error:
        raise CliUserError(f"fuente {name!r} no habilitada (SEC-002): {error}") from None
    return record


# -- Salidas (JSON estable por stdout; logs por stderr) -------------------------


def _source_json(record: SourceRecord) -> dict[str, Any]:
    """Vista JSON estable de una fuente (contracts §5): manifest + `enabled`."""
    return {
        "name": record.name,
        "adapter": record.adapter,
        "enabled": record.enabled,
        "manifest": record.manifest.model_dump(mode="json"),
    }


def _stats_json(stats: CrawlerStats) -> dict[str, Any]:
    """Vista JSON estable de `CrawlerStats` (FR-014 · contracts §5).

    La clave de fuente `None` de `jobs_by_source` se normaliza a `"null"` para
    mantener un JSON estable (json.dumps ya la serializaría así). Desde PR-035
    se añade `rate_limits` (contabilidad del `RateLimiter` por fuente, SC-005 ·
    NFR-004) como sección NUEVA: los campos existentes permanecen intactos.
    """
    return {
        "jobs_by_status": dict(sorted(stats.jobs_by_status.items())),
        "jobs_by_source": {
            ("null" if key is None else key): value
            for key, value in sorted(stats.jobs_by_source.items(), key=lambda item: item[0] or "")
        },
        "videos_by_status": dict(sorted(stats.videos_by_status.items())),
        "recent_errors": [
            {
                "job_id": error.job_id,
                "job_type": error.job_type,
                "source": error.source,
                "video_id": error.video_id,
                "error": error.error,
                "updated_at": error.updated_at.isoformat(),
            }
            for error in stats.recent_errors
        ],
        "rate_limits": {
            name: record.model_dump(mode="json")
            for name, record in sorted(stats.rate_limits.items())
        },
    }


def _print_sources_human(records: list[SourceRecord]) -> None:
    """Vista humana de `sources` (sin `--json`): nombre + adapter + enabled + compliance."""
    for record in records:
        manifest = record.manifest
        review = manifest.review_date or "-"
        typer.echo(
            f"{record.name}\tadapter={record.adapter}\t"
            f"enabled={'yes' if record.enabled else 'no'}\t"
            f"robots_reviewed={'yes' if manifest.robots_reviewed else 'no'}\t"
            f"terms_reviewed={'yes' if manifest.terms_reviewed else 'no'}\t"
            f"review_date={review}"
        )


def _print_stats_human(stats: CrawlerStats) -> None:
    """Vista humana de `stats` (sin `--json`): mismos datos que el JSON (FR-014)."""
    typer.echo("jobs por estado: " + _counts_human(stats.jobs_by_status))
    typer.echo(
        "jobs por fuente: "
        + _counts_human(
            {("null" if key is None else key): value for key, value in stats.jobs_by_source.items()}
        )
    )
    typer.echo("vídeos por estado: " + _counts_human(stats.videos_by_status))
    if stats.rate_limits:
        typer.echo(
            "rate limits por fuente: "
            + " ".join(
                f"{name}=requests={record.requests} waits={record.rate_limit_waits} "
                f"wait_ms={record.total_wait_ms}"
                for name, record in sorted(stats.rate_limits.items())
            )
        )
    if stats.recent_errors:
        typer.echo("errores recientes:")
        for error in stats.recent_errors:
            typer.echo(
                f"  - {error.job_type} {error.source or '-'}: {error.error} (job {error.job_id})"
            )
    else:
        typer.echo("errores recientes: (ninguno)")


def _counts_human(counts: dict[str, int]) -> str:
    """`{"a": 1, "b": 2}` → `"a=1 b=2"` (orden estable)."""
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


# -- Composición raíz (DI por defecto) ------------------------------------------


def _default_registry() -> AdapterRegistry:
    """Registry por defecto del CLI: mock (exento del gate, FR-003) + xvideos (real).

    `XvideosAdapter` tiene el manifest **revisado** (2026-08-16, SEC-002 ·
    PR-042): la habilitación **efectiva** sigue exigiendo `sources.enabled=true`
    en BD (gate del registry, PR-028) — `backfill --source xvideos` falla con
    el detalle del gate en vez de "fuente desconocida".

    SC-007 (PR-031): ningún módulo del core importa **estáticamente** el adapter
    xvideos (test AST `test_core_no_importa_el_adapter_xvideos`); la composición
    raíz del CLI es el punto de registro de adapters concretos y lo resuelve con
    import dinámico (la instancia cumple el protocolo `SourceAdapter`).
    """
    registry = AdapterRegistry()
    registry.register(MockAdapter(), real=False)
    module: Any = importlib.import_module("xtrace_crawler.adapters.xvideos")
    registry.register(module.XvideosAdapter(), real=True)
    return registry


def _list_source_videos_for(repo: CrawlerRepo) -> Callable[[str, int], Awaitable[list[str]]]:
    """Vista `(source, limit) → external_ids` de los vídeos web de una fuente.

    El repo (PR-028) no expone listado de vídeos y `repo.py` no está en
    allowed_paths de PR-032: el CLI consulta directamente sobre la conexión del
    repo (SEC-003: credenciales de servidor, nunca cliente).
    """

    async def _list(source: str, limit: int) -> list[str]:
        async with await repo.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select v.external_id from public.videos v "
                    "join public.sources s on s.id = v.source_id "
                    "where s.name = %s and v.external_id is not null "
                    "order by v.external_id limit %s",
                    (source, limit),
                )
                rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    return _list


def _embedding_provider_for(settings: Settings) -> EmbeddingProviderProtocol | None:
    """Proveedor de embeddings del contexto por defecto (PR-050 · FR-011 · contracts §6).

    `XTRACE_CRAWLER_EMBEDDINGS=fake` (default) → `None`: el pipeline (PR-030)
    usa su default, el `FakeEmbeddingProvider` determinista de `xtrace_spike`
    (dimensión del esquema; tests/CI sin torch — comportamiento de hoy).

    `siglip` → el **`SiglipLocalProvider` REAL** de
    `xtrace_spike.embeddings.siglip_local` (ADR-0011): import dinámico (mismo
    patrón que el adapter xvideos, SC-007) e instanciación **sin argumentos**
    (paridad con el CLI del spike, PR-005: `ViT-B-16-SigLIP`/`webli`, D=768,
    embeddings L2-normalizados). El constructor no carga torch (carga lazy en
    el primer `embed_images`), así que la selección en sí no exige el extra
    `siglip` instalado; solo su uso real lo requiere.

    **PR-051 (hallazgo de la 1a ejecución real con SigLIP, 2026-08-16)**: el
    `SiglipLocalProvider` del spike importa torch/open_clip de forma **LAZY**
    (PR-005: `_ensure_loaded` en el primer `embed_images`), así que sin
    comprobación el `run-worker` arrancaría y TODOS los jobs INDEX_VIDEO
    fallarían con `ModuleNotFoundError: No module named 'open_clip'` — opaco
    y tardío. Este helper exige el extra **en el arranque del CLI**
    (`_require_siglip_extra`, fail-fast) y, además, si el import/instancia
    del módulo del spike falla con `ModuleNotFoundError`, lanza el **mismo
    error claro y accionable** con el comando de instalación — **sin fallback
    silencioso a fake** (un backfill con embeddings fake cuando se pidió
    SigLIP corrompería la validación). El error es `CliUserError` → stderr +
    exit 1 (contracts §5).

    El switch solo se evalúa en el contexto por defecto real (este helper solo
    lo llama `_default_context`): los tests inyectan `CliContext.embeddings`
    y nunca cargan torch.
    """
    if settings.embeddings == "siglip":
        _require_siglip_extra()
        try:
            module: Any = importlib.import_module("xtrace_spike.embeddings.siglip_local")
            provider = module.SiglipLocalProvider()
        except ModuleNotFoundError as error:
            raise CliUserError(
                "XTRACE_CRAWLER_EMBEDDINGS=siglip: no se pudo cargar "
                f"'xtrace_spike.embeddings.siglip_local' ({error}). Si falta el "
                "extra 'siglip' del crawler (open-clip-torch/torch), instálalo "
                "con 'uv sync --extra siglip' en services/crawler (o el "
                "equivalente de tu gestor de paquetes) y reintenta — sin "
                "fallback a fake"
            ) from error
        return cast(EmbeddingProviderProtocol, provider)
    return None


def _require_siglip_extra() -> None:
    """Fail-fast del extra `siglip` en el arranque del CLI (PR-051 · FR-011 · contracts §6).

    `SiglipLocalProvider` (spike, PR-005) importa `open_clip`/`torch` de forma
    **LAZY** (en el primer `embed_images`): sin esta comprobación, un
    `run-worker` con `XTRACE_CRAWLER_EMBEDDINGS=siglip` y el extra no
    instalado fallaría job a job con `ModuleNotFoundError` opaco (hallazgo de
    la 1a ejecución real con SigLIP, 2026-08-16). Se comprueba aquí, al
    construir el contexto, que el extra está instalado — con `find_spec` (no
    importa el módulo: sin coste de carga de torch ni de open_clip).

    Raises:
        CliUserError: si falta `open_clip` o `torch` (extra `siglip` no
            instalado), con el comando de instalación (contracts §6).
    """
    missing = [name for name in ("open_clip", "torch") if find_spec(name) is None]
    if missing:
        raise CliUserError(
            "XTRACE_CRAWLER_EMBEDDINGS=siglip requiere el extra 'siglip' del "
            "crawler (open-clip-torch/torch), que no está instalado "
            f"(faltan: {', '.join(missing)}): instálalo con 'uv sync --extra "
            "siglip' en services/crawler (o el equivalente de tu gestor de "
            "paquetes) y reintenta — sin fallback a fake"
        )


def _default_context() -> CliContext:
    """Contexto por defecto: repos reales + registry con mock/xvideos + env (D5).

    PR-050 (FR-011 · contracts §6): `embeddings` se resuelve desde
    `XTRACE_CRAWLER_EMBEDDINGS` — `fake` (default) → `None` (el pipeline usa
    su FakeEmbeddingProvider determinista); `siglip` → `SiglipLocalProvider`
    real para las validaciones del operador.
    """
    settings = Settings()
    repo = CrawlerRepo()
    shared_limiters: dict[str, RateLimiter] = {}

    def limiter_factory(adapter: SourceAdapter) -> RateLimiter:
        """Limiter por fuente compartido en el proceso (PR-035 · SC-005).

        El pipeline cachea su limiter por fuente (PR-030); este factory además
        lo registra en `shared_limiters` para que `stats` (mismo proceso) y el
        resumen del `run-worker` expongan la contabilidad acumulada.
        """
        name = adapter.manifest.source
        limiter = shared_limiters.get(name)
        if limiter is None:
            spec = settings.rate_limit_for(name, adapter.manifest.rate_limit)
            limiter = RateLimiter(spec, source=name)
            shared_limiters[name] = limiter
        return limiter

    def rate_limits_provider() -> dict[str, RateLimitStatsRecord]:
        return {
            name: rate_limit_stats_record(limiter.stats)
            for name, limiter in sorted(shared_limiters.items())
        }

    return CliContext(
        settings=settings,
        registry=_default_registry(),
        repo=repo,
        jobs=JobsRepo(),
        list_source_videos=_list_source_videos_for(repo),
        embeddings=_embedding_provider_for(settings),
        limiter_factory=limiter_factory,
        rate_limits_provider=rate_limits_provider,
    )


def _rate_limits(deps: CliContext) -> dict[str, RateLimitStatsRecord]:
    """Contabilidad del rate limiter por fuente del contexto (PR-035 · SC-005).

    `rate_limits_provider` inyectable (tests) o el registro compartido del
    contexto por defecto; sin provider la sección queda vacía (JSON estable).
    """
    if deps.rate_limits_provider is None:
        return {}
    return deps.rate_limits_provider()


def _log_rate_limits(deps: CliContext) -> None:
    """Resumen de respeto de límites por fuente al final de la pasada (SC-005 · NFR-004).

    La contabilidad del limiter vive en memoria del proceso que ejecutó el
    pipeline: `stats` en otro proceso no la ve, así que el `run-worker` deja la
    evidencia en logs (plan §Observability: esperas medibles/loggeadas).
    """
    rate_limits = _rate_limits(deps)
    if not rate_limits:
        return
    logger.info(
        "rate limits por fuente: %s",
        ", ".join(
            f"{name}=requests={record.requests} waits={record.rate_limit_waits} "
            f"wait_ms={record.total_wait_ms}"
            for name, record in sorted(rate_limits.items())
        ),
    )


def _deps(ctx: typer.Context) -> CliContext:
    """Dependencias del comando: `ctx.obj` (inyectado en tests o contexto por defecto)."""
    assert isinstance(ctx.obj, CliContext), "el callback debe inicializar ctx.obj"
    return ctx.obj


def _fail(error: CliUserError) -> NoReturn:
    """Error de usuario: mensaje claro en stderr y exit code 1 (contracts §5)."""
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(1)


def _setup_logging(settings: Settings) -> None:
    """Logs SIEMPRE a stderr (contracts §5): stdout queda para el JSON de salida.

    Solo configura si la raíz no tiene handlers: en tests (pytest) el logging
    capturado de pytest ya está instalado y no se toca; en producción se
    instala un StreamHandler sobre el `sys.stderr` actual.
    """
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    app()
