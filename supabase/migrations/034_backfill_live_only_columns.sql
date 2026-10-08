-- The live database has columns that no earlier migration creates (added by hand or
-- inherited from the original schema). The application reads these two, so a fresh
-- database (local testing, a new environment) failed without them. Definitions match
-- live exactly; on live this migration changes nothing.
alter table public.call_logs add column if not exists cost numeric;
alter table public.leads
  add column if not exists callback_reschedule_count integer not null default 0;
