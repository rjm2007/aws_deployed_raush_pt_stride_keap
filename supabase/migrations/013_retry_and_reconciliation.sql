alter table public.call_logs
  add column if not exists outreach_event_id bigint references public.outreach_events(id) on delete set null;

create index if not exists idx_call_logs_outreach_event
  on public.call_logs(outreach_event_id);

alter table public.provider_events
  add column if not exists dead_lettered_at timestamptz;

create index if not exists idx_provider_events_active_retry
  on public.provider_events(next_attempt_at)
  where processed_at is null and dead_lettered_at is null;

alter table public.notification_log
  add column if not exists attempts integer not null default 0,
  add column if not exists next_attempt_at timestamptz not null default now();

create index if not exists idx_notification_retry
  on public.notification_log(next_attempt_at)
  where status = 'queued';

comment on column public.notification_log.attempts is
  'Number of provider submission attempts, including the current sending attempt.';
comment on column public.notification_log.next_attempt_at is
  'Earliest time a safely retryable notification may be claimed again.';
comment on column public.provider_events.dead_lettered_at is
  'Set when durable internal webhook processing exhausts its bounded attempts.';
