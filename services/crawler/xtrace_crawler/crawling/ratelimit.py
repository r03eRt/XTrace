"""Rate limiter por fuente (PR-022 · FR-009 · SC-005 · contracts §4 · Decisión D5).

Limitador async por fuente que combina dos límites:

- **intervalo mínimo** entre peticiones (`min_interval_ms`), y
- **ráfaga sostenida** (`max_rps`) mediante un token bucket con capacidad de
  ráfaga de 1 segundo de tokens y refill continuo a `max_rps` tokens/s.

El **jitter es aditivo**: solo suma a la espera base, por lo que nunca puede
violar el intervalo mínimo (lo exige el contrato de tests de PR-022).

`RateLimitSpec` NO se redefine aquí: su definición canónica única vive en
`adapters/base.py` (contracts §1 · alineación exigida por la revisión de la Ola A
a PR-030) y este módulo la importa/re-exporta — `config.py` (PR-022) sigue
resolviendo `RateLimitSpec` desde este módulo.

El limiter es **puro**: reloj, sleeper y RNG inyectables → tests deterministas
sin dormir de verdad. Las esperas son medibles/contables vía `RateLimiter.stats`
(SC-005, NFR-004).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Re-export explícito (`as` = convención mypy de re-export): `config.py` (PR-022)
# sigue resolviendo `RateLimitSpec` desde este módulo; la definición canónica
# única vive en `adapters/base.py` (contracts §1 · alineación Ola A exigida a PR-030).
from xtrace_crawler.adapters.base import RateLimitSpec as RateLimitSpec


@dataclass
class RateLimitStats:
    """Contabilidad de esperas del limiter (SC-005: esperas medibles/contables)."""

    requests: int = 0
    waits: int = 0
    total_wait_seconds: float = 0.0
    last_wait_seconds: float = 0.0


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


async def _asyncio_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class RateLimiter:
    """Limitador async por fuente: intervalo mínimo + ráfaga sostenida + jitter aditivo.

    Uso: una instancia por fuente, compartida por el worker/adaptador de esa
    fuente. `acquire()` espera lo necesario (durmiendo con el sleeper inyectado)
    y devuelve los segundos esperados; `stats` expone la contabilidad para
    logs/observabilidad (SC-005).

    Inyección (tests sin dormir de verdad): `clock` (reloj monotónico),
    `sleeper` (corrutina de espera) y `rng` (jitter determinista).
    """

    def __init__(
        self,
        spec: RateLimitSpec | None = None,
        *,
        source: str | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
        jitter_factor: float = 0.1,
    ) -> None:
        if jitter_factor < 0:
            raise ValueError("jitter_factor debe ser >= 0 (el jitter es aditivo)")
        effective = spec if spec is not None else RateLimitSpec()
        self.source = source
        self.spec = effective
        self.jitter_factor = jitter_factor
        self.stats = RateLimitStats()
        self._min_interval = effective.min_interval_ms / 1000.0
        self._max_rps = effective.max_rps
        self._capacity = effective.max_rps  # ráfaga: 1 segundo de tokens
        self._tokens = self._capacity
        self._last_refill = 0.0
        self._last_request_at: float | None = None
        self._clock = clock if clock is not None else time.monotonic
        self._sleeper = sleeper if sleeper is not None else _asyncio_sleep
        self._rng = rng if rng is not None else random.Random()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Espera lo necesario para respetar el límite y devuelve los segundos esperados.

        Nunca deja pasar una request que viole el intervalo mínimo ni el ritmo
        sostenido (FR-009/SC-005); el jitter solo puede alargar la espera.
        """
        async with self._lock:
            now = self._clock()
            self._refill(now)
            wait = self._required_wait(now)
            delay = wait + self._jitter(wait)
            self.stats.requests += 1
            if delay > 0:
                self.stats.waits += 1
                self.stats.last_wait_seconds = delay
                self.stats.total_wait_seconds += delay
                await self._sleeper(delay)
                now = self._clock()
                self._refill(now)
            self._tokens -= 1.0
            self._last_request_at = now
            return delay

    def _refill(self, now: float) -> None:
        """Repone tokens según el tiempo transcurrido desde el último refill."""
        self._tokens = min(self._capacity, self._tokens + (now - self._last_refill) * self._max_rps)
        self._last_refill = now

    def _required_wait(self, now: float) -> float:
        """Espera base (sin jitter) exigida por el bucket y el intervalo mínimo."""
        wait_for_token = max(0.0, (1.0 - self._tokens) / self._max_rps)
        wait_interval = 0.0
        if self._last_request_at is not None:
            wait_interval = max(0.0, self._min_interval - (now - self._last_request_at))
        return max(wait_for_token, wait_interval)

    def _jitter(self, wait: float) -> float:
        """Jitter aditivo en [0, wait * jitter_factor]: nunca reduce la espera base."""
        if wait <= 0.0 or self.jitter_factor <= 0.0:
            return 0.0
        return self._rng.uniform(0.0, wait * self.jitter_factor)
