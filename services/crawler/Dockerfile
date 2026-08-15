# Imagen del servicio crawler (CLI interna). PR-019 · FR-003 · ADR-0011.
# La dependencia editable xtrace-spike (ADR-0011) vive en ../search-spike, así
# que el build context es la RAÍZ del repositorio:
#   docker build -f services/crawler/Dockerfile -t xtrace-crawler .
# Run: docker run --rm xtrace-crawler --help
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Spike reutilizado en solo lectura (ADR-0011) + servicio crawler.
COPY services/search-spike/pyproject.toml services/search-spike/README.md /app/services/search-spike/
COPY services/search-spike/xtrace_spike /app/services/search-spike/xtrace_spike
COPY services/crawler/pyproject.toml services/crawler/README.md /app/services/crawler/
COPY services/crawler/xtrace_crawler /app/services/crawler/xtrace_crawler
COPY services/crawler/uv.lock /app/services/crawler/uv.lock

RUN uv sync --frozen --no-dev --project /app/services/crawler

ENV PATH="/app/services/crawler/.venv/bin:$PATH"

ENTRYPOINT ["xtrace-crawler"]
CMD ["--help"]
