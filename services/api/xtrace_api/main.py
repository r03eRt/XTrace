"""Aplicación FastAPI del servicio de búsqueda de XTrace (PR-054/055 · FR-006/011
· SEC-001 · ADR-0012).

Base del bootstrap (PR-054): app con CORS (allowlist por env, SEC-001),
lifespan que asegura `work_root` (SEC-005) y `GET /health` (FR-006 ·
contracts §2) que no depende de la BD.

PR-055: registra el router `POST /search` y los **exception handlers
estructurados** del contracts §5 (FR-011, mensajes en español — UX-001):
400 (media/petición inválida, body no multipart), 413 (media > 10 MB),
415 (firma MIME no soportada), 503 (índice/BD no disponible) y 500 (fallo
interno). Los routers `/stats` y `/videos/{id}` y el cleanup TTL llegan en
PR-056.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from xtrace_api import __version__
from xtrace_api.config import get_settings
from xtrace_api.media import MediaValidationError
from xtrace_api.routers.search import router as search_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranque/apagado del servicio (PR-054: asegura `work_root`; TTL en PR-056)."""
    settings = get_settings()
    settings.work_root.mkdir(parents=True, exist_ok=True)
    yield


def _error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Cuerpo de error del contracts §5: `{"error", "error_type"}` (UX-001)."""
    return JSONResponse(
        status_code=status_code, content={"error": message, "error_type": error_type}
    )


def create_app() -> FastAPI:
    """Construye la aplicación FastAPI (testeable; se extiende en PR-056)."""
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

    # Router de búsqueda (PR-055); stats/videos se registran en PR-056.
    app.include_router(search_router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Estado del servicio (FR-006 · contracts §2); no depende de la BD."""
        return {"status": "ok", "service": "xtrace-api", "version": __version__}

    # ------------------------------------------------------------------
    # Exception handlers estructurados (FR-011 · contracts §5 · UX-001)
    # ------------------------------------------------------------------

    @app.exception_handler(MediaValidationError)
    def media_validation_handler(_request: Request, exc: MediaValidationError) -> JSONResponse:
        """Media/petición inválida: 400/413/415 del contracts §5.

        El mensaje ya viene en español y sin rutas ni nombres de fichero
        (SEC-005); `error_type` es estable para el frontend.
        """
        return _error_response(exc.status_code, exc.error_type, exc.message)

    @app.exception_handler(RequestValidationError)
    def request_validation_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        """Body no multipart/malformado en un endpoint de subida: 400 (contracts §5).

        FastAPI rechaza con 422 los cuerpos que no son `multipart/form-data`
        (p. ej. JSON) en `POST /search`; el contrato exige 400 con la parte
        `image` ausente → `missing_file_part`.
        """
        return _error_response(
            400,
            "missing_file_part",
            "la petición debe ser multipart/form-data con la parte 'image'",
        )

    @app.exception_handler(psycopg.Error)
    def db_unavailable_handler(request: Request, _exc: psycopg.Error) -> JSONResponse:
        """Índice/BD no disponible: 503 `index_unavailable` (contracts §5).

        Sin detalles de la excepción en la respuesta; el log no incluye media
        (SEC-005), solo la ruta de la petición.
        """
        logger.error(
            "índice/BD no disponible: %s %s (error=%s)",
            request.method,
            request.url.path,
            type(_exc).__name__,
        )
        return _error_response(
            503,
            "index_unavailable",
            "el índice de búsqueda no está disponible en este momento",
        )

    @app.exception_handler(StarletteHTTPException)
    def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Errores HTTP del framework (404/405…): respuesta JSON estándar.

        Los errores de la API (400/404/413/415/503) se emiten con el cuerpo
        del contracts §5; los del framework (p. ej. 404 de ruta desconocida)
        conservan su comportamiento por defecto.
        """
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Fallo interno: 500 `internal_error` (contracts §5), sin filtrar detalles.

        El traceback se registra para el operador; la respuesta no expone
        internals ni media (SEC-005).
        """
        logger.exception("error interno en %s %s", request.method, request.url.path)
        return _error_response(500, "internal_error", "error interno del servidor")

    return app


app = create_app()
