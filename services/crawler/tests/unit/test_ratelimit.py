"""Tests del rate limiter por fuente (PR-022 · FR-009 · SC-005 · D5 · contracts §4).

Validan:
- FR-009/SC-005: el límite declarado nunca se supera (intervalo mínimo y ráfaga
  sostenida), medido con reloj/sleeper fake (sin dormir de verdad).
- FR-009/D5: los overrides por env (`XTRACE_CRAWLER_RATE_<SOURCE>_*`) ganan a los
  defaults del spec (config.py).
- FR-009: el jitter es aditivo y nunca viola el intervalo mínimo.
- SC-005: las esperas son medibles/contables (stats del limiter).

El limiter es puro: reloj, sleeper y RNG inyectables, por lo que los tests son
deterministas y no dependen del tiempo real.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from pydantic import ValidationError

from xtrace_crawler.config import RateLimitOverride, Settings
from xtrace_crawler.crawling.ratelimit import RateLimiter, RateLimitSpec


class FakeClock:
    """Reloj fake: solo avanza cuando el sleeper fake duerme."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeSleeper:
    """Sleeper fake: registra cada espera y avanza el reloj fake."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds


def _gaps(instants: list[float]) -> list[float]:
    """Distancias entre instantes consecutivos (debe haber al menos 1)."""
    # Zip desplazado a propósito: la segunda lista tiene un elemento menos.
    return [b - a for a, b in zip(instants, instants[1:], strict=False)]


def test_min_interval_between_requests_never_exceeded() -> None:
    """FR-009/SC-005: ninguna pareja de requests queda por debajo del intervalo mínimo."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(
        RateLimitSpec(min_interval_ms=500, max_rps=1000.0),
        clock=clock,
        sleeper=sleeper,
        jitter_factor=0.0,
    )

    async def run() -> list[float]:
        instants: list[float] = []
        for _ in range(4):
            await limiter.acquire()
            instants.append(clock.now)
        return instants

    instants = asyncio.run(run())

    # La primera request es inmediata; las siguientes respetan el mínimo de 500 ms.
    assert instants[0] == 0.0
    assert all(gap >= 0.5 for gap in _gaps(instants))
    assert sleeper.sleeps == [0.5, 0.5, 0.5]


def test_sustained_burst_respects_max_rps() -> None:
    """FR-009/SC-005: la ráfaga inicial es acotada y el ritmo sostenido respeta MAX_RPS."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(
        RateLimitSpec(min_interval_ms=0, max_rps=2.0),
        clock=clock,
        sleeper=sleeper,
        jitter_factor=0.0,
    )

    async def run() -> None:
        for _ in range(8):
            await limiter.acquire()

    asyncio.run(run())

    # Capacidad de ráfaga = 1 segundo de tokens (2 requests inmediatas) y después
    # 1 request cada 0.5 s: 6 esperas de 0.5 s = 3.0 s totales con reloj fake.
    # Solo se registran esperas > 0 (las 2 primeras requests no esperan nada).
    assert sleeper.sleeps == [0.5] * 6
    assert clock.now == 3.0
    assert limiter.stats.requests == 8
    assert limiter.stats.waits == 6
    assert limiter.stats.total_wait_seconds == 3.0


def test_jitter_is_additive_and_never_violates_min_interval() -> None:
    """FR-009: el jitter solo suma a la espera mínima, nunca la reduce."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    rng = random.Random(42)
    limiter = RateLimiter(
        RateLimitSpec(min_interval_ms=200, max_rps=1000.0),
        clock=clock,
        sleeper=sleeper,
        rng=rng,
        jitter_factor=0.5,
    )

    async def run() -> list[float]:
        instants: list[float] = []
        for _ in range(20):
            await limiter.acquire()
            instants.append(clock.now)
        return instants

    instants = asyncio.run(run())

    # Con factor 0.5 el jitter va de 0 a 0.5×200 ms = 100 ms; nunca baja de 200 ms.
    assert all(gap >= 0.2 for gap in _gaps(instants))
    assert all(0.2 <= sleep <= 0.3 for sleep in sleeper.sleeps)
    # Con el seed fijo el jitter se aplica de verdad en al menos una espera.
    assert any(sleep > 0.2 for sleep in sleeper.sleeps)


def test_waits_are_measurable_and_countable() -> None:
    """SC-005: el limiter expone waits contables y tiempo total esperado."""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(
        RateLimitSpec(min_interval_ms=1000, max_rps=1000.0),
        clock=clock,
        sleeper=sleeper,
        jitter_factor=0.0,
    )

    async def run() -> None:
        for _ in range(3):
            await limiter.acquire()

    asyncio.run(run())

    assert limiter.stats.requests == 3
    assert limiter.stats.waits == 2
    assert limiter.stats.total_wait_seconds == 2.0
    assert limiter.stats.last_wait_seconds == 1.0
    # El tiempo total esperado es exactamente lo que avanzó el reloj fake.
    assert limiter.stats.total_wait_seconds == clock.now


def test_env_override_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-009/D5: `XTRACE_CRAWLER_RATE_<SOURCE>_{MIN_INTERVAL_MS,MAX_RPS}` gana al default."""
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_XVIDEOS_MIN_INTERVAL_MS", "250")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_XVIDEOS_MAX_RPS", "4")
    settings = Settings()

    override = settings.rate_limits["xvideos"]
    assert override == RateLimitOverride(min_interval_ms=250, max_rps=4.0)

    effective = settings.rate_limit_for("xvideos", RateLimitSpec(min_interval_ms=1000, max_rps=1.0))
    assert effective.min_interval_ms == 250
    assert effective.max_rps == 4.0


def test_source_without_env_keeps_spec_defaults() -> None:
    """FR-009/D5: sin env para una fuente, los defaults del spec se conservan."""
    settings = Settings()
    spec = RateLimitSpec(min_interval_ms=750, max_rps=2.0)

    assert settings.rate_limit_for("mock", spec) == spec


def test_partial_env_override_keeps_other_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-009/D5: un override parcial solo cambia el campo presente."""
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_FOO_MIN_INTERVAL_MS", "125")
    settings = Settings()

    effective = settings.rate_limit_for("foo", RateLimitSpec(min_interval_ms=500, max_rps=3.0))
    assert effective.min_interval_ms == 125
    assert effective.max_rps == 3.0


def test_invalid_rate_env_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-009: un env de rate inválido falla en la construcción de settings (config errónea)."""
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_XVIDEOS_MIN_INTERVAL_MS", "not-a-number")
    with pytest.raises(ValidationError):
        Settings()


def test_rate_env_source_ignores_unrelated_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-009: vars que no matchean el patrón de rate no tocan la config."""
    monkeypatch.setenv("XTRACE_CRAWLER_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("XTRACE_CRAWLER_RATE_XVIDEOS_FOO", "1")
    settings = Settings()

    # Estructura base de PR-019 intacta (config.py base sin romper).
    assert settings.log_level == "DEBUG"
    assert settings.rate_limits == {}
