"""Tests de la política de reintentos del crawler (PR-023 · FR-008 · ADR-0010 · contracts §3).

Validan `jobs/backoff.py`:
- crecimiento exponencial del retraso acotado por `cap` (FR-008: backoff exponencial,
  sin reintentos infinitos);
- jitter completo: el retraso cae siempre en `[0, delay]` (contracts §3);
- determinismo con seed inyectado (tests repetibles, NFR-003);
- clasificador de errores transitorios vs terminales (contracts §3: 404/removed y
  bloqueo robots/ToS → terminales, sin reintento).
"""

from __future__ import annotations

import random

import pytest

from xtrace_crawler.jobs.backoff import (
    DEFAULT_BASE_SECONDS,
    DEFAULT_CAP_SECONDS,
    DEFAULT_FACTOR,
    ErrorClass,
    classify_error,
    classify_http_status,
    is_terminal_message,
    next_attempt_delay,
)


class _FixedRng:
    """Doble determinista de `random.Random`: `uniform(a, b)` devuelve siempre `b`.

    Con jitter completo en `[0, delay]`, fijar el extremo superior hace el retraso
    exactamente igual al retraso bruto, permitiendo asertar los valores del
    crecimiento exponencial sin aleatoriedad.
    """

    def uniform(self, a: float, b: float) -> float:
        return b


def _raw_delay(attempt: int, base: float, factor: float, cap: float) -> float:
    """Retraso bruto esperado (sin jitter): `min(base * factor ** (attempt - 1), cap)`."""
    return min(base * factor ** (attempt - 1), cap)


# --- Crecimiento exponencial acotado por cap (FR-008) ---------------------------------


def test_growth_is_exponential_with_exact_values() -> None:
    """El retraso bruto crece base·factor^(attempt-1) (FR-008, contracts §3: base 1 s, factor 2)."""
    rng = _FixedRng()
    assert next_attempt_delay(1, rng=rng) == pytest.approx(1.0)
    assert next_attempt_delay(2, rng=rng) == pytest.approx(2.0)
    assert next_attempt_delay(3, rng=rng) == pytest.approx(4.0)
    assert next_attempt_delay(4, rng=rng) == pytest.approx(8.0)


def test_growth_respects_custom_base_and_factor() -> None:
    """Base y factor configurables cambian el crecimiento (FR-008)."""
    rng = _FixedRng()
    assert next_attempt_delay(1, base=0.5, factor=3.0, rng=rng) == pytest.approx(0.5)
    assert next_attempt_delay(2, base=0.5, factor=3.0, rng=rng) == pytest.approx(1.5)
    assert next_attempt_delay(3, base=0.5, factor=3.0, rng=rng) == pytest.approx(4.5)


def test_growth_is_bounded_by_cap() -> None:
    """El retraso nunca supera `cap`, por grandes que sean base/factor/attempt (FR-008)."""
    rng = _FixedRng()
    # 2^4 = 16 > 10 → el quinto intento ya queda acotado por cap.
    assert next_attempt_delay(5, cap=10.0, rng=rng) == pytest.approx(10.0)
    assert next_attempt_delay(50, cap=10.0, rng=rng) == pytest.approx(10.0)
    # Default del contrato: cap 1 h.
    assert next_attempt_delay(100, rng=rng) == pytest.approx(DEFAULT_CAP_SECONDS)
    assert DEFAULT_CAP_SECONDS == pytest.approx(3600.0)
    # Si el cap es menor que la base, domina el cap desde el primer intento.
    assert next_attempt_delay(1, base=5.0, cap=2.0, rng=rng) == pytest.approx(2.0)


def test_growth_never_overflows_for_huge_attempts() -> None:
    """Intentos enormes no desbordan: el resultado sigue acotado por cap (FR-008)."""
    rng = _FixedRng()
    assert next_attempt_delay(10_000, rng=rng) == pytest.approx(DEFAULT_CAP_SECONDS)


def test_defaults_match_contract() -> None:
    """Defaults del contrato: base 1 s, factor 2, cap 1 h (contracts §3 · ADR-0010)."""
    assert DEFAULT_BASE_SECONDS == pytest.approx(1.0)
    assert DEFAULT_FACTOR == pytest.approx(2.0)
    assert DEFAULT_CAP_SECONDS == pytest.approx(3600.0)


def test_invalid_attempt_raises() -> None:
    """`attempt` debe contar intentos ya fallidos (>= 1); 0/negativo es error de uso."""
    with pytest.raises(ValueError, match="attempt"):
        next_attempt_delay(0)
    with pytest.raises(ValueError, match="attempt"):
        next_attempt_delay(-3)


def test_invalid_parameters_raise() -> None:
    """base/cap deben ser > 0 y factor > 1 (el backoff debe crecer, FR-008)."""
    with pytest.raises(ValueError, match="base"):
        next_attempt_delay(1, base=0.0)
    with pytest.raises(ValueError, match="base"):
        next_attempt_delay(1, base=-1.0)
    with pytest.raises(ValueError, match="factor"):
        next_attempt_delay(1, factor=1.0)
    with pytest.raises(ValueError, match="factor"):
        next_attempt_delay(1, factor=0.5)
    with pytest.raises(ValueError, match="cap"):
        next_attempt_delay(1, cap=0.0)


