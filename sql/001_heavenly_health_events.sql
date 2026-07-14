-- Default Supabase schema for Heavenly Health Protocol.
-- The onboarding CLI will later support a custom table name, but this is the
-- recommended default for this deployment.

create extension if not exists pgcrypto;

create table if not exists public.heavenly_health_events (
  id uuid primary key default gen_random_uuid(),
  source text not null,
  metric_type text not null,
  event_at timestamptz not null,
  value_numeric numeric,
  value_text text,
  unit text,
  source_record_id text not null,
  metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  received_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint heavenly_health_events_source_record_unique unique (source, source_record_id)
);

create index if not exists idx_heavenly_health_events_event_at
  on public.heavenly_health_events (event_at desc);

create index if not exists idx_heavenly_health_events_source_metric_event_at
  on public.heavenly_health_events (source, metric_type, event_at desc);

alter table public.heavenly_health_events enable row level security;

comment on table public.heavenly_health_events is
  'Private normalized health events for Heavenly Health Protocol. No public RLS policies; access is granted only through the user-controlled MCP/storage integration.';
