-- Bring the supplied pre-project schema forward without replacing its data.
-- This migration is intentionally additive except for check/index definitions.

alter table public.practice_settings
  add column if not exists max_calls_per_lead_per_day smallint not null default 1,
  add column if not exists max_sms_per_lead_per_day smallint not null default 2;

alter table public.leads
  add column if not exists source_system text not null default 'manual',
  add column if not exists external_referral_id text,
  add column if not exists first_name text,
  add column if not exists last_name text,
  add column if not exists email text,
  add column if not exists last_call_outcome text,
  add column if not exists callback_requested_at timestamptz,
  add column if not exists callback_notes text;
alter table public.leads
  drop constraint if exists leads_last_call_outcome_check;
alter table public.leads
  add constraint leads_last_call_outcome_check check(last_call_outcome is null or last_call_outcome in (
    'booked','not_interested','no_answer','voicemail','callback','transferred','manual',
    'call_opt_out','do_not_contact'));

create unique index if not exists idx_leads_source_ref
  on public.leads(practice_id,source_system,external_referral_id)
  where external_referral_id is not null;

alter table public.outreach_events
  add column if not exists settled_at timestamptz,
  add column if not exists vapi_call_id text,
  add column if not exists settled_by text,
  add column if not exists outcome text;
create unique index if not exists idx_outreach_vapi_call_id
  on public.outreach_events(vapi_call_id)
  where vapi_call_id is not null;
alter table public.outreach_events
  drop constraint if exists outreach_events_status_check;
alter table public.outreach_events
  add constraint outreach_events_status_check check(status in (
    'planned','in_flight','attempted','delivered','failed','skipped','unknown'));
alter table public.outreach_events
  drop constraint if exists outreach_events_settled_by_check;
alter table public.outreach_events
  add constraint outreach_events_settled_by_check check(
    settled_by is null or settled_by in ('worker','tool','webhook','sweeper'));
alter table public.outreach_events
  drop constraint if exists outreach_events_outcome_check;
alter table public.outreach_events
  add constraint outreach_events_outcome_check check(outcome is null or outcome in (
    'booked','not_interested','no_answer','voicemail','callback','transferred','manual',
    'call_opt_out','do_not_contact'));

alter table public.provider_events
  add column if not exists processing_error text,
  add column if not exists processing_attempts integer not null default 0,
  add column if not exists next_attempt_at timestamptz not null default now();

alter table public.appointments
  drop constraint if exists appointments_state_check;
alter table public.appointments
  add constraint appointments_state_check check(state in (
    'booking','scheduled','completed','cancelled','no_show','rescheduled','failed','unknown'));
create unique index if not exists idx_one_active_appointment_per_lead
  on public.appointments(lead_id)
  where state in ('booking','scheduled','unknown');

create table if not exists public.notification_log (
  id bigint generated always as identity primary key,
  lead_id uuid references public.leads(id) on delete set null,
  appointment_id bigint references public.appointments(id) on delete set null,
  notification_type text not null,
  channel text not null check(channel in ('sms','call')),
  status text not null,
  provider_ref text,
  payload jsonb,
  error text,
  sent_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.notification_log
  add column if not exists provider_ref text,
  add column if not exists error text,
  add column if not exists delivered_at timestamptz,
  add column if not exists updated_at timestamptz not null default now();
alter table public.notification_log
  drop constraint if exists notification_log_status_check;
alter table public.notification_log
  add constraint notification_log_status_check check(status in (
    'queued','sending','sent','delivered','undelivered','failed','skipped','unknown'));

-- A single durable row owns the complete send lifecycle. If development data
-- already contains duplicates this statement fails transactionally instead of
-- choosing a record to delete without operator review.
drop index if exists public.idx_notification_log_dedupe;
drop index if exists public.idx_notification_dedupe;
create unique index idx_notification_dedupe
  on public.notification_log(appointment_id,notification_type,channel)
  where appointment_id is not null;

create table if not exists public.integration_outbox (
  id bigint generated always as identity primary key,
  event_id text not null unique,
  event_type text not null,
  aggregate_id text not null,
  payload jsonb not null,
  status text not null default 'pending' check(status in ('pending','sending','delivered','dead')),
  attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error text,
  delivered_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.integration_outbox
  add column if not exists updated_at timestamptz not null default now();

alter table public.notification_log enable row level security;
alter table public.integration_outbox enable row level security;

drop trigger if exists trg_notification_updated on public.notification_log;
create trigger trg_notification_updated before update on public.notification_log
  for each row execute function public.set_updated_at();
drop trigger if exists trg_outbox_updated on public.integration_outbox;
create trigger trg_outbox_updated before update on public.integration_outbox
  for each row execute function public.set_updated_at();

create index if not exists idx_provider_events_retry
  on public.provider_events(next_attempt_at)
  where processed_at is null;
create index if not exists idx_outreach_events_inflight
  on public.outreach_events(updated_at)
  where status='in_flight';
create index if not exists idx_outbox_pending
  on public.integration_outbox(next_attempt_at)
  where status='pending';
