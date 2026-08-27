create extension if not exists pgcrypto;

create or replace function public.set_updated_at() returns trigger language plpgsql set search_path = '' as $$
begin new.updated_at=now(); return new; end;
$$;

create table if not exists public.practices (
  id bigint generated always as identity primary key,
  name text not null,
  slug text not null unique,
  timezone text not null default 'America/Los_Angeles',
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.practice_settings (
  practice_id bigint primary key references public.practices(id) on delete cascade,
  vapi_assistant_id text,
  vapi_phone_number_id text,
  twilio_from_number text,
  booking_link_url text,
  transfer_number text,
  max_calls_per_lead_per_day smallint not null default 1,
  max_sms_per_lead_per_day smallint not null default 2,
  business_hours jsonb not null default '{
    "1":{"open":"09:00","close":"17:00"},"2":{"open":"09:00","close":"17:00"},
    "3":{"open":"09:00","close":"17:00"},"4":{"open":"09:00","close":"17:00"},
    "5":{"open":"09:00","close":"17:00"},"6":null,"7":null}'::jsonb,
  holidays jsonb not null default '[]'::jsonb,
  stride_location_id bigint not null default 3,
  stride_clinician_ids text not null default '42',
  stride_appointment_type_id bigint not null default 1452,
  stride_default_duration_mins smallint not null default 60,
  stride_case_title text not null default 'Physical Therapy',
  stride_location_timezone text not null default 'America/Los_Angeles',
  stride_booking_enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.cadence_steps (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete cascade,
  step_order smallint not null,
  day_offset smallint not null check(day_offset between 0 and 14),
  channel text not null check(channel in ('call','sms')),
  key text not null,
  description text not null,
  is_active boolean not null default true,
  unique(practice_id,day_offset,step_order)
);

create table if not exists public.message_templates (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete cascade,
  cadence_step_id bigint references public.cadence_steps(id) on delete cascade,
  key text not null,
  channel text not null check(channel in ('call','sms')),
  body text not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.leads (
  id uuid primary key default gen_random_uuid(),
  practice_id bigint not null references public.practices(id) on delete restrict,
  source_system text not null default 'manual',
  external_referral_id text,
  is_test boolean not null default false,
  test_run_id uuid,
  first_name text,
  last_name text,
  full_name text not null,
  phone_e164 text check(phone_e164 is null or phone_e164 ~ '^\+[1-9]\d{7,14}$'),
  phone_original text,
  email text,
  date_of_birth date,
  timezone text,
  line_type text check(line_type is null or line_type in ('mobile','landline','voip','unknown')),
  consent_captured_at timestamptz,
  consent_source text,
  consent_reference text,
  consent_text_version text,
  status text not null default 'new' check(status in (
    'new','in_progress','callback_scheduled','booking_link_sent','booked','declined',
    'transferred_human','needs_attention','invalid_phone','closed_no_response','do_not_contact')),
  status_reason text,
  status_changed_at timestamptz,
  last_call_outcome text check(last_call_outcome is null or last_call_outcome in (
    'booked','not_interested','no_answer','voicemail','callback','transferred','manual',
    'call_opt_out','do_not_contact')),
  cadence_started_on date,
  cadence_state text not null default 'pending' check(cadence_state in (
    'pending','active','paused','completed','terminated')),
  call_opt_out boolean not null default false,
  sms_opt_out boolean not null default false,
  call_attempts integer not null default 0,
  callback_requested_at timestamptz,
  callback_notes text,
  last_contacted_at timestamptz,
  needs_review boolean not null default false,
  review_reason text,
  review_flagged_at timestamptz,
  review_resolved_at timestamptz,
  stride_patient_id bigint,
  stride_case_id bigint,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_leads_phone on public.leads(phone_e164);
create index if not exists idx_leads_active on public.leads(cadence_state) where cadence_state='active';
create index if not exists idx_leads_test_run on public.leads(test_run_id) where is_test;

create table if not exists public.lead_status_history (
  id bigint generated always as identity primary key,
  lead_id uuid not null references public.leads(id) on delete cascade,
  from_status text,
  to_status text not null,
  reason text,
  source text not null,
  changed_at timestamptz not null default now()
);

create table if not exists public.suppressed_numbers (
  phone_e164 text primary key,
  reason text not null,
  source text,
  list_type text not null default 'internal' check(list_type in ('internal','national','state','carrier')),
  last_verified_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table if not exists public.outreach_events (
  id bigint generated always as identity primary key,
  lead_id uuid not null references public.leads(id) on delete cascade,
  cadence_step_id bigint references public.cadence_steps(id) on delete set null,
  attempt_no smallint not null default 1,
  channel text not null check(channel in ('call','sms')),
  day_offset smallint,
  status text not null default 'planned' check(status in (
    'planned','in_flight','attempted','delivered','failed','skipped','unknown')),
  scheduled_for timestamptz,
  executed_at timestamptz,
  settled_at timestamptz,
  failure_reason text,
  provider text check(provider is null or provider in ('vapi','twilio')),
  provider_ref text,
  vapi_call_id text,
  settled_by text check(settled_by is null or settled_by in ('worker','tool','webhook','sweeper')),
  outcome text check(outcome is null or outcome in (
    'booked','not_interested','no_answer','voicemail','callback','transferred','manual',
    'call_opt_out','do_not_contact')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_outreach_due on public.outreach_events(scheduled_for) where status='planned';
create index if not exists idx_outreach_lead on public.outreach_events(lead_id,scheduled_for);

create table if not exists public.provider_events (
  id bigint generated always as identity primary key,
  provider text not null check(provider in ('vapi','twilio','stride')),
  event_id text not null,
  event_type text,
  payload jsonb not null,
  processed_at timestamptz,
  processing_error text,
  processing_attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique(provider,event_id)
);

create table if not exists public.call_logs (
  id bigint generated always as identity primary key,
  lead_id uuid not null references public.leads(id) on delete cascade,
  vapi_call_id text unique,
  dialed_at timestamptz not null,
  ended_at timestamptz,
  duration_seconds integer not null default 0,
  answer_state text not null default 'no_answer',
  ended_reason text,
  created_at timestamptz not null default now()
);

create table if not exists public.sms_messages (
  id bigint generated always as identity primary key,
  lead_id uuid references public.leads(id) on delete set null,
  outreach_event_id bigint unique references public.outreach_events(id) on delete set null,
  direction text not null check(direction in ('outbound','inbound')),
  body text not null,
  occurred_at timestamptz not null,
  delivered_at timestamptz,
  delivery_status text not null default 'queued' check(delivery_status in (
    'queued','sent','delivered','undelivered','failed','received')),
  failure_reason text,
  provider_message_id text unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.appointments (
  id bigint generated always as identity primary key,
  lead_id uuid not null references public.leads(id) on delete restrict,
  practice_id bigint not null references public.practices(id) on delete restrict,
  outreach_event_id bigint references public.outreach_events(id) on delete set null,
  booked_at timestamptz not null default now(),
  booking_source text not null default 'voice_agent',
  state text not null default 'booking' check(state in (
    'booking','scheduled','completed','cancelled','no_show','rescheduled','failed','unknown')),
  start_utc timestamptz,
  end_utc timestamptz,
  stride_appointment_id bigint,
  clinician_id bigint,
  location_id bigint,
  appointment_type_id bigint,
  stride_error text,
  booking_key text,
  confirmed_at timestamptz,
  needs_staff_review boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists idx_one_active_appointment_per_lead
  on public.appointments(lead_id) where state in ('booking','scheduled','unknown');

create table if not exists public.notification_log (
  id bigint generated always as identity primary key,
  lead_id uuid references public.leads(id) on delete set null,
  appointment_id bigint references public.appointments(id) on delete set null,
  notification_type text not null,
  channel text not null check(channel in ('sms','call')),
    status text not null check(status in (
      'queued','sending','sent','delivered','undelivered','failed','skipped','unknown')),
  provider_ref text,
  payload jsonb,
    error text,
    sent_at timestamptz,
    delivered_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
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
create index if not exists idx_outbox_pending on public.integration_outbox(next_attempt_at)
  where status='pending';

do $$ declare table_name text; begin
  foreach table_name in array array['practices','practice_settings','cadence_steps','message_templates',
    'leads','lead_status_history','suppressed_numbers','outreach_events','provider_events','call_logs',
    'sms_messages','appointments','notification_log','integration_outbox'] loop
    execute format('alter table public.%I enable row level security', table_name);
  end loop;
end $$;

drop trigger if exists trg_practices_updated on public.practices;
create trigger trg_practices_updated before update on public.practices
  for each row execute function public.set_updated_at();
drop trigger if exists trg_leads_updated on public.leads;
create trigger trg_leads_updated before update on public.leads
  for each row execute function public.set_updated_at();
drop trigger if exists trg_outreach_updated on public.outreach_events;
create trigger trg_outreach_updated before update on public.outreach_events
  for each row execute function public.set_updated_at();
drop trigger if exists trg_notification_updated on public.notification_log;
create trigger trg_notification_updated before update on public.notification_log
  for each row execute function public.set_updated_at();
drop trigger if exists trg_outbox_updated on public.integration_outbox;
create trigger trg_outbox_updated before update on public.integration_outbox
  for each row execute function public.set_updated_at();
