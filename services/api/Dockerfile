# Imagen del servicio API de búsqueda (FastAPI). PR-054 · FR-006 · ADR-0012.
# La dependencia editable xtrace-spike (ADR-0011/0012) vive en ../search-spike,
# así que el build context es la RAÍZ del repositorio:
#   docker build -f services/api/Dockerfile -t xtrace-api .
# Run (SEC-001: bind local): docker run --rm -p 127.0.0.1:8000:8000 xtrace-api
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Spike reutilizado en solo lectura (ADR-0011/0012) + servicio API.
COPY services/search-spike/pyproject.toml services/search-spike/README.md /app/services/search-spike/
COPY services/search-spike/xtrace_spike /app/services/search-spike/xtrace_spike
COPY services/api/pyproject.toml services/api/README.md /app/services/api/
COPY services/api/xtrace_api /app/services/api/xtrace_api
COPY services/api/uv.lock /app/services/api/uv.lock

RUN uv sync --frozen --no-dev --project /app/services/api

ENV PATH="/app/services/api/.venv/bin:$PATH"

# Solo local (SEC-001/D3): bind 127.0.0.1 por defecto; no exponer 0.0.0.0.
EXPOSE 8000

ENTRYPOINT ["uvicorn", "xtrace_api.main:app", "--host", "127.0.0.1", "--port", "8000"]
