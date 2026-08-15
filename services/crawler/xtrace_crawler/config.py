"""Configuración base del servicio crawler (PR-019 · FR-003 · SEC-003).

Los valores se inyectan por variables de entorno (nunca en el repositorio).
Prefijo de env: `XTRACE_CRAWLER_` (p. ej. `XTRACE_CRAWLER_SUPABASE_URL`).

Secciones adicionales (rate limits D5, retries, DB) se añaden en PR-022/PR-026
sobre esta base; aquí solo el bootstrap necesario.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    """Instancia única de configuración por proceso (patrón del spike)."""
    return Settings()
