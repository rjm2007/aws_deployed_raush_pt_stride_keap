-- SMS agent ("Elliot by text", demo/SMS-Agent-Plan.md, owner 2026-11-02). A patient's text queues one
-- job; the sms-agent-worker waits a short quiet window (3 quick texts get one reply), decides whether
-- to reply at all, runs the agent and sends the reply. "if not exists" throughout: safe to re-run.

-- Who wrote a text. Inbound = 'patient'. Outbound: 'cadence' (a cadence step), 'ai' (the SMS agent),
-- 'staff' (dashboard manual text; the AI stays quiet for that lead a while), 'system' (confirmation
-- and other automatic texts). Rows from before this migration stay null: outbound null = cadence or
-- system, never the AI.
alter table public.sms_messages add column if not exists sender text;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'sms_messages_sender_check') then
    alter table public.sms_messages add constraint sms_messages_sender_check
      check (sender is null or sender in ('patient','cadence','ai','staff','system'));
  end if;
end $$;
create index if not exists idx_sms_messages_lead_time on public.sms_messages(lead_id, occurred_at desc);

-- One row per "reply to this phone". New texts while a job is still pending push its due time out
-- (quiet window) instead of adding a second job; a text arriving while a job runs gets a new job.
create table if not exists public.sms_agent_jobs (
  id bigint generated always as identity primary key,
  lead_id uuid references public.leads(id) on delete set null,
  phone_e164 text not null,
  status text not null default 'pending'
    check (status in ('pending','running','done','skipped','failed')),
  due_at timestamptz not null,
  attempts integer not null default 0,
  claimed_at timestamptz,
  finished_at timestamptz,
  -- Why it ended: 'replied', 'fixed_text', or a skip reason ('do_not_contact', 'staff_took_over',
  -- 'reply_cap', 'agent_disabled', 'not_test_lead', 'call_in_progress', ...). No message text.
  outcome text,
  error text,
  -- The newest inbound text this job answers (all inbound texts up to it are answered together).
  last_inbound_id bigint references public.sms_messages(id) on delete set null,
  -- The outbound reply, once sent.
  reply_message_id bigint references public.sms_messages(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists idx_sms_agent_jobs_one_pending
  on public.sms_agent_jobs(phone_e164) where status = 'pending';
create index if not exists idx_sms_agent_jobs_due
  on public.sms_agent_jobs(due_at) where status = 'pending';
create index if not exists idx_sms_agent_jobs_lead on public.sms_agent_jobs(lead_id, created_at desc);
alter table public.sms_agent_jobs enable row level security;

-- What the agent did in a job (tool name + result code only: no names, dates of birth or text),
-- for the lead's Activity and for debugging.
create table if not exists public.sms_agent_actions (
  id bigint generated always as identity primary key,
  job_id bigint references public.sms_agent_jobs(id) on delete cascade,
  lead_id uuid references public.leads(id) on delete set null,
  tool text not null,
  result_code text,
  created_at timestamptz not null default now()
);
create index if not exists idx_sms_agent_actions_lead on public.sms_agent_actions(lead_id, created_at desc);
alter table public.sms_agent_actions enable row level security;

-- Texts staff can edit in the SMS Template Studio (standalone templates: no cadence step or version,
-- which the Studio already lists and edits). The agent sends them word for word; a deleted or inactive
-- one falls back to the same draft in code (services/sms_agent_texts.py).
insert into public.message_templates(practice_id, key, channel, body)
select p.id, t.key, 'sms', t.body
from public.practices p
cross join (values
  ('sms_agent.intro',
   'Hi {first_name}, this is Elliot from Rausch Physical Therapy! We have your referral and would love to get your first visit on the calendar.' || chr(10) || chr(10) ||
   'I can book it right here by text. Just tell me which days or times work for you.' || chr(10) || chr(10) ||
   'Prefer to book yourself? {booking_link}' || chr(10) ||
   'Or call our front desk: {front_desk_phone}'),
  ('sms_agent.new_person_intro',
   'Hi, this is Elliot from Rausch Physical Therapy! I can help you book your first visit right here by text.' || chr(10) || chr(10) ||
   'Prefer to book yourself? {booking_link}' || chr(10) ||
   'Or call our front desk: {front_desk_phone}'),
  ('sms_agent.booked',
   'You''re all set, {first_name}! {day} at {time} with {therapist} at our {clinic} office ({address}).' || chr(10) || chr(10) ||
   'Your new patient paperwork will be sent over via email, please complete this prior to your visit. Just bring a photo ID.' || chr(10) || chr(10) ||
   'See you then!'),
  ('sms_agent.front_desk',
   'That''s one for our front desk team. You can call them at {front_desk_phone}, or they''ll reach out to you as soon as they''re available.'),
  ('sms_agent.english_only',
   'Sorry, I can only help in English by text. Our front desk can help you at {front_desk_phone}, or they''ll reach out to you.'),
  ('sms_agent.text_only',
   'Sorry, I can only read text messages. Could you type that out for me?'),
  ('sms_agent.unknown_number',
   'Hi, this is Rausch Physical Therapy! You can book your first visit online here: {booking_link}' || chr(10) ||
   'Or call our front desk: {front_desk_phone}')
) as t(key, body)
where p.slug = 'rausch-pt'
  and not exists (
    select 1 from public.message_templates mt
    where mt.practice_id = p.id and mt.key = t.key and mt.cadence_step_id is null
  );
