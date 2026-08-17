"""Configuración del servicio API de búsqueda (PR-054/055 · FR-002/006 · SEC-001/006 · ADR-0012).

Los valores se inyectan por variables de entorno (nunca en el repositorio).
Prefijo de env del servicio: `XTRACE_API_` (p. ej. `XTRACE_API_HOST`); los
convenios compartidos del repo (`SUPABASE_DB_URL`, `XTRACE_EMBEDDING_PROVIDER`,
mismo uso que spike/crawler) se leen sin prefijo vía `validation_alias`.

Secciones: base del bootstrap (PR-054): bind local (SEC-001), DSN de servidor
(SEC-004), proveedor de embeddings (paridad CLI), `work_root` para los
temporales de media de consulta (SEC-005) y allowlist CORS; search (PR-055):
defaults de `POST /search` (top_k/min_score, mismos que la CLI — contracts
§1). La sección TTL de `searches` llega en PR-056.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from xtrace_spike.search import DEFAULT_TOP_K  # type: ignore[import-untyped]
from xtrace_spike.search.ranking import DEFAULT_MIN_SCORE  # type: ignore[import-untyped]

#: Default de `work_root`: directorio de temporales del sistema (gitignored y
#: configurable por env; la media de consulta nunca se persiste — SEC-005).
DEFAULT_WORK_ROOT = Path(tempfile.gettempdir()) / "xtrace-api"


class Settings(BaseSettings):
    """Configuración global del servicio API, leída de variables de entorno."""

    model_config = SettingsConfigDict(
        env_prefix="XTRACE_API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bind local (SEC-001/D3): la API escucha solo en 127.0.0.1; no se expone.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # Supabase (SEC-004): DSN de servidor; convenio compartido del repo
    # (`SUPABASE_DB_URL`, mismo que spike/crawler — sin prefijo). Vacío →
    # la búsqueda usa el backend in-memory (tests/dev).
    supabase_db_url: str = Field(
        default="",
        validation_alias=AliasChoices("SUPABASE_DB_URL", "XTRACE_API_SUPABASE_DB_URL"),
    )

    # Proveedor de embeddings (paridad CLI): `fake` (default; determinista,
    # tests/CI sin torch) | `siglip` (SiglipLocalProvider real del spike;
    # requiere el extra `siglip`, se importa lazy). Convenio del spike
    # (`XTRACE_EMBEDDING_PROVIDER`, sin prefijo).
    embedding_provider: Literal["fake", "siglip"] = Field(
        default="fake",
        validation_alias=AliasChoices("XTRACE_EMBEDDING_PROVIDER", "XTRACE_API_EMBEDDING_PROVIDER"),
    )

    # Temporales de media de consulta (SEC-005): la subida se vuelca a un
    # temporal seguro dentro de `work_root` y se borra inmediatamente tras
    # procesar (PR-055); nunca se persiste ni se loguea la media.
    work_root: Path = DEFAULT_WORK_ROOT

    # Sección search (PR-055 · FR-002 · contracts §1): defaults del endpoint
    # `POST /search` — los MISMOS que la CLI `search` del spike (top_k=10,
    # min_score=0.0). El cliente puede sobreescribirlos por petición con los
    # campos de formulario `top_k`/`min_score` (paridad SC-001).
    search_default_top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=1000)
    search_default_min_score: float = Field(default=DEFAULT_MIN_SCORE, ge=0.0, le=1.0)

    # Allowlist CORS (SEC-001): default el frontend local del skeleton; los
    # orígenes de Vercel Preview se añaden por env del operador si se quiere
    # probar el preview contra la API local (plan §Security strategy).
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


@lru_cache
def get_settings() -> Settings:
    """Instancia única de configuración por proceso (patrón del repo)."""
    return Settings()
