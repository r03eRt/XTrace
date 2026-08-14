# Imagen del servicio del spike (CLI interna). PR-001 · FR-017 · ADR-0003.
# Build:  docker build -t xtrace-spike services/search-spike
# Run:    docker run --rm xtrace-spike --help
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY xtrace_spike ./xtrace_spike

RUN pip install --no-cache-dir .

ENTRYPOINT ["xtrace-spike"]
CMD ["--help"]
