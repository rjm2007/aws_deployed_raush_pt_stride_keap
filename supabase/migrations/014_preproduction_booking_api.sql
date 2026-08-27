alter table public.call_logs
  add column if not exists outcome_source text;

alter table public.call_logs
  drop constraint if exists call_logs_outcome_source_check;
alter table public.call_logs
  add constraint call_logs_outcome_source_check check(
    outcome_source is null or outcome_source in ('tool','webhook')
  );

create table if not exists public.integration_events (
  id bigint generated always as identity primary key,
  request_id text not null,
  direction text not null check(direction in ('inbound','outbound')),
  provider text not null,
  operation text not null,
  status text not null,
  http_status integer,
  error_category text,
  created_at timestamptz not null default now()
);

create index if not exists idx_integration_events_request
  on public.integration_events(request_id,created_at);

alter table public.integration_events enable row level security;

comment on table public.integration_events is
  'PHI-free request audit for inbound tools and outbound provider exchanges.';
comment on column public.call_logs.outcome_source is
  'tool when the live call reported its outcome; webhook when end-of-call fallback settled it.';
