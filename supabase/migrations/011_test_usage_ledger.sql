create table if not exists public.test_usage_ledger (
  id bigint generated always as identity primary key,
  lead_id uuid references public.leads(id) on delete set null,
  test_run_id uuid,
  provider text not null check(provider in ('vapi','twilio')),
  usage_type text not null check(usage_type in ('call','cadence_sms','booking_confirmation_sms')),
  recipient_e164 text not null,
  provider_ref text not null,
  status text not null default 'accepted',
  outcome text,
  provider_cost numeric(12,6),
  currency text,
  accepted_at timestamptz not null default now(),
  finalized_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(provider,provider_ref)
);

create index if not exists idx_test_usage_ledger_accepted
  on public.test_usage_ledger(accepted_at desc,provider);

alter table public.test_usage_ledger enable row level security;

drop trigger if exists trg_test_usage_ledger_updated on public.test_usage_ledger;
create trigger trg_test_usage_ledger_updated
  before update on public.test_usage_ledger
  for each row execute function public.set_updated_at();

comment on table public.test_usage_ledger is
  'Durable development cost audit for real provider operations on synthetic test leads; excluded from same-name cleanup.';
comment on column public.test_usage_ledger.recipient_e164 is
  'Sensitive test-recipient identifier. Never write it unmasked to client reports or logs.';
