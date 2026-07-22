-- Constrain who can reach health data, not just which rows they see.
--
-- 001 and 002 enabled row level security with no policies, which denies anon
-- and authenticated today. That is necessary but not sufficient:
--   * the table owner bypasses RLS unless it is FORCEd;
--   * nothing revoked the schema-level default grants, so a table added later
--     inherits a weaker posture than these two;
--   * service_role carries BYPASSRLS, so an application running with the
--     service-role key is unconstrained by any policy written here.
--
-- This migration closes the first two and creates the scoped role that closes
-- the third. Point SUPABASE_HEALTH_ROLE_KEY at a JWT whose `role` claim is
-- heavenly_health_app and the MCP process stops holding project-wide rights.

alter table public.heavenly_health_events force row level security;
alter table public.heavenly_health_raw_events force row level security;

revoke all on public.heavenly_health_events from anon, authenticated;
revoke all on public.heavenly_health_raw_events from anon, authenticated;

-- Future tables, sequences, and functions in this schema start closed.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'heavenly_health_app') then
    create role heavenly_health_app nologin noinherit;
  end if;
end $$;

-- PostgREST switches into this role after verifying the request JWT.
grant heavenly_health_app to authenticator;
grant usage on schema public to heavenly_health_app;

-- Exactly the operations the protocol performs, and nothing else. Normalized
-- events are upserted; raw provenance is append-only and never updated.
grant select, insert, update on public.heavenly_health_events to heavenly_health_app;
grant select, insert on public.heavenly_health_raw_events to heavenly_health_app;

drop policy if exists heavenly_health_events_app_access on public.heavenly_health_events;
create policy heavenly_health_events_app_access
  on public.heavenly_health_events
  for all
  to heavenly_health_app
  using (true)
  with check (true);

drop policy if exists heavenly_health_raw_events_app_read on public.heavenly_health_raw_events;
create policy heavenly_health_raw_events_app_read
  on public.heavenly_health_raw_events
  for select
  to heavenly_health_app
  using (true);

drop policy if exists heavenly_health_raw_events_app_insert on public.heavenly_health_raw_events;
create policy heavenly_health_raw_events_app_insert
  on public.heavenly_health_raw_events
  for insert
  to heavenly_health_app
  with check (true);

comment on role heavenly_health_app is
  'Scoped PostgREST role for the Heavenly MCP process. Holds rights on the two health tables only; use it instead of service_role so a compromised MCP process cannot read the rest of the project.';
