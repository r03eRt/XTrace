"""Bucle del worker de jobs (PR-027 · FR-006/FR-008/FR-010 · SC-008 · ADR-0010 · contracts §3).

`JobWorker` es el consumidor de la cola `jobs` (PR-026) sobre Postgres:

- **Bucle async con concurrencia configurable** (N tasks): cada task reclama con
  `claim_next` (`FOR UPDATE SKIP LOCKED`, FR-006) y ejecuta el **handler
  registrado para el `job_type`** del job (dispatcher por tipo, DATA-002).
  `run_once` procesa los jobs elegibles actuales (una pasada, útil para tests y
  para el `--once` del CLI, contracts §5); `run_forever` corre hasta `stop`.
- **Transiciones garantizadas**: éxito → `complete` (`done`); fallo → `fail`
  con backoff (`not_before`, jobs/backoff.py PR-023) o `unavailable` definitivo
  (FR-008 · contracts §3); job sin handler → `failed` definitivo (estados
  terminales garantizados — tasks.md PR-027, Done).
- **Aislamiento por fuente (SC-008 · FR-010)**: un fallo de handler NUNCA se
  propaga al bucle ni a otros jobs. Se clasifica con
  `jobs/backoff.classify_error` (PR-023, coordinado con los errores tipados del
  MockAdapter — nota de la revisión PR-021): terminal (removed/404/robots/ToS,
  por mensaje o atributo `terminal=True`) → `unavailable`; transitorio →
  `fail()` (reintento acotado por `max_attempts`, sin reintentos infinitos).
  Solo los errores NO aislados (p. ej. BD caída) detienen el worker (fail-fast
  en `run_forever`, con log crítico).
- **Lease reset periódico** (ADR-0010, crash de worker): los `running` con
  lease vencido vuelven a `pending` — cada `lease_reset_interval_seconds` en
  `run_forever`, y al inicio de cada `run_once`.
- **Handlers base genéricos** de `DISCOVER` y `CHECK_AVAILABILITY`
  (`discover_handler`, `check_availability_handler`): factories con
  dependencias inyectadas (resolución del adapter y callbacks de resultado),
  genéricos y testeables — la lógica concreta de discover (upsert de vídeos,
  encolado de FETCH_METADATA) la cierra PR-030; los tests de este PR usan
  handlers falsos/inyectados.

El módulo depende del contrato de repo (`JobsRepoProtocol`, satisfecho por
`JobsRepo` de PR-026) y de `adapters/base.py` (protocolo `SourceAdapter`); no
depende de ningún adapter concreto (SC-007: añadir una fuente no toca el core).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from xtrace_crawler.adapters.base import SourceAdapter
from xtrace_crawler.adapters.models import VideoAvailability, VideoSource
from xtrace_crawler.jobs.backoff import ErrorClass, classify_error
from xtrace_crawler.jobs.types import Job, JobType

logger = logging.getLogger(__name__)

#: Concurrencia por defecto: nº de tasks de proceso del bucle.
DEFAULT_CONCURRENCY: int = 4
#: Lease por defecto: un job `running` sin actualizar durante este tiempo se
#: considera huérfano (crash) y vuelve a `pending` (ADR-0010).
DEFAULT_LEASE_TIMEOUT_SECONDS: float = 300.0
#: Periodo del lease reset periódico en `run_forever`.
DEFAULT_LEASE_RESET_INTERVAL_SECONDS: float = 60.0
#: Espera del bucle cuando la cola elegible está vacía (polling, ADR-0010).
DEFAULT_POLL_INTERVAL_SECONDS: float = 1.0
#: Tamaño de página por defecto del handler base de DISCOVER (payload sin `limit`).
DEFAULT_DISCOVER_LIMIT: int = 50


class JobsRepoProtocol(Protocol):
    """Contrato de repo que el worker necesita (satisfecho por `JobsRepo`, PR-026).

    Solo las operaciones del ciclo de vida de un job: reclamar, transicionar y
    resetear leases. Permite inyectar un repo fake en tests (NFR-003).
    """

    async def claim_next(self, worker_id: str) -> Job | None: ...

    async def complete(self, job_id: uuid.UUID) -> Job: ...

    async def fail(self, job_id: uuid.UUID, error: str, *, terminal: bool = False) -> Job: ...

    async def unavailable(self, job_id: uuid.UUID, error: str | None = None) -> Job: ...

    async def reset_stale_leases(self, timeout_seconds: float) -> int: ...


#: Handler de un job: ejecuta el trabajo del `job_type` o lanza (el worker
#: clasifica el fallo). El resultado de la transición lo aplica el worker.
JobHandler = Callable[[Job], Awaitable[None]]


class JobWorker:
    """Worker de jobs con concurrencia configurable y aislamiento de fallos (SC-008).

    Uso típico:

        worker = JobWorker(JobsRepo(), concurrency=4)
        worker.register_handler(JobType.DISCOVER, discover_handler(...))
        await worker.run_forever(stop)      # o: await worker.run_once()
    """

    def __init__(
        self,
        repo: JobsRepoProtocol,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        worker_id: str | None = None,
        lease_timeout_seconds: float = DEFAULT_LEASE_TIMEOUT_SECONDS,
        lease_reset_interval_seconds: float = DEFAULT_LEASE_RESET_INTERVAL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Crea el worker sobre `repo` (JobsRepo real o fake en tests).

        Args:
            repo: repositorio de la cola (contrato `JobsRepoProtocol`).
            concurrency: nº de tasks de proceso concurrentes (>= 1).
            worker_id: identidad del lease (`locked_by`); default único por
                instancia (`worker-<uuid>`).
            lease_timeout_seconds: lease máximo de un `running` antes del reset.
            lease_reset_interval_seconds: periodo del lease reset periódico.
            poll_interval_seconds: espera cuando la cola elegible está vacía.

        Raises:
            ValueError: parámetros fuera de rango (uso incorrecto).
        """
        if concurrency < 1:
            raise ValueError(f"concurrency debe ser >= 1; recibido {concurrency}")
        if lease_timeout_seconds <= 0:
            raise ValueError(
                f"lease_timeout_seconds debe ser > 0; recibido {lease_timeout_seconds}"
            )
        if lease_reset_interval_seconds <= 0:
            raise ValueError(
                "lease_reset_interval_seconds debe ser > 0; "
                f"recibido {lease_reset_interval_seconds}"
            )
        if poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds debe ser > 0; recibido {poll_interval_seconds}"
            )
        self._repo = repo
        self._concurrency = concurrency
        self._worker_id = worker_id if worker_id is not None else f"worker-{uuid.uuid4().hex[:8]}"
        self._lease_timeout_seconds = lease_timeout_seconds
        self._lease_reset_interval_seconds = lease_reset_interval_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._handlers: dict[JobType, JobHandler] = {}

    def register_handler(self, job_type: JobType, handler: JobHandler) -> None:
        """Registra el handler de un `job_type` (dispatcher por tipo, DATA-002).

        Raises:
            ValueError: ya hay un handler registrado para ese `job_type`.
        """
        if job_type in self._handlers:
            raise ValueError(f"ya hay un handler registrado para job_type={job_type.value}")
        self._handlers[job_type] = handler

    async def run_once(self) -> int:
        """Procesa los jobs elegibles actuales una vez; devuelve cuántos procesó.

        Primero resetea los leases vencidos (crash de worker → `pending`,
        ADR-0010) y después lanza `concurrency` pasadas concurrentes que
        reclaman y ejecutan hasta agotar la cola elegible. Los fallos de
        handler se aíslan dentro de cada job (SC-008); los errores de la BD
        (repo) se propagan al llamador (no son fallos de fuente) y cancelan las
        pasadas restantes — sin trabajos huérfanos (los jobs reclamados quedan
        `running` y el lease reset los recupera).
        """
        await self._repo.reset_stale_leases(self._lease_timeout_seconds)
        passes = [asyncio.create_task(self._process_pass()) for _ in range(self._concurrency)]
        try:
            processed = await asyncio.gather(*passes)
        except BaseException:
            for task in passes:
                task.cancel()
            await asyncio.gather(*passes, return_exceptions=True)
            raise
        total = sum(processed)
        logger.info(
            "worker %s: pasada completada con %d job(s) procesado(s)", self._worker_id, total
        )
        return total

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Bucle principal: `concurrency` tasks de proceso + lease reset periódico.

        Corre hasta que `stop` se marca (Ctrl+C del CLI, CI) o hasta que una
        task muere por un error NO aislado (p. ej. BD caída): entonces el
        worker entero se detiene con un log crítico (fail-fast) en vez de
        colgarse en silencio con tasks muertas.
        """
        stop_event = stop if stop is not None else asyncio.Event()
        tasks = [
            asyncio.create_task(self._process_loop(stop_event), name=f"{self._worker_id}-proc-{i}")
            for i in range(self._concurrency)
        ]
        tasks.append(
            asyncio.create_task(self._lease_reset_loop(stop_event), name=f"{self._worker_id}-lease")
        )
        waiter = asyncio.create_task(stop_event.wait(), name=f"{self._worker_id}-stop")
        try:
            done, _pending = await asyncio.wait(
                {*tasks, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in tasks:
                if task in done and not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        logger.critical(
                            "worker %s: la task %s murió por un error no aislado (%s) — "
                            "parando el worker",
                            self._worker_id,
                            task.get_name(),
                            error,
                        )
        finally:
            stop_event.set()
            for pending_task in (*tasks, waiter):
                pending_task.cancel()
            await asyncio.gather(*tasks, waiter, return_exceptions=True)

    # -- Bucle interno ---------------------------------------------------------

    async def _process_loop(self, stop: asyncio.Event) -> None:
        """Bucle de una task de proceso: claim → ejecutar → transición (SC-008).

        Solo termina con `stop` o por un error NO aislado (p. ej. BD caída en
        `claim_next`): los fallos de handler se aíslan dentro de `_execute_job`.
        """
        while not stop.is_set():
            job = await self._repo.claim_next(self._worker_id)
            if job is None:
                await self._sleep_until(stop, self._poll_interval_seconds)
                continue
            await self._execute_job(job)

    async def _process_pass(self) -> int:
        """Una pasada: reclama y ejecuta jobs hasta agotar la cola elegible.

        Nunca lanza por fallos de handler: se aíslan en `_execute_job` (SC-008).
        """
        processed = 0
        while True:
            job = await self._repo.claim_next(self._worker_id)
            if job is None:
                return processed
            await self._execute_job(job)
            processed += 1

    async def _lease_reset_loop(self, stop: asyncio.Event) -> None:
        """Lease reset periódico: `running` con lease vencido → `pending` (ADR-0010).

        El primer reset ocurre tras `lease_reset_interval_seconds`. Un fallo
        del reset (p. ej. BD) se registra y se reintenta en el siguiente ciclo:
        el mantenimiento nunca tumba el worker.
        """
        while not stop.is_set():
            await self._sleep_until(stop, self._lease_reset_interval_seconds)
            if stop.is_set():
                return
            try:
                reset = await self._repo.reset_stale_leases(self._lease_timeout_seconds)
            except Exception:
                logger.exception(
                    "worker %s: lease reset fallido; se reintentará en el siguiente ciclo",
                    self._worker_id,
                )
                continue
            if reset:
                logger.warning(
                    "worker %s: lease reset — %d job(s) 'running' con lease vencido "
                    "devueltos a 'pending' (crash de worker)",
                    self._worker_id,
                    reset,
                )

    async def _execute_job(self, job: Job) -> None:
        """Ejecuta el handler de `job` y aplica la transición de estado.

        Aislamiento (SC-008 · FR-010): el fallo de un handler NUNCA se propaga:
        se clasifica con `jobs/backoff.classify_error` y se aplica la
        transición. El job queda en un estado terminal o reintentable — nunca
        colgado en `running` por un fallo del handler. (`CancelledError` sí se
        propaga: el job queda `running` con lease y el lease reset lo recupera.)
        """
        handler = self._handlers.get(job.job_type)
        if handler is None:
            await self._missing_handler(job)
            return
        try:
            await handler(job)
        except Exception as exc:
            await self._handle_failure(job, exc)
        else:
            await self._repo.complete(job.id)
            logger.info(
                "worker %s: job %s (%s) completado", self._worker_id, job.id, job.job_type.value
            )

    async def _missing_handler(self, job: Job) -> None:
        """Un job cuyo `job_type` no tiene handler es misconfiguración: `failed` definitivo.

        Quemar reintentos no tiene sentido (el handler no aparecerá solo): el
        estado terminal se alcanza en el primer intento (FR-008: sin reintentos
        infinitos; tasks.md PR-027: estados terminales garantizados).
        """
        error = f"no hay handler registrado para job_type={job.job_type.value}"
        logger.error("worker %s: job %s: %s → failed (terminal)", self._worker_id, job.id, error)
        await self._repo.fail(job.id, error, terminal=True)

    async def _handle_failure(self, job: Job, exc: Exception) -> None:
        """Clasifica el fallo del handler y aplica la transición (FR-008 · contracts §3).

        - terminal (`classify_error`: removed/404/robots/ToS, por mensaje o
          atributo `terminal=True` — nota revisión PR-021): `unavailable()`
          definitivo, sin reintentos (tasks.md PR-027);
        - transitorio: `fail()` → `pending` con `not_before` programado con
          backoff (jobs/backoff.py PR-023), acotado por `max_attempts`.
        """
        error = _describe_error(exc)
        if classify_error(exc) is ErrorClass.TERMINAL:
            logger.warning(
                "worker %s: job %s (%s): fallo terminal %r → unavailable",
                self._worker_id,
                job.id,
                job.job_type.value,
                error,
            )
            await self._repo.unavailable(job.id, error)
        else:
            logger.warning(
                "worker %s: job %s (%s): fallo transitorio %r → fail con backoff",
                self._worker_id,
                job.id,
                job.job_type.value,
                error,
            )
            await self._repo.fail(job.id, error)

    async def _sleep_until(self, stop: asyncio.Event, seconds: float) -> None:
        """Duerme `seconds` o hasta que `stop` se marque (espera interrumpible)."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


