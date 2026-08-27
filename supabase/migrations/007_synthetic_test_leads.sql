-- Keep synthetic test records explicit so compressed cadence rules can never
-- affect ordinary development or production-like leads.
alter table public.leads
  add column if not exists is_test boolean not null default false,
  add column if not exists test_run_id uuid;

create index if not exists idx_leads_test_run
  on public.leads(test_run_id)
  where is_test;

comment on column public.leads.is_test is
  'True only for synthetic local integration tests. Enables cadence time compression when TEST_MODE=true.';
comment on column public.leads.test_run_id is
  'Groups synthetic leads created by one local test run.';
