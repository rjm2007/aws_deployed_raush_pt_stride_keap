-- The hosted development schema predated this project's local migration runner.
-- Record the reconciled files so `rpt migrate` will not replay the baseline.

create table if not exists public.schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
);

insert into public.schema_migrations(version) values
  ('001_initial.sql'),
  ('002_existing_schema_compatibility.sql'),
  ('003_supabase_security_and_indexes.sql'),
  ('004_idempotency_keys.sql')
on conflict(version) do nothing;

alter table public.schema_migrations enable row level security;
