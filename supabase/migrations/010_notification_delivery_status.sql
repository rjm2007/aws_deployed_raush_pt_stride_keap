alter table public.notification_log
  add column if not exists delivered_at timestamptz;

alter table public.notification_log
  drop constraint if exists notification_log_status_check;

alter table public.notification_log
  add constraint notification_log_status_check check(status in (
    'queued','sending','sent','delivered','undelivered','failed','skipped','unknown'));

comment on column public.notification_log.delivered_at is
  'Set only from an authenticated provider delivery callback.';
