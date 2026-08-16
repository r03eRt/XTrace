"""Tipos de la cola `jobs` (PR-026 · FR-006/FR-008 · DATA-002 · contracts §3).

Modelan la tabla `jobs` de la migración PR-025 (data-model.md): tipo de job
(DATA-002), estados (contracts §3) y la entidad `Job` como snapshot de una fila.
El despacho y las transiciones de estado viven en `jobs/repo.py` (ADR-0010).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobType(StrEnum):
    """Tipos de job de la cola (DATA-002 · contracts §3 · data-model.md).

    Coherentes con PRODUCT_IDEA.md y el CHECK `chk_jobs_job_type` de la
    migración PR-025.
    """

    DISCOVER = "DISCOVER"
    FETCH_METADATA = "FETCH_METADATA"
    INDEX_VIDEO = "INDEX_VIDEO"
    EXTRACT_FRAMES = "EXTRACT_FRAMES"
    GENERATE_EMBEDDINGS = "GENERATE_EMBEDDINGS"
    CHECK_AVAILABILITY = "CHECK_AVAILABILITY"
    REINDEX = "REINDEX"


class JobStatus(StrEnum):
    """Estados de un job (contracts §3 · data-model.md).

    `PENDING`/`RUNNING` son estados vivos de la cola; `DONE`/`FAILED`/
    `UNAVAILABLE` son terminales: nunca vuelven a despacharse (FR-008, sin
    reintentos infinitos).
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


#: Estados terminales: un job en uno de ellos ya no es elegible ni reintentable (FR-008).
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.UNAVAILABLE}
)


class Job(BaseModel):
    """Snapshot de una fila de `public.jobs` (data-model.md · migración PR-025).

    Validación estricta y sin campos extra (paridad con `adapters/models.py`,
    PR-020): el modelo refleja exactamente las columnas que devuelve `returning *`
    de la BD, con los tipos nativos de psycopg (uuid, datetime, int, dict).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: uuid.UUID
    job_type: JobType
    status: JobStatus
    source_id: uuid.UUID | None = None
    video_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    not_before: datetime
    locked_by: str | None = None
    locked_at: datetime | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("job_type", mode="before")
    @classmethod
    def _coerce_job_type(cls, value: str) -> JobType:
        """Coerce el string de la BD al enum (los enums no se coaccionan en strict)."""
        return JobType(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: str) -> JobStatus:
        """Coerce el string de la BD al enum (los enums no se coaccionan en strict)."""
        return JobStatus(value)