def _describe_error(exc: BaseException) -> str:
    """Mensaje para la columna `error` del job: clase + detalle (observabilidad FR-014)."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# -- Handlers base genéricos (tasks.md PR-027; la lógica concreta la cierra PR-030) ----


def discover_handler(
    *,
    adapter_for: Callable[[Job], SourceAdapter],
    on_discovered: Callable[[Job, list[str]], Awaitable[None]],
    on_next_cursor: Callable[[Job, str], Awaitable[None]],
) -> JobHandler:
    """Handler base genérico de `DISCOVER` (PR-027; PR-030 cierra la lógica concreta).

    Orquesta UNA página de `discover` del adapter de la fuente del job:

    1. resuelve el adapter con `adapter_for(job)` (p. ej. el registry, PR-028);
    2. lee `cursor`/`limit` del payload (default: sin cursor,
       `DEFAULT_DISCOVER_LIMIT`);
    3. `adapter.discover(cursor=..., limit=...)` (FR-001);
    4. delega los ids descubiertos en `on_discovered(job, ids)` y el siguiente
       cursor en `on_next_cursor(job, cursor)` — PR-030 cierra ahí la lógica
       concreta (upsert de vídeos, encolar FETCH_METADATA/el siguiente
       DISCOVER); los tests de este PR usan callbacks falsos.

    Los fallos del adapter no se capturan aquí: se propagan al worker, que los
    clasifica (transitorio → retry con backoff; terminal → `unavailable`).
    """

    async def _handle(job: Job) -> None:
        adapter = adapter_for(job)
        cursor = job.payload.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError(
                f"payload['cursor'] debe ser str o null; recibido {type(cursor).__name__}"
            )
        limit_raw = job.payload.get("limit", DEFAULT_DISCOVER_LIMIT)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"payload['limit'] debe ser un entero; recibido {limit_raw!r}"
            ) from None
        if limit < 1:
            raise ValueError(f"payload['limit'] debe ser >= 1; recibido {limit}")
        page = await adapter.discover(cursor=cursor, limit=limit)
        if page.external_ids:
            await on_discovered(job, page.external_ids)
        if page.next_cursor is not None:
            await on_next_cursor(job, page.next_cursor)

    return _handle


def check_availability_handler(
    *,
    adapter_for: Callable[[Job], SourceAdapter],
    video_for: Callable[[Job], Awaitable[VideoSource]],
    on_result: Callable[[Job, VideoAvailability], Awaitable[None]],
) -> JobHandler:
    """Handler base genérico de `CHECK_AVAILABILITY` (PR-027; PR-030 cierra la lógica concreta).

    Comprueba la disponibilidad del vídeo del job en la fuente:

    1. resuelve el adapter con `adapter_for(job)`;
    2. obtiene el `VideoSource` con `video_for(job)` (p. ej. del repo de
       vídeos, PR-028);
    3. `adapter.check_availability(video)` (FR-001);
    4. delega el resultado en `on_result(job, availability)` — PR-030 aplica el
       estado del VÍDEO (`unavailable`/`removed`, FR-012 · spec edge cases).

    El handler completa el job aunque el resultado sea `unavailable`/`removed`
    (la comprobación SÍ se hizo; el estado del vídeo lo aplica `on_result` y el
    job deja de consumir reintentos). Los fallos del adapter se propagan al
    worker (retry con backoff o `unavailable` según clasificación, SC-008).
    """

    async def _handle(job: Job) -> None:
        adapter = adapter_for(job)
        video = await video_for(job)
        availability = await adapter.check_availability(video)
        await on_result(job, availability)

    return _handle
