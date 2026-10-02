-- Dashboard identity attribution and per-lead activity without an external auth provider.
-- Existing owner text and audit rows remain valid during rollout.

alter table public.leads
  add column if not exists owner_user_id text;

alter table public.leads
  drop constraint if exists leads_owner_user_id_format;
alter table public.leads
  add constraint leads_owner_user_id_format check(
    owner_user_id is null
    or owner_user_id ~ '^[a-z0-9][a-z0-9._-]{2,31}$'
  );

create index if not exists idx_leads_owner_user
  on public.leads(owner_user_id) where owner_user_id is not null;

alter table public.dashboard_audit_log
  add column if not exists actor_name text,
  add column if not exists lead_id uuid;

update public.dashboard_audit_log
set actor_name=actor_email
where actor_name is null and actor_email is not null;

update public.dashboard_audit_log
set lead_id=entity_id::uuid
where lead_id is null
  and entity_type='lead'
  and entity_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';

update public.dashboard_audit_log
set lead_id=(metadata->>'lead_id')::uuid
where lead_id is null
  and metadata->>'lead_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';

create index if not exists idx_dashboard_audit_lead
  on public.dashboard_audit_log(lead_id,created_at desc);

comment on column public.leads.owner_user_id is
  'Server-configured employee ID; owner text remains the display-name snapshot.';
comment on column public.dashboard_audit_log.actor_name is
  'Display-name snapshot recorded when the employee action occurred.';
comment on column public.dashboard_audit_log.lead_id is
  'Denormalized lead attribution retained even when the subject lead is later deleted.';
