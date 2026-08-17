"""DI del servicio de búsqueda (PR-055 · FR-013 · DATA-003 · paridad CLI SC-001).

Resuelve el backend del índice y el proveedor de embeddings con **exactamente
la misma regla que la CLI del spike** (`xtrace_spike.cli.build_backend` y
`resolve_embedding_provider`): mismo store, misma etiqueta de backend y mismo
provider → la API produce los mismos resultados que la CLI `search` para la
misma configuración (FR-005, SC-001).

La caché por proceso de `build_backend` es la **misma** que usa la CLI: en
modo in-memory el índice sembrado con `xtrace-spike index` es visible para la
API (y viceversa), igual que para `search` de la CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

from xtrace_spike.cli import (  # type: ignore[import-untyped]
    CliBackend,
    build_backend,
    resolve_embedding_provider,
)
from xtrace_spike.embeddings.provider import EmbeddingProvider  # type: ignore[import-untyped]

from xtrace_api.config import get_settings


@dataclass(frozen=True)
class SearchComponents:
    """Backend del índice + proveedor de embeddings para una búsqueda."""

    backend: CliBackend
    embeddings: EmbeddingProvider


def get_search_components() -> SearchComponents:
    """Componentes de búsqueda con la regla de la CLI (paridad SC-001 · FR-013).

    El backend se resuelve según `SUPABASE_DB_URL` (env, igual que la CLI):
    `PgVectorStore` + `PgVideoStateStore` contra el **índice real** si está
    definida (FR-013/DATA-003, sin reindexar); in-memory si no (tests/dev).
    El proveedor se lee de la configuración del servicio (env
    `XTRACE_EMBEDDING_PROVIDER` o `XTRACE_API_EMBEDDING_PROVIDER`, default
    `fake` — convenio del spike; `siglip` opcional con el extra).
    """
    settings = get_settings()
    return SearchComponents(
        backend=build_backend(),
        embeddings=resolve_embedding_provider(settings.embedding_provider),
    )
