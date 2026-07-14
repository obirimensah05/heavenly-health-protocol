-- Separate restricted source records from normalized analysis-ready events.
-- Raw payloads are never the default agent/LLM read surface.

create extension if not exists pgcrypto;

create table if not exists public.heavenly_health_raw_events (
  id uuid primary key default gen_random_uuid(),
  source text not null,
  resource_type text not null,
  source_record_id text not null,
  event_at timestamptz,
  payload jsonb not null check (jsonb_typeof(payload) in ('object', 'array')),
  payload_sha256 text not null,
  is_synthetic boolean not null default false,
  ingest_mode text not null default 'live'
    check (ingest_mode in ('live', 'backfill', 'manual', 'synthetic_test')),
  received_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint heavenly_health_raw_events_source_record_unique unique (source, source_record_id),
  constraint heavenly_health_raw_events_synthetic_mode_check check (
    (is_synthetic and ingest_mode = 'synthetic_test')
    or (not is_synthetic and ingest_mode <> 'synthetic_test')
  )
);

alter table public.heavenly_health_raw_events enable row level security;

alter table public.heavenly_health_events
  add column if not exists raw_event_id uuid references public.heavenly_health_raw_events(id) on delete set null,
  add column if not exists is_synthetic boolean not null default false,
  add column if not exists ingest_mode text not null default 'live'
    check (ingest_mode in ('live', 'backfill', 'manual', 'synthetic_test'));

do $$
begin
  if exists (select 1 from public.heavenly_health_events where source_record_id is null) then
    raise exception 'Cannot enforce event identity: source_record_id contains null values';
  end if;
  alter table public.heavenly_health_events alter column source_record_id set not null;
end $$;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'heavenly_health_events_synthetic_mode_check'
      and conrelid = 'public.heavenly_health_events'::regclass
  ) then
    alter table public.heavenly_health_events
      add constraint heavenly_health_events_synthetic_mode_check check (
        (is_synthetic and ingest_mode = 'synthetic_test')
        or (not is_synthetic and ingest_mode <> 'synthetic_test')
      );
  end if;
end $$;

update public.heavenly_health_events
set
  source = 'synthetic',
  is_synthetic = true,
  ingest_mode = 'synthetic_test',
  metadata = metadata || jsonb_build_object('synthetic', true, 'schema_version', '1.0')
where source = 'synthetic_test' or metadata->>'synthetic' = 'true';

create index if not exists idx_heavenly_health_events_analysis
  on public.heavenly_health_events (is_synthetic, metric_type, event_at desc);

create index if not exists idx_heavenly_health_raw_events_source_event
  on public.heavenly_health_raw_events (source, event_at desc);

comment on table public.heavenly_health_raw_events is
  'Restricted source records/payloads for audit and re-normalization. Do not expose to general LLM/agent access.';

comment on table public.heavenly_health_events is
  'Normalized analysis-ready health events. Real records have is_synthetic=false; test records have is_synthetic=true and ingest_mode=synthetic_test.';
