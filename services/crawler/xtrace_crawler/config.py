"""Configuración del servicio crawler (PR-019 · FR-003 · SEC-003; PR-022 · FR-009 · D5).

Los valores se inyectan por variables de entorno (nunca en el repositorio).
Prefijo de env: `XTRACE_CRAWLER_` (p. ej. `XTRACE_CRAWLER_SUPABASE_URL`).

Sección rate limits (PR-022, Decisión D5, contracts §4): defaults en el spec de
rate limit (`RateLimitSpec`, en código) y overrides por env
`XTRACE_CRAWLER_RATE_<SOURCE>_MIN_INTERVAL_MS` / `XTRACE_CRAWLER_RATE_<SOURCE>_MAX_RPS`,
leídos por `_RateLimitEnvSource` (pydantic-settings). La estructura base de PR-019
no cambia; las secciones retries/DB llegan en PR-026.

Sección worker/operación (PR-032, contracts §5): concurrencia y lease timeout del
`run-worker` y límites por defecto de `backfill`/`check-availability`, todos
overrideables por env con prefijo `XTRACE_CRAWLER_` (la base PR-019/022 no se rompe:
nuevos campos con defaults y validación).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from xtrace_crawler.crawling.ratelimit import RateLimitSpec

# Patrón de env del contrato (contracts §4): XTRACE_CRAWLER_RATE_<SOURCE>_{MIN_INTERVAL_MS,MAX_RPS}
_RATE_ENV_RE = re.compile(
    r"^XTRACE_CRAWLER_RATE_(?P<source>[A-Z0-9]+)_(?P<field>MIN_INTERVAL_MS|MAX_RPS)$"
)


class RateLimitOverride(BaseModel):
    """Override por env para una fuente (D5); solo los campos presentes ganan."""

    min_interval_ms: int | None = Field(default=None, ge=0)
    max_rps: float | None = Field(default=None, gt=0)


class _RateLimitEnvSource(PydanticBaseSettingsSource):
    """Fuente pydantic-settings que lee los overrides de rate por fuente del entorno.

    Escanea `os.environ` con el patrón `XTRACE_CRAWLER_RATE_<SOURCE>_{MIN_INTERVAL_MS,MAX_RPS}`
    y devuelve el campo `rate_limits`; pydantic valida/coerciona los valores
    (un env inválido falla al construir `Settings`).
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Esta fuente no resuelve campos individuales: aporta `rate_limits`
        # completo desde `__call__`.
        return (None, "", False)

    def __call__(self) -> dict[str, Any]:
        overrides: dict[str, dict[str, Any]] = {}
        for key, value in os.environ.items():
            match = _RATE_ENV_RE.fullmatch(key)
            if match is None:
                continue
            source = match.group("source").lower()
            entry = overrides.setdefault(source, {})
            if match.group("field") == "MIN_INTERVAL_MS":
                entry["min_interval_ms"] = value
            else:
                entry["max_rps"] = value
        return {"rate_limits": overrides}


class Settings(BaseSettings):
    """Configuración global del crawler, leída de variables de entorno."""

    model_config = SettingsConfigDict(
        env_prefix="XTRACE_CRAWLER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Supabase (SEC-003): `service_role` solo en código de servidor; nunca expuesto.
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Ajustes generales.
    log_level: str = "INFO"
    request_timeout_seconds: float = 30.0

    # Rate limits por fuente (PR-022 · FR-009 · D5 · contracts §4): defaults en el
    # spec de rate limit y overrides por env (leídos por `_RateLimitEnvSource`).
    rate_limits: dict[str, RateLimitOverride] = Field(default_factory=dict)

    # Worker (PR-032 · contracts §5): concurrencia y lease timeout del `run-worker`
    # (paridad con `jobs/worker.py` PR-027: DEFAULT_CONCURRENCY/DEFAULT_LEASE_TIMEOUT_SECONDS).
    worker_concurrency: int = Field(default=4, ge=1)
    job_lease_timeout_seconds: float = Field(default=300.0, gt=0)

    # Límites por defecto de los comandos operativos (PR-032 · contracts §5):
    # `backfill --limit` (paridad con `DEFAULT_DISCOVER_LIMIT`) y
    # `check-availability --limit` cuando no se pasa el flag.
    backfill_default_limit: int = Field(default=50, ge=1)
    check_availability_default_limit: int = Field(default=100, ge=1)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Añade la fuente de overrides de rate sin sustituir la cadena base (PR-019)."""
        return (
            init_settings,
            env_settings,
            _RateLimitEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    def rate_limit_for(self, source: str, defaults: RateLimitSpec | None = None) -> RateLimitSpec:
        """Spec efectivo para `source`: override por env > default (spec o global).

        Sin env para la fuente devuelve `defaults` (p. ej. el del manifest del
        adapter, contracts §1) o el `RateLimitSpec` global con sus defaults en
        código (D5).
        """
        spec = defaults if defaults is not None else RateLimitSpec()
        override = self.rate_limits.get(source.lower())
        if override is None:
            return spec
        return RateLimitSpec(
            min_interval_ms=(
                override.min_interval_ms
                if override.min_interval_ms is not None
                else spec.min_interval_ms
            ),
            max_rps=override.max_rps if override.max_rps is not None else spec.max_rps,
        )


@lru_cache
def get_settings() -> Settings:
    """Instancia única de configuración por proceso (patrón del spike)."""
    return Settings()
