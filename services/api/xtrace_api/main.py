"""Aplicación FastAPI del servicio de búsqueda de XTrace (PR-054 · FR-006 · ADR-0012).

Base del bootstrap: app con CORS (allowlist por env, SEC-001), lifespan que
asegura `work_root` (SEC-005) y `GET /health` (FR-006 · contracts §2) que no
depende de la BD. Los routers (`/search`, `/stats`, `/videos/{id}`) y los
exception handlers estructurados (FR-011) llegan en PR-055/056; el cleanup
TTL de `searches` en el lifespan llega en PR-056.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from xtrace_api import __version__
from xtrace_api.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranque/apagado del servicio (PR-054: asegura `work_root`; TTL en PR-056)."""
    settings = get_settings()
    settings.work_root.mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    """Construye la aplicación FastAPI (testeable; se extiende en PR-055/056)."""
    settings = get_settings()
    app = FastAPI(
        title="XTrace Search API",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS restringido a la allowlist por env (SEC-001): default solo el
    # frontend local del skeleton (`http://localhost:3000`).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Estado del servicio (FR-006 · contracts §2); no depende de la BD."""
        return {"status": "ok", "service": "xtrace-api", "version": __version__}

    return app


app = create_app()