# --- Jitter completo en [0, delay] (contracts §3) -------------------------------------


def test_jitter_always_within_range_with_seed() -> None:
    """Con seed, el retraso cae siempre en [0, delay bruto] (contracts §3)."""
    rng = random.Random(20260815)
    for attempt in range(1, 11):
        raw = _raw_delay(attempt, DEFAULT_BASE_SECONDS, DEFAULT_FACTOR, DEFAULT_CAP_SECONDS)
        for _ in range(50):
            delay = next_attempt_delay(attempt, rng=rng)
            assert 0.0 <= delay <= raw


def test_jitter_always_within_range_without_seed() -> None:
    """Sin rng inyectado (random global), el retraso sigue acotado por [0, delay] (contracts §3)."""
    for attempt in range(1, 9):
        raw = _raw_delay(attempt, DEFAULT_BASE_SECONDS, DEFAULT_FACTOR, DEFAULT_CAP_SECONDS)
        for _ in range(100):
            delay = next_attempt_delay(attempt)
            assert 0.0 <= delay <= raw


def test_jitter_is_not_degenerate() -> None:
    """El jitter no es trivial: para delay > 0 existen muestras > 0 (contracts §3)."""
    rng = random.Random(42)
    samples = [next_attempt_delay(4, rng=rng) for _ in range(200)]  # raw = 8 s
    assert any(s > 0.0 for s in samples)
    assert max(samples) <= 8.0


# --- Determinismo con seed (NFR-003, tests repetibles) --------------------------------


def test_same_seed_produces_same_delays() -> None:
    """Misma semilla → misma secuencia de retrasos (determinismo, NFR-003)."""
    seq_a = [next_attempt_delay(n, rng=random.Random(1234)) for n in range(1, 21)]
    seq_b = [next_attempt_delay(n, rng=random.Random(1234)) for n in range(1, 21)]
    assert seq_a == seq_b


def test_different_seed_produces_different_delays() -> None:
    """Semillas distintas → secuencias distintas (el jitter depende de la semilla)."""
    seq_a = [next_attempt_delay(n, rng=random.Random(1)) for n in range(1, 21)]
    seq_b = [next_attempt_delay(n, rng=random.Random(2)) for n in range(1, 21)]
    assert seq_a != seq_b


# --- Clasificador de errores transitorios vs terminales (contracts §3) ---------------


def test_http_404_and_410_are_terminal() -> None:
    """404 (página de vídeo eliminada) y 410 (gone) → terminales, sin reintento (contracts §3)."""
    assert classify_http_status(404) is ErrorClass.TERMINAL
    assert classify_http_status(410) is ErrorClass.TERMINAL
    assert classify_error(404) is ErrorClass.TERMINAL
    assert classify_error(410) is ErrorClass.TERMINAL


def test_transient_http_statuses_are_retried() -> None:
    """429/timeout/5xx/403 (anti-bot legítimo) → transitorios (FR-008, spec edge cases)."""
    for status in (408, 425, 429, 500, 502, 503, 504, 403):
        assert classify_http_status(status) is ErrorClass.TRANSIENT
        assert classify_error(status) is ErrorClass.TRANSIENT


def test_terminal_messages_do_not_retry() -> None:
    """Mensajes de contenido retirado o bloqueo robots/ToS → terminales (contracts §3)."""
    assert is_terminal_message("video has been removed")
    assert is_terminal_message("This video was removed by the uploader")
    assert is_terminal_message("blocked by robots.txt")
    assert is_terminal_message("access denied per terms of service")
    assert is_terminal_message("ToS violation")
    assert classify_error("video has been removed") is ErrorClass.TERMINAL
    assert classify_error("blocked by robots") is ErrorClass.TERMINAL


def test_unknown_errors_default_to_transient() -> None:
    """Fallos desconocidos (parseo, red, timeout) → transitorios por defecto (fail-safe, FR-008)."""
    assert classify_error(Exception("html structure changed")) is ErrorClass.TRANSIENT
    assert classify_error(TimeoutError("request timed out")) is ErrorClass.TRANSIENT
    assert classify_error(ConnectionError("connection reset")) is ErrorClass.TRANSIENT
    assert classify_error("connection reset by peer") is ErrorClass.TRANSIENT
    assert is_terminal_message("connection reset by peer") is False


def test_error_class_values_are_stable() -> None:
    """Valores de `ErrorClass` estables para logs/BD (DATA-002, estados terminales)."""
    assert ErrorClass.TRANSIENT.value == "transient"
    assert ErrorClass.TERMINAL.value == "terminal"
    assert ErrorClass.TRANSIENT is not ErrorClass.TERMINAL
