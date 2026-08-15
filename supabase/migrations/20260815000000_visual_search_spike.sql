-- ============================================================================
-- PR-006 · Visual Search Spike — pgvector + esquema + índices + RLS
-- ----------------------------------------------------------------------------
-- Spec/Req : FR-006, FR-007, FR-008, FR-018, SC-005 · ADR-0004, ADR-0006
-- Modelo   : specs/001-visual-search-spike/data-model.md
-- Depende  : PR-005 (SigLIP2 → D = 768, vector(768))
-- ----------------------------------------------------------------------------
-- Idempotente por diseño: create ... if not exists + drop/create trigger.
-- ============================================================================

-- ADR-0004: pgvector (extensión vector). Idempotente.
create extension if not exists vector;

-- PKs uuid con gen_random_uuid() (disponible en core desde PG13; se asegura
-- pgcrypto por consistencia con la migración base).
create extension if not exists pgcrypto;

-- ============================================================================
-- videos — FR-007 (estado), FR-008 (UNIQUE local_ref), FR-014 (excluded),
--          FR-018 (solo metadatos; el vídeo nunca se persiste)
-- ============================================================================
create table if not exists public.videos (
  id          uuid primary key default gen_random_uuid(),
  local_ref   text not null,
  duration_ms int,
  status      text not null default 'discovered',
  frame_count int not null default 0,
  excluded    boolean not null default false,
  error       text,
  indexed_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint uq_videos_local_ref unique (local_ref),
  constraint chk_videos_status check (
    status in ('discovered', 'pending', 'indexing', 'indexed', 'failed')
  )
);

-- Filtro por estado (data-model.md).
create index if not exists idx_videos_status on public.videos (status);

-- updated_at automático (data-model.md).
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_videos_set_updated_at on public.videos;
create trigger trg_videos_set_updated_at
  before update on public.videos
  for each row execute function public.set_updated_at();

-- ============================================================================
-- frames — FR-006 (ANN), FR-008 (idempotencia), FR-018 (sin media física)
-- ============================================================================
create table if not exists public.frames (
  id           uuid primary key default gen_random_uuid(),
  video_id     uuid not null,
  timestamp_ms int,
  frame_seq    int not null,
  phash        bigint not null,
  embedding    vector(768) not null,
  width        int,
  height       int,
  source_kind  text not null default 'video_frame',
  created_at   timestamptz not null default now(),
  constraint fk_frames_video foreign key (video_id)
    references public.videos (id) on delete cascade,
  constraint uq_frames_video_frame_seq unique (video_id, frame_seq),
  constraint uq_frames_video_timestamp_ms unique (video_id, timestamp_ms),
  constraint chk_frames_source_kind check (
    source_kind in ('video_frame', 'storyboard', 'thumbnail')
  )
);

-- HNSW ANN sobre vector(768) con coseno (FR-006 · ADR-0004).
create index if not exists idx_frames_embedding_hnsw
  on public.frames using hnsw (embedding vector_cosine_ops);

-- pHash: near-exact / prefiltro por distancia de Hamming (FR-004 · ADR-0005).
create index if not exists idx_frames_phash on public.frames (phash);

-- Agrupación/borrado por vídeo (FR-010/FR-013).
create index if not exists idx_frames_video_id on public.frames (video_id);

-- ============================================================================
-- searches — registro analítico de consultas (FR-018: no almacena la media de
-- consulta; ADR-0006)
-- ============================================================================
create table if not exists public.searches (
  id            uuid primary key default gen_random_uuid(),
  search_type   text not null,
  processing_ms int not null,
  results_count int not null,
  created_at    timestamptz not null default now(),
  constraint chk_searches_search_type check (search_type in ('image'))
);

-- ============================================================================
-- RLS deny-by-default — data-model.md "RLS y seguridad".
-- El servicio Python accede con service_role (BYPASSRLS, solo servidor).
-- Sin políticas y sin privilegios para anon/authenticated: NUNCA expuesto a
-- cliente. No se conceden grants de ningún tipo.
-- ============================================================================
alter table public.videos enable row level security;
alter table public.frames enable row level security;
alter table public.searches enable row level security;

revoke all on public.videos from anon, authenticated;
revoke all on public.frames from anon, authenticated;
revoke all on public.searches from anon, authenticated;
