-- Clear review reasons and resolve-with-a-choice (2026-10-22).
--
-- Incident 2026-10-21: two leads were flagged "needs review" (a Name edited in the Sheet, a Day 0
-- text that failed with Twilio 30006). Staff pressed Resume: the steps came back but needs_review
-- stayed, so the worker (CLAIM_SQL skips needs_review) silently sent nothing while the page said the
-- cadence was running. Nothing recorded what was flagged, why, or what staff decided.
--
-- 1. lead_reviews: one row per flag (reason code, plain message, details such as old/new values or
--    the error code, source, when) and its resolution (who, which choice, note, when).
-- 2. Texts off per lead ("Continue with calls only"): leads.sms_disabled. A separate switch, not
--    sms_opt_out: that one means the patient texted STOP, is inherited by a new referral on the
--    number (hand_over_number) and is cleared when an admin unblocks the number.
-- 3. leads.sheet_name_accepted: the Sheet name staff chose to ignore ("Same person, keep old name"),
--    so the next profile sync does not flag the same value again.
-- 4. The migration 030 fail-safe also records a review row, and two triggers on leads keep every
--    other path honest: a flag without a review row gets one (at commit, so the application can
--    write its own detailed row first), and clearing needs_review closes the open rows.
-- 5. Existing flagged leads get a review row from their review_reason text.
--
-- Re-runnable: if not exists, create or replace, drop trigger if exists, guarded inserts.

alter table public.leads
  add column if not exists sms_disabled boolean not null default false,
  add column if not exists sms_disabled_at timestamptz,
  add column if not exists sms_disabled_reason text,
  add column if not exists sheet_name_accepted text;

comment on column public.leads.sms_disabled is
  'Staff chose calls only for this lead (e.g. a landline). The worker never texts it; separate from the patient''s own sms_opt_out.';
comment on column public.leads.sheet_name_accepted is
  'A Sheet name staff reviewed and chose not to use; profile sync does not flag it again.';

-- The Name the Sheet row held at the last profile read: fills in the new name of a name review
-- flagged before this migration (only "Name changed; needs review" was stored).
alter table public.sheet_row_state add column if not exists sheet_name text;

