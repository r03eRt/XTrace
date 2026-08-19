-- ============================================================================
-- TASK-006-T002 · Refinamiento temporal bajo demanda
-- ----------------------------------------------------------------------------
-- Spec/Req: 006-temporal-refinement · DATA-001..003 · SEC-005 · ADR-0014
-- Solo telemetría server-side: no almacena imágenes, bytes de vídeo ni consultas.
-- Idempotente y no destructiva: searches/videos/frames existentes no se alteran.
-- ============================================================================

create table if not exists public.search_refinements (
  search_id               uuid primary key,
  status                  text not null default 'completed',
  policy_version          text not null,
  candidates_requested    int not null default 0,
  candidates_processed    int not null default 0,
  assets_requested        int not null default 0,
  assets_evaluated        int not null default 0,
  assets_discarded        int not null default 0,
  bytes_downloaded        bigint not null default 0,
  embedding_count         int not null default 0,
  embedding_elapsed_ms    int not null default 0,
  errors_count            int not null default 0,
  improved_count          int not null default 0,
  unchanged_count         int not null default 0,
  elapsed_ms              int not null default 0,
  limit_reason            text,
  created_at              timestamptz not null default now(),
  finished_at             timestamptz,
  constraint fk_search_refinements_search
    foreign key (search_id) references public.searches(id) on delete cascade,
  constraint chk_search_refinements_status check (
    status in ('completed', 'disabled', 'unavailable', 'limited', 'failed')
  ),
  constraint chk_search_refinements_candidates check (
    candidates_requested >= 0 and candidates_processed >= 0
    and candidates_processed <= candidates_requested
  ),
  constraint chk_search_refinements_assets check (
    assets_requested >= 0 and assets_evaluated >= 0 and assets_discarded >= 0
  ),
  constraint chk_search_refinements_cost check (
    bytes_downloaded >= 0 and embedding_count >= 0
    and embedding_elapsed_ms >= 0
  ),
  constraint chk_search_refinements_counts check (
    errors_count >= 0 and improved_count >= 0 and unchanged_count >= 0
    and elapsed_ms >= 0
  )
);

create table if not exists public.search_refinement_evidence (
  search_id          uuid not null,
  video_id           uuid not null,
  source             text not null,
  candidate_rank     int not null,
  asset_kind         text not null,
  asset_url          text not null,
  asset_url_hash     text not null,
  position           int,
  timestamp_ms       int,
  similarity         double precision not null,
  selected           boolean not null default false,
  discarded_reason   text,
  created_at         timestamptz not null default now(),
  constraint fk_refinement_evidence_refinement
    foreign key (search_id) references public.search_refinements(search_id)
    on delete cascade,
  constraint fk_refinement_evidence_video
    foreign key (video_id) references public.videos(id) on delete cascade,
  constraint chk_refinement_evidence_rank check (candidate_rank >= 1),
  constraint chk_refinement_evidence_kind check (asset_kind in ('thumbnail', 'storyboard')),
  constraint chk_refinement_evidence_position check (position is null or position >= 0),
  constraint chk_refinement_evidence_timestamp check (timestamp_ms is null or timestamp_ms >= 0),
  constraint chk_refinement_evidence_similarity check (similarity >= 0 and similarity <= 1),
  constraint chk_refinement_evidence_selected_timestamp check (
    not selected or timestamp_ms is not null
  )
);

create index if not exists idx_search_refinements_status
  on public.search_refinements (status);
create index if not exists idx_search_refinements_policy
  on public.search_refinements (policy_version);
create index if not exists idx_search_refinements_created_at
  on public.search_refinements (created_at);
create index if not exists idx_refinement_evidence_source
  on public.search_refinement_evidence (source, created_at);
create index if not exists idx_refinement_evidence_video
  on public.search_refinement_evidence (video_id, created_at);

-- NULL timestamp participa en la clave con un valor sentinela; URL/timestamp
-- repetidos no cuentan dos veces (DATA-002/FR-013).
create unique index if not exists uq_refinement_evidence_asset
  on public.search_refinement_evidence (
    search_id,
    video_id,
    asset_url_hash,
    coalesce(timestamp_ms, -1)
  );

alter table public.search_refinements enable row level security;
alter table public.search_refinement_evidence enable row level security;

revoke all on public.search_refinements from anon, authenticated;
revoke all on public.search_refinement_evidence from anon, authenticated;
