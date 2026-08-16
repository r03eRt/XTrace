"""Política de reintentos: backoff exponencial + jitter completo (FR-008 · ADR-0010 · contracts §3).

- `next_attempt_delay`: retraso en segundos hasta el siguiente intento tras un fallo
  **transitorio**. El retraso bruto crece `base * factor ** (attempt - 1)` y queda
  acotado por `cap`; el resultado aplica **jitter completo**: uniforme en `[0, bruto]`
  (evita la sincronización de reintentos entre workers/jobs).
- `ErrorClass` + clasificadores: distingue errores **transitorios** (se reintentan,
  con reintentos acotados por `max_attempts`) de errores **terminales** — 404/removed
  y bloqueo declarado por robots/ToS → `unavailable`/`failed` definitivo, **sin
  reintento** (contracts §3). El default es fail-safe: lo desconocido se trata como
  transitorio; nunca se reintenta infinitamente porque el worker combina esta
  clasificación con `max_attempts` (FR-008).

El módulo no depende de `httpx` ni de la BD: recibe códigos HTTP, mensajes o
excepciones, y se mantiene puro para poder testearlo sin red (NFR-003).
"""

from __future__ import annotations

import random
from enum import StrEnum

#: Retraso base por defecto: 1 s (contracts §3 · ADR-0010).
DEFAULT_BASE_SECONDS: float = 1.0
#: Factor de crecimiento exponencial por defecto (contracts §3 · ADR-0010).
DEFAULT_FACTOR: float = 2.0
#: Tope del retraso por defecto: 1 h (contracts §3 · ADR-0010).
DEFAULT_CAP_SECONDS: float = 3600.0

#: Códigos HTTP terminales: el recurso ya no existe (404, 410 gone).
TERMINAL_HTTP_STATUSES: frozenset[int] = frozenset({404, 410})

#: Marcadores de terminalidad en mensajes de error (contracts §3): contenido retirado
#: ("removed"/404) o bloqueo declarado por robots/ToS.
TERMINAL_MARKERS: tuple[str, ...] = (
    "removed",
    "robots",
    "terms of service",
    "tos",
    "404",
)


class ErrorClass(StrEnum):
    """Clasificación de un fallo para la política de reintentos (contracts §3).

    - `TRANSIENT`: reintentar con backoff (acotado por `max_attempts`, FR-008).
    - `TERMINAL`: no reintentar; el job pasa a `unavailable`/`failed` definitivo.
    """

    TRANSIENT = "transient"
    TERMINAL = "terminal"


#: Fuente de aleatoriedad por defecto (inyectable vía `rng` para determinismo en tests).
_DEFAULT_RNG = random.Random()


def next_attempt_delay(
    attempt: int,
    base: float = DEFAULT_BASE_SECONDS,
    factor: float = DEFAULT_FACTOR,
    cap: float = DEFAULT_CAP_SECONDS,
    *,
    rng: random.Random | None = None,
) -> float:
    """Retraso (s) hasta el siguiente intento tras el fallo número `attempt`.

    `attempt` es el número de intentos ya fallidos (1 = primer fallo). El retraso
    bruto es `min(base * factor ** (attempt - 1), cap)` y el resultado aplica
    **jitter completo**: uniforme en `[0, bruto]`.

    Inyecta `rng` (p. ej. `random.Random(seed)`) para obtener secuencias
    deterministas en tests (NFR-003). Sin `rng` se usa el generador global del
    módulo.
    """
    if attempt < 1:
        raise ValueError(f"attempt debe ser >= 1 (intentos ya fallidos); recibido {attempt}")
    if base <= 0:
        raise ValueError(f"base debe ser > 0; recibido {base}")
    if factor <= 1:
        raise ValueError(f"factor debe ser > 1 (el backoff debe crecer); recibido {factor}")
    if cap <= 0:
        raise ValueError(f"cap debe ser > 0; recibido {cap}")

    raw = _raw_delay(attempt, base, factor, cap)
    generator = rng if rng is not None else _DEFAULT_RNG
    return generator.uniform(0.0, raw)


def classify_http_status(status: int) -> ErrorClass:
    """Clasifica un código HTTP (404/410 terminales; el resto transitorio, contracts §3)."""
    return ErrorClass.TERMINAL if status in TERMINAL_HTTP_STATUSES else ErrorClass.TRANSIENT


def is_terminal_message(message: str) -> bool:
    """True si el mensaje indica contenido retirado o bloqueo robots/ToS (contracts §3)."""
    lowered = message.lower()
    return any(marker in lowered for marker in TERMINAL_MARKERS)


def classify_error(error: BaseException | int | str) -> ErrorClass:
    """Clasifica un error (excepción, código HTTP o mensaje) según contracts §3.

    Default fail-safe: todo lo no reconocido como terminal se trata como
    `TRANSIENT` (se reintenta, acotado por `max_attempts` — FR-008).
    """
    if isinstance(error, int):
        return classify_http_status(error)
    if isinstance(error, str):
        return ErrorClass.TERMINAL if is_terminal_message(error) else ErrorClass.TRANSIENT
    return ErrorClass.TRANSIENT


def _raw_delay(attempt: int, base: float, factor: float, cap: float) -> float:
    """Retraso bruto (sin jitter): `min(base * factor ** (attempt - 1), cap)`.

    Crecimiento paso a paso con `min` por iteración: exacto, nunca desborda a
    infinito aunque `attempt` sea enorme y corta en cuanto alcanza el cap.
    El `min` inicial garantiza que el cap domina también cuando `cap < base`.
    """
    delay = min(base, cap)
    for _ in range(attempt - 1):
        delay = min(delay * factor, cap)
    return delay
