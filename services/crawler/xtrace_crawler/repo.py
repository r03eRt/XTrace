"""Acceso a PostgreSQL (Supabase local) del crawler — sources/vídeos-web/stats (PR-028).

Capa de datos sobre psycopg (async) para las tablas de la migración PR-025
(`supabase/migrations/20260815000001_source_sdk_crawler.sql` · data-model.md):

- **sources** CRUD: `manifest` jsonb + `enabled` (default false = gate SEC-002).
- **videos web**: upsert idempotente por `(source_id, external_id)` (SC-003) con
  unicidad parcial (DATA-001) y convivencia con los vídeos locales del spike
  (DATA-003); transiciones de estado FR-012 (incl. `unavailable`/`removed`);
  `exclude` (FR-013, paridad de semántica con `xtrace_spike.repo.PgRepo.exclude`).
- **stats** (FR-014): jobs por estado/fuente, vídeos por estado, errores recientes
  con causa y — desde PR-035 — la contabilidad del `RateLimiter` por fuente
  (`rate_limits`, SC-005 · NFR-004 · plan §Observability): el pipeline la
  agrega y `stats(rate_limits=...)` la incrusta como sección nueva sin tocar
  las existentes (compatibilidad de JSON).

Acceso con credenciales de servidor (SEC-003): `service_role`/superuser local vía DSN
(`SUPABASE_DB_URL`, mismo convenio que el spike); RLS deny-by-default en BD y sin
grants a anon/authenticated — esta capa nunca se expone a cliente.

Decisión PR-028: `local_ref` de los vídeos web se deriva del **id de fuente** (uuid),
no del nombre (`web:<source_id>:<external_id>`): estable ante renombrados de fuente y
sin colisión posible con `local_ref` locales del spike (DATA-003).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from xtrace_crawler.adapters.base import AdapterManifest
from xtrace_crawler.adapters.models import VideoSource
from xtrace_crawler.crawling.ratelimit import RateLimitStats

#: Variable de entorno del DSN (mismo convenio que `xtrace_spike.repo` y quickstart.md).
DATABASE_URL_ENV = "SUPABASE_DB_URL"

#: Supabase local (config.toml): postgres superuser, RLS bypassed (solo servidor).
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:55322/postgres"

#: Estados de vídeo del CHECK ampliado por PR-025 (FR-012 · data-model.md).
VideoStatus = Literal[
    "discovered",
    "pending",
    "indexing",
    "indexed",
    "failed",
    "unavailable",
    "removed",
]
VIDEO_STATUSES: frozenset[str] = frozenset(
    {"discovered", "pending", "indexing", "indexed", "failed", "unavailable", "removed"}
)

#: Límite por defecto de `stats.recent_errors` (FR-014).
DEFAULT_RECENT_ERRORS_LIMIT = 20

#: Columnas de `videos` leídas por el repo (orden del SELECT de `_video_columns`).
_VIDEO_COLUMNS: tuple[str, ...] = (
    "id",
    "source_id",
    "external_id",
    "local_ref",
    "status",
    "excluded",
    "error",
    "frame_count",
    "title",
    "page_url",
    "duration_ms",
    "thumbnail_url",
    "preview_url",
    "storyboard_urls",
    "tags",
    "published_at",
    "created_at",
    "updated_at",
)

_SOURCE_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "adapter",
    "manifest",
    "enabled",
    "created_at",
    "updated_at",
)


def resolve_dsn() -> str:
    """DSN efectivo: variable de entorno `SUPABASE_DB_URL` o Supabase local (default)."""
    return os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL


def parse_uuid(value: str, field: str) -> uuid.UUID:
    """Valida un id UUID de contrato (mismo patrón que el spike: error temprano)."""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} no es un UUID válido: {value!r}") from exc


class SourceRecord(BaseModel):
    """Fila `sources`: manifest de compliance parseado + flag de habilitación."""

    id: str
    name: str
    adapter: str
    manifest: AdapterManifest
    enabled: bool
    created_at: datetime
    updated_at: datetime


class VideoRecord(BaseModel):
    """Fila `videos` (web o local): estado FR-012 + metadatos normalizados."""

    id: str
    source_id: str | None
    external_id: str | None
    local_ref: str
    status: VideoStatus
    excluded: bool
    error: str | None
    frame_count: int
    title: str | None
    page_url: str | None
    duration_ms: int | None
    thumbnail_url: str | None
    preview_url: str | None
    storyboard_urls: list[str] | None
    tags: list[str] | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobErrorRecord(BaseModel):
    """Error reciente de un job (FR-014): causa + contexto de fuente/vídeo."""

    job_id: str
    job_type: str
    source: str | None
    video_id: str | None
    error: str | None
    updated_at: datetime


class RateLimitStatsRecord(BaseModel):
    """Contabilidad del `RateLimiter` de una fuente para `stats` (PR-035 · SC-005 · NFR-004).

    Vista JSON estable de `RateLimitStats` (crawling/ratelimit.py): requests
    totales a la fuente, esperas impuestas por el rate limit y tiempo total
    esperado en ms — el respeto de límites declarados es medible por fuente
    (plan §Observability · FR-014).
    """

    requests: int
    rate_limit_waits: int
    total_wait_ms: int


def rate_limit_stats_record(stats: RateLimitStats) -> RateLimitStatsRecord:
    """Convierte la contabilidad del limiter a la vista de `stats` (PR-035).

    `total_wait_seconds` → `total_wait_ms` (redondeado): el contrato del JSON
    usa ms como unidad (`total_wait_ms`).
    """
    return RateLimitStatsRecord(
        requests=stats.requests,
        rate_limit_waits=stats.waits,
        total_wait_ms=round(stats.total_wait_seconds * 1000),
    )


class CrawlerStats(BaseModel):
    """Estadísticas básicas del crawler (FR-014 · plan §Observability).

    `rate_limits` (PR-035 · SC-005 · NFR-004): contabilidad del `RateLimiter`
    por fuente que agrega el pipeline (`CrawlerPipeline.rate_limit_stats`) y
    que `stats()` incrusta — sección NUEVA; las anteriores no cambian
    (compatibilidad de JSON, contracts §5).
    """

    jobs_by_status: dict[str, int]
    jobs_by_source: dict[str | None, int]
    videos_by_status: dict[str, int]
    recent_errors: list[JobErrorRecord]
    rate_limits: dict[str, RateLimitStatsRecord] = Field(default_factory=dict)


class CrawlerRepo:
    """Acceso a la DB del crawler (psycopg async, una conexión por operación).

    Las conexiones se abren con `autocommit=True` (mismo patrón que el spike): la
    validación de contrato ocurre en Python antes del SQL; la transaccionalidad de
    claims/lease es del repo de jobs (PR-026).
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or resolve_dsn()

    async def connect(self) -> psycopg.AsyncConnection[Any]:
        """Nueva conexión async (autocommit) contra el DSN del repo."""
        return await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)

    # ------------------------------------------------------------------
    # sources CRUD (DATA-001 · SEC-002)
    # ------------------------------------------------------------------

    async def upsert_source(
        self,
        *,
        name: str,
        adapter: str,
        manifest: AdapterManifest,
        enabled: bool = False,
    ) -> SourceRecord:
        """Crea o actualiza la fuente por `name` (idempotente, DATA-001).

        `enabled` por defecto `False` (gate SEC-002): solo el operador humano lo
        pone a `true` tras la revisión legal (spec 002 · SEC-002).
        """
        manifest_json = Jsonb(manifest.model_dump(mode="json"))
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "insert into public.sources (name, adapter, manifest, enabled) "
                    "values (%s, %s, %s, %s) "
                    "on conflict (name) do update set "
                    "adapter = excluded.adapter, "
                    "manifest = excluded.manifest, "
                    "enabled = excluded.enabled "
                    "returning id::text, name, adapter, manifest, enabled, "
                    "created_at, updated_at",
                    (name, adapter, manifest_json, enabled),
                )
                row = await cur.fetchone()
        assert row is not None
        return SourceRecord(**dict(zip(_SOURCE_COLUMNS, row, strict=True)))

    async def get_source(self, name: str) -> SourceRecord | None:
        """Lee una fuente por nombre; `None` si no existe (DATA-001)."""
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select id::text, name, adapter, manifest, enabled, "
                    "created_at, updated_at from public.sources where name = %s",
                    (name,),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        return SourceRecord(**dict(zip(_SOURCE_COLUMNS, row, strict=True)))

    async def list_sources(self) -> list[SourceRecord]:
        """Lista todas las fuentes registradas, por nombre (DATA-001)."""
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select id::text, name, adapter, manifest, enabled, "
                    "created_at, updated_at from public.sources order by name"
                )
                rows = await cur.fetchall()
        return [SourceRecord(**dict(zip(_SOURCE_COLUMNS, row, strict=True))) for row in rows]

    async def set_source_enabled(self, name: str, enabled: bool) -> bool:
        """Habilita/deshabilita la fuente (aprobación humana, SEC-002).

        Returns:
            True si el flag cambió; False si no existe o ya tenía ese valor.
        """
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.sources set enabled = %s "
                    "where name = %s and enabled is distinct from %s",
                    (enabled, name, enabled),
                )
                return cur.rowcount == 1

    async def delete_source(self, name: str) -> bool:
        """Borra la fuente (los vídeos/jobs quedan con `source_id` NULL, SET NULL)."""
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute("delete from public.sources where name = %s", (name,))
                return cur.rowcount == 1

    # ------------------------------------------------------------------
    # vídeos web (FR-012 · SC-003 · DATA-001/003)
    # ------------------------------------------------------------------

    async def upsert_web_video(self, source_id: str, video: VideoSource) -> VideoRecord:
        """Upsert idempotente de un vídeo web por `(source_id, external_id)` (SC-003).

        Inserta el vídeo con estado `discovered` (FR-012); ante conflicto actualiza
        **solo los metadatos** (título/URLs/assets/tags/publicado) y no toca
        `status`/`excluded`/`error` (decisión PR-028: las transiciones de estado son
        explícitas vía `set_video_status`; el edge case "vídeo reaparecido" lo decide
        el pipeline).

        `local_ref` se deriva como `web:<source_id>:<external_id>` (uuid de fuente:
        estable ante renombrados y sin colisión con locales, DATA-003).
        """
        source_uuid = parse_uuid(source_id, "source_id")
        local_ref = f"web:{source_uuid}:{video.external_id}"
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "insert into public.videos ("
                    "source_id, external_id, local_ref, status, "
                    "page_url, title, duration_ms, thumbnail_url, preview_url, "
                    "storyboard_urls, tags, published_at"
                    ") values (%s, %s, %s, 'discovered', %s, %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict (source_id, external_id) "
                    "where source_id is not null and external_id is not null "
                    "do update set "
                    "page_url = excluded.page_url, "
                    "title = excluded.title, "
                    "duration_ms = excluded.duration_ms, "
                    "thumbnail_url = excluded.thumbnail_url, "
                    "preview_url = excluded.preview_url, "
                    "storyboard_urls = excluded.storyboard_urls, "
                    "tags = excluded.tags, "
                    "published_at = excluded.published_at "
                    "returning id::text, source_id::text, external_id, local_ref, "
                    "status, excluded, error, frame_count, title, page_url, "
                    "duration_ms, thumbnail_url, preview_url, storyboard_urls, tags, "
                    "published_at, created_at, updated_at",
                    (
                        source_uuid,
                        video.external_id,
                        local_ref,
                        video.page_url,
                        video.title,
                        video.duration_ms,
                        video.thumbnail_url,
                        video.preview_url,
                        Jsonb(video.storyboard_urls),
                        Jsonb(video.tags),
                        video.published_at,
                    ),
                )
                row = await cur.fetchone()
        assert row is not None
        return VideoRecord(**dict(zip(_VIDEO_COLUMNS, row, strict=True)))

    async def get_web_video(self, source_name: str, external_id: str) -> VideoRecord | None:
        """Localiza un vídeo web por fuente (nombre canónico) + external_id (FR-012)."""
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select v.id::text, v.source_id::text, v.external_id, v.local_ref, "
                    "v.status, v.excluded, v.error, v.frame_count, v.title, v.page_url, "
                    "v.duration_ms, v.thumbnail_url, v.preview_url, v.storyboard_urls, "
                    "v.tags, v.published_at, v.created_at, v.updated_at "
                    "from public.videos v "
                    "join public.sources s on s.id = v.source_id "
                    "where s.name = %s and v.external_id = %s",
                    (source_name, external_id),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        return VideoRecord(**dict(zip(_VIDEO_COLUMNS, row, strict=True)))

    async def set_video_status(self, video_id: str, status: VideoStatus) -> bool:
        """Transición de estado del vídeo (FR-012, incl. `unavailable`/`removed`).

        Valida el estado contra el CHECK ampliado de FR-012 antes de tocar la BD.

        Returns:
            True si el estado cambió; False si el vídeo no existe o ya estaba en él.
        """
        if status not in VIDEO_STATUSES:
            allowed = ", ".join(sorted(VIDEO_STATUSES))
            raise ValueError(f"estado de vídeo inválido: {status!r} (permitidos: {allowed})")
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.videos set status = %s "
                    "where id = %s and status is distinct from %s",
                    (status, video_uuid, status),
                )
                return cur.rowcount == 1

    # ------------------------------------------------------------------
    # exclusión (FR-013 · paridad con el spike)
    # ------------------------------------------------------------------

    async def exclude(self, video_id: str, *, excluded: bool = True) -> bool:
        """Marca (o desmarca) un vídeo como excluido del índice (FR-013).

        Semántica idéntica a `xtrace_spike.repo.PgRepo.exclude`: solo actualiza
        `videos.excluded`; los frames permanecen en el índice y los filtros de
        exclusión del spike (`ann_search(exclude_videos=True)`, ranking) los ignoran.

        Returns:
            True si el flag cambió; False si el vídeo no existe o ya tenía el valor.
        """
        video_uuid = parse_uuid(video_id, "video_id")
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update public.videos set excluded = %s "
                    "where id = %s and excluded is distinct from %s",
                    (excluded, video_uuid, excluded),
                )
                return cur.rowcount == 1

    # ------------------------------------------------------------------
    # estadísticas (FR-014)
    # ------------------------------------------------------------------

    async def stats(
        self,
        *,
        recent_errors_limit: int = DEFAULT_RECENT_ERRORS_LIMIT,
        rate_limits: dict[str, RateLimitStatsRecord] | None = None,
    ) -> CrawlerStats:
        """Estadísticas básicas del crawler (FR-014 · plan §Observability).

        - `jobs_by_status`: jobs por estado (pending/running/done/failed/unavailable).
        - `jobs_by_source`: jobs por fuente (clave `None` = jobs sin fuente).
        - `videos_by_status`: vídeos por estado (descubiertos/indexados/fallidos/…).
        - `recent_errors`: últimos `recent_errors_limit` jobs `failed`/`unavailable`
          con causa (`error`), más recientes primero.
        - `rate_limits` (PR-035 · SC-005 · NFR-004): contabilidad del
          `RateLimiter` por fuente agregada por el pipeline; se incrusta como
          sección nueva de `CrawlerStats` (campos existentes intactos).
        """
        async with await self.connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select status, count(*) from public.jobs group by status")
                jobs_by_status = {str(row[0]): int(row[1]) for row in await cur.fetchall()}
                await cur.execute(
                    "select s.name, count(*) from public.jobs j "
                    "left join public.sources s on s.id = j.source_id "
                    "group by s.name"
                )
                jobs_by_source = {row[0]: int(row[1]) for row in await cur.fetchall()}
                await cur.execute("select status, count(*) from public.videos group by status")
                videos_by_status = {str(row[0]): int(row[1]) for row in await cur.fetchall()}
                await cur.execute(
                    "select j.id::text, j.job_type, s.name, j.video_id::text, j.error, "
                    "j.updated_at from public.jobs j "
                    "left join public.sources s on s.id = j.source_id "
                    "where j.status in ('failed', 'unavailable') and j.error is not null "
                    "order by j.updated_at desc limit %s",
                    (recent_errors_limit,),
                )
                rows = await cur.fetchall()
        recent_errors = [
            JobErrorRecord(
                job_id=row[0],
                job_type=row[1],
                source=row[2],
                video_id=row[3],
                error=row[4],
                updated_at=row[5],
            )
            for row in rows
        ]
        return CrawlerStats(
            jobs_by_status=jobs_by_status,
            jobs_by_source=jobs_by_source,
            videos_by_status=videos_by_status,
            recent_errors=recent_errors,
            rate_limits=rate_limits or {},
        )
