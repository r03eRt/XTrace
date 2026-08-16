-- ============================================================================
-- PR-025 · Source SDK + Crawler — sources + jobs + ampliación de videos (web)
-- ----------------------------------------------------------------------------
-- Spec/Req : FR-006 (jobs FOR UPDATE SKIP LOCKED), FR-012 (estados de vídeo),
--            DATA-001/002/003 · ADR-0010 · specs/002-source-sdk-crawler/data-model.md
-- Depende  : 20260815000000_visual_search_spike.sql (tabla videos, set_updated_at)
-- ----------------------------------------------------------------------------
-- NO destructiva: solo añade tablas/columnas/constraints; las filas del spike
-- (videos locales con local_ref, frames, searches) permanecen intactas (DATA-003).
-- Idempotente por diseño (mismo patrón que el spike): create ... if not exists,
-- drop constraint/trigger + create, create index if not exists.
-- Validación: supabase/tests/source_sdk_crawler_schema.test.sql (pgTAP).
-- ============================================================================

-- ============================================================================
-- sources — DATA-001 · SEC-002 (enabled default false = gate de habilitación)
-- ============================================================================
create table if not exists public.sources (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  adapter    text not null,
  manifest   jsonb not null,
  enabled    boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint uq_sources_name unique (name)
);

-- updated_at automático reutilizando la función del spike (NO se recrea).
drop trigger if exists trg_sources_set_updated_at on public.sources;
create trigger trg_sources_set_updated_at
  before update on public.sources
  for each row execute function public.set_updated_at();

-- ============================================================================
-- jobs — FR-006/DATA-002 (tipos) · ADR-0010 (despacho, lease, backoff)
-- ============================================================================
create table if not exists public.jobs (
  id           uuid primary key default gen_random_uuid(),
  job_type     text not null,
  status       text not null default 'pending',
  source_id    uuid,
  video_id     uuid,
  payload      jsonb not null default '{}'::jsonb,
  attempts     int not null default 0,
  max_attempts int not null default 3,
  not_before   timestamptz not null default now(),
  locked_by    text,
  locked_at    timestamptz,
  error        text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  constraint chk_jobs_job_type check (
    job_type in (
      'DISCOVER', 'FETCH_METADATA', 'INDEX_VIDEO', 'EXTRACT_FRAMES',
      'GENERATE_EMBEDDINGS', 'CHECK_AVAILABILITY', 'REINDEX'
    )
  ),
  constraint chk_jobs_status check (
    status in ('pending', 'running', 'done', 'failed', 'unavailable')
  ),
  constraint fk_jobs_source foreign key (source_id)
    references public.sources (id) on delete set null,
  constraint fk_jobs_video foreign key (video_id)
    references public.videos (id) on delete cascade
);

drop trigger if exists trg_jobs_set_updated_at on public.jobs;
create trigger trg_jobs_set_updated_at
  before update on public.jobs
  for each row execute function public.set_updated_at();

-- Índices de despacho y consulta (data-model.md · ADR-0010).
create index if not exists idx_jobs_dispatch on public.jobs (status, not_before);
create index if not exists idx_jobs_source on public.jobs (source_id);
create index if not exists idx_jobs_type on public.jobs (job_type);

-- ============================================================================
-- videos — ampliación NO destructiva (FR-012, DATA-001/003)
-- Todas las columnas nuevas son NULL por defecto: las filas locales del spike
-- (source_id IS NULL) conviven sin colisión con las web (DATA-003).
-- ============================================================================
alter table public.videos add column if not exists source_id uuid;
alter table public.videos add column if not exists external_id text;
alter table public.videos add column if not exists page_url text;
alter table public.videos add column if not exists title text;
alter table public.videos add column if not exists tags jsonb;
alter table public.videos add column if not exists published_at timestamptz;
alter table public.videos add column if not exists thumbnail_url text;
alter table public.videos add column if not exists preview_url text;
alter table public.videos add column if not exists storyboard_urls jsonb;

-- FK source_id → sources(id) ON DELETE SET NULL (data-model.md).
alter table public.videos drop constraint if exists fk_videos_source;
alter table public.videos add constraint fk_videos_source
  foreign key (source_id) references public.sources (id) on delete set null;

-- CHECK de status ampliado con unavailable/removed (FR-012): drop+add idempotente.
alter table public.videos drop constraint if exists chk_videos_status;
alter table public.videos add constraint chk_videos_status check (
  status in (
    'discovered', 'pending', 'indexing', 'indexed', 'failed',
    'unavailable', 'removed'
  )
);

-- Unicidad web (DATA-001/003): único parcial solo cuando ambos NOT NULL;
-- los vídeos locales (source_id NULL) no colisionan con los web.
create unique index if not exists uq_videos_source_external
  on public.videos (source_id, external_id)
  where source_id is not null and external_id is not null;

-- Índice de consulta por fuente (data-model.md); el existente idx_videos_status
-- se mantiene (no se toca).
create index if not exists idx_videos_source_external
  on public.videos (source_id, external_id);

-- ============================================================================
-- RLS deny-by-default — SEC-003 (paridad con el spike).
-- El servicio Python accede con service_role (solo servidor). Sin políticas y
-- sin privilegios para anon/authenticated: nunca expuesto a cliente.
-- ============================================================================
alter table public.sources enable row level security;
alter table public.jobs enable row level security;

revoke all on public.sources from anon, authenticated;
revoke all on public.jobs from anon, authenticated;
