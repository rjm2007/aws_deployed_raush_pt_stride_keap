-- Keep cadence linkage intact while pre-020 API instances finish rolling over.

create or replace function public.set_outreach_cadence_version()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.cadence_version_id is null and new.cadence_step_id is not null then
    select cadence_version_id into new.cadence_version_id
    from public.cadence_steps
    where id = new.cadence_step_id;
  end if;
  return new;
end;
$$;

update public.outreach_events oe set cadence_version_id=cs.cadence_version_id
from public.cadence_steps cs
where cs.id=oe.cadence_step_id and oe.cadence_version_id is null;

drop trigger if exists trg_outreach_cadence_version on public.outreach_events;
create trigger trg_outreach_cadence_version
before insert or update of cadence_step_id on public.outreach_events
for each row execute function public.set_outreach_cadence_version();
