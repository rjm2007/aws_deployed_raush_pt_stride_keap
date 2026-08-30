-- Staff dashboard support: patient-scoped call text, audited configuration edits,
-- local SMS overrides, and idempotent manual message requests.

alter table public.call_logs
  add column if not exists transcript_text text,
  add column if not exists summary_text text;

create table if not exists public.lead_message_overrides (
  lead_id uuid not null references public.leads(id) on delete cascade,
  message_template_id bigint not null references public.message_templates(id) on delete cascade,
  body text not null check(char_length(body) between 1 and 1600),
  updated_by text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(lead_id,message_template_id)
);

create table if not exists public.dashboard_sms_requests (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null references public.leads(id) on delete restrict,
  idempotency_key text not null unique,
  body text not null check(char_length(body) between 1 and 1600),
  status text not null default 'pending' check(status in ('pending','sending','sent','failed','unknown')),
  provider_ref text unique,
  requested_by text not null,
  failure_category text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.dashboard_audit_log (
  id bigint generated always as identity primary key,
  practice_id bigint references public.practices(id) on delete set null,
  actor_id text not null,
  actor_email text,
  action text not null,
  entity_type text not null,
  entity_id text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_dashboard_audit_entity
  on public.dashboard_audit_log(entity_type,entity_id,created_at desc);
create index if not exists idx_dashboard_sms_lead
  on public.dashboard_sms_requests(lead_id,created_at desc);

alter table public.lead_message_overrides enable row level security;
alter table public.dashboard_sms_requests enable row level security;
alter table public.dashboard_audit_log enable row level security;

drop trigger if exists trg_lead_message_overrides_updated on public.lead_message_overrides;
create trigger trg_lead_message_overrides_updated before update on public.lead_message_overrides
  for each row execute function public.set_updated_at();
drop trigger if exists trg_dashboard_sms_requests_updated on public.dashboard_sms_requests;
create trigger trg_dashboard_sms_requests_updated before update on public.dashboard_sms_requests
  for each row execute function public.set_updated_at();