create table if not exists public.lead_reviews (
  id bigint generated always as identity primary key,
  lead_id uuid not null references public.leads(id) on delete cascade,
  code text not null,
  message text not null,
  details jsonb not null default '{}'::jsonb,
  source text not null default 'system',
  outreach_event_id bigint references public.outreach_events(id) on delete set null,
  flagged_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolved_by text,
  resolved_by_name text,
  resolution text,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_lead_reviews_lead on public.lead_reviews(lead_id, flagged_at desc);
create index if not exists idx_lead_reviews_open on public.lead_reviews(lead_id) where resolved_at is null;

alter table public.lead_reviews enable row level security;

-- review_reason text -> reason code, for rows written before this migration and for the fail-safe
-- below. The application passes its own code; this only has the stored words to go on.
create or replace function public.review_code_for_reason(reason text, lead_status text)
returns text
language sql
immutable
set search_path = ''
as $$
  select case
    when reason ilike 'Name and phone number changed%' or reason ilike 'Phone number changed%'
      or reason ilike 'Replaced by Sheet lead%' then 'phone_changed'
    when reason ilike 'Name changed%' then 'name_changed'
    when reason ilike 'Number blocked%' then 'number_blocked'
    when reason ilike 'cadence SMS was not delivered%' then 'sms_failed'
    when reason ilike 'booking link text could not be delivered%' then 'booking_link_failed'
    when reason ilike 'call answered but no outcome%' then 'call_no_outcome'
    when reason ilike 'structured outcome could not be applied%' then 'call_outcome_not_applied'
    when reason ilike 'unrecognized status%' or reason ilike 'call outcome:%' then 'unrecognized_status'
    when reason ilike 'call outcome not reported%' then 'call_result_missing'
    when reason ilike 'SMS delivery status not reported%' then 'sms_unconfirmed'
    when reason ilike 'ambiguous booking-link SMS%' or reason ilike 'ambiguous SMS notification%'
      or reason ilike 'SMS notification%' then 'booking_link_unconfirmed'
    when reason ilike 'manual SMS%' then 'manual_sms_unconfirmed'
    when reason ilike 'ambiguous%' or reason ilike 'worker stopped%'
      or reason ilike 'outreach delivery requires review%' then 'dispatch_unconfirmed'
    when reason ilike 'dispatch failed%' or reason ilike 'dispatch retries exhausted%' then 'dispatch_failed'
    when reason ilike 'moved to review from the board%' then 'moved_by_staff'
    -- The agent's wrong_person note is free text; the status says what happened.
    when lead_status = 'invalid_phone' then 'wrong_person'
    else 'other'
  end
$$;

-- Migration 030's fail-safe, plus a review row naming the step that failed. Still never sets
-- leads.status (the application owns it). A lead whose cadence already ended has nothing left to
-- pause, so it is no longer dragged back into review.
create or replace function public.pause_failed_outreach_for_review()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status is distinct from new.status and new.status in ('failed', 'unknown') then
    update public.leads
    set needs_review = true,
        review_reason = coalesce(review_reason, case
          when new.channel = 'sms' then 'cadence SMS was not delivered'
          else coalesce(new.failure_reason, 'outreach delivery requires review')
        end),
        review_flagged_at = coalesce(review_flagged_at, now()),
        cadence_state = 'paused'
    where id = new.lead_id
      and status not in ('booked', 'declined', 'do_not_contact', 'closed_no_response')
      and cadence_state not in ('completed', 'terminated');

    if found and not exists (
      select 1 from public.lead_reviews r
      where r.lead_id = new.lead_id and r.resolved_at is null
        and r.outreach_event_id = new.id
    ) then
      insert into public.lead_reviews(lead_id, code, message, details, source, outreach_event_id)
      values (
        new.lead_id,
        case
          when new.channel = 'sms' and new.status = 'failed' then 'sms_failed'
          when new.channel = 'sms' then 'sms_unconfirmed'
          when new.status = 'failed' then 'call_failed'
          else 'dispatch_unconfirmed'
        end,
        case
          when new.channel = 'sms' and new.status = 'failed' then 'A cadence text was not delivered'
          when new.channel = 'sms' then 'A cadence text was not confirmed'
          when new.status = 'failed' then 'A cadence call could not be placed'
          else 'A cadence call was not confirmed'
        end,
        jsonb_build_object(
          'channel', new.channel,
          'day_offset', new.day_offset,
          'event_status', new.status,
          'error_code', case when new.failure_reason ~ '^[0-9]{5}$' then new.failure_reason end,
          'failure_reason', left(new.failure_reason, 300)
        ),
        'db',
        new.id
      );
    end if;

    update public.outreach_events
    set status = 'skipped',
        failure_reason = 'paused_for_review',
        updated_at = now()
    where lead_id = new.lead_id
      and status = 'planned';
  end if;
  return new;
end;
$$;

-- Any path that flags a lead without writing a review row (a direct SQL fix, an older worker)
-- still gets one. Deferred to commit: the application writes its own, detailed row in the same
-- transaction, and this only fills the gap when none exists by then.
create or replace function public.lead_review_failsafe()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  current_lead record;
begin
  select id, needs_review, review_reason, review_flagged_at, status
  into current_lead from public.leads where id = new.id;
  if current_lead.id is null or not current_lead.needs_review then
    return null;
  end if;
  if exists (select 1 from public.lead_reviews r where r.lead_id = new.id and r.resolved_at is null) then
    return null;
  end if;
  insert into public.lead_reviews(lead_id, code, message, source, flagged_at)
  values (
    new.id,
    public.review_code_for_reason(current_lead.review_reason, current_lead.status),
    coalesce(nullif(current_lead.review_reason, ''), 'Needs review'),
    'db',
    coalesce(current_lead.review_flagged_at, now())
  );
  return null;
end;
$$;

drop trigger if exists trg_lead_review_failsafe on public.leads;
create constraint trigger trg_lead_review_failsafe
after insert or update of needs_review on public.leads
deferrable initially deferred
for each row when (new.needs_review)
execute function public.lead_review_failsafe();

-- Clearing needs_review closes the open review rows. The dashboard's own resolve closes them first
-- (with who and which choice); this records the rest: Booked, Do Not Contact, a restart, a STOP
-- reply, an unblock, a replaced referral.
create or replace function public.lead_review_close_on_clear()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  update public.lead_reviews
  set resolved_at = now(),
      resolved_by = coalesce(resolved_by, 'system'),
      resolved_by_name = coalesce(resolved_by_name, 'System'),
      resolution = coalesce(resolution, case
        when new.status = 'booked' then 'booked'
        when new.status = 'do_not_contact' then 'do_not_contact'
        when new.status = 'new' then 'restarted'
        when new.status_reason = 'replaced by a newer referral on this number' then 'replaced'
        when new.cadence_state in ('completed', 'terminated') then 'closed'
        else 'cleared'
      end),
      updated_at = now()
  where lead_id = new.id and resolved_at is null;
  return new;
end;
$$;

drop trigger if exists trg_lead_review_close_on_clear on public.leads;
create trigger trg_lead_review_close_on_clear
after update of needs_review on public.leads
for each row when (old.needs_review and not new.needs_review)
execute function public.lead_review_close_on_clear();

-- Leads already waiting for review: one open row each, from the stored reason. A failed text also
-- carries the carrier's error code (30006 = landline), read from its message.
insert into public.lead_reviews(lead_id, code, message, details, source, outreach_event_id, flagged_at)
select
  l.id,
  public.review_code_for_reason(l.review_reason, l.status),
  coalesce(nullif(l.review_reason, ''), 'Needs review'),
  case when public.review_code_for_reason(l.review_reason, l.status) = 'sms_failed' then
    jsonb_strip_nulls(jsonb_build_object(
      'channel', 'sms',
      'day_offset', last_sms.day_offset,
      'error_code', case when last_sms.failure_reason ~ '^[0-9]{5}$' then last_sms.failure_reason end,
      'failure_reason', last_sms.failure_reason))
  else '{}'::jsonb end,
  'legacy',
  case when public.review_code_for_reason(l.review_reason, l.status) = 'sms_failed'
    then last_sms.outreach_event_id end,
  coalesce(l.review_flagged_at, l.status_changed_at, l.updated_at, now())
from public.leads l
left join lateral (
  select sm.failure_reason, sm.outreach_event_id, oe.day_offset
  from public.sms_messages sm
  left join public.outreach_events oe on oe.id = sm.outreach_event_id
  where sm.lead_id = l.id and sm.direction = 'outbound'
    and sm.delivery_status in ('failed', 'undelivered')
  order by sm.occurred_at desc, sm.id desc
  limit 1
) last_sms on true
where l.needs_review
  and not exists (select 1 from public.lead_reviews r where r.lead_id = l.id and r.resolved_at is null);
