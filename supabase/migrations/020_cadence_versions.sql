-- Version global cadences and lead-specific overrides without duplicating the
-- existing normalized cadence/template model.

create table public.cadence_versions (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete cascade,
  lead_id uuid references public.leads(id) on delete cascade,
  version_number integer not null check(version_number > 0),
  name text not null check(length(trim(name)) between 1 and 120),
  status text not null default 'draft' check(status in ('draft','active','archived')),
  source_version_id bigint references public.cadence_versions(id) on delete set null,
  activated_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index cadence_versions_global_number
  on public.cadence_versions(practice_id,version_number) where lead_id is null;
create unique index cadence_versions_local_number
  on public.cadence_versions(lead_id,version_number) where lead_id is not null;
create unique index cadence_versions_one_active_global
  on public.cadence_versions(practice_id) where lead_id is null and status='active';
create unique index cadence_versions_one_active_local
  on public.cadence_versions(lead_id) where lead_id is not null and status='active';

-- Preserve every existing step ID by placing the current practice cadence into
-- Standard v3 before making version ownership required.
insert into public.cadence_versions(practice_id,version_number,name,status,activated_at)
select distinct cs.practice_id,3,'Standard v3','active',now()
from public.cadence_steps cs
where not exists (
  select 1 from public.cadence_versions cv
  where cv.practice_id=cs.practice_id and cv.lead_id is null
);

alter table public.cadence_steps
  add column cadence_version_id bigint references public.cadence_versions(id) on delete restrict;
update public.cadence_steps cs set cadence_version_id=cv.id
from public.cadence_versions cv
where cv.practice_id=cs.practice_id and cv.lead_id is null and cv.status='active'
  and cs.cadence_version_id is null;
alter table public.cadence_steps alter column cadence_version_id set not null;

alter table public.cadence_steps
  drop constraint if exists cadence_steps_practice_id_day_offset_step_order_key;
alter table public.cadence_steps
  drop constraint if exists cadence_steps_unique_slot;
drop index if exists public.cadence_steps_unique_slot;
drop index if exists public.idx_cadence_steps_practice_key;
alter table public.cadence_steps
  drop constraint if exists cadence_steps_day_offset_check;
alter table public.cadence_steps
  add constraint cadence_steps_day_offset_check check(day_offset between 0 and 365),
  add constraint cadence_steps_version_slot unique(cadence_version_id,day_offset,step_order),
  add constraint cadence_steps_version_key unique(cadence_version_id,key);
create index idx_cadence_steps_version on public.cadence_steps(cadence_version_id);

alter table public.message_templates
  add column cadence_version_id bigint references public.cadence_versions(id) on delete restrict;
update public.message_templates mt set cadence_version_id=cs.cadence_version_id
from public.cadence_steps cs
where cs.id=mt.cadence_step_id and mt.cadence_version_id is null;
drop index if exists public.idx_message_templates_practice_key;
create unique index idx_message_templates_version_key
  on public.message_templates(cadence_version_id,key) where cadence_version_id is not null;
create unique index idx_message_templates_cadence_step
  on public.message_templates(cadence_step_id) where cadence_step_id is not null;

alter table public.outreach_events
  add column cadence_version_id bigint references public.cadence_versions(id) on delete restrict;
update public.outreach_events oe set cadence_version_id=cs.cadence_version_id
from public.cadence_steps cs
where cs.id=oe.cadence_step_id and oe.cadence_version_id is null;
create index idx_outreach_cadence_version on public.outreach_events(cadence_version_id);

alter table public.cadence_versions enable row level security;
drop trigger if exists trg_cadence_versions_updated on public.cadence_versions;
create trigger trg_cadence_versions_updated before update on public.cadence_versions
  for each row execute function public.set_updated_at();

comment on table public.cadence_versions is
  'Immutable global cadence versions and lead-scoped local overrides. Only drafts may be edited.';
