-- Give the live databases the column types the migrations always declared (2026-10-22).
--
-- Production and demo were filled by a copy on 2026-10-10 that stored these columns as TEXT with no
-- foreign keys, while migrations 001/013/023 create them as uuid / bigint with references. Every
-- test database therefore had the declared types and a `text = uuid` comparison only ever failed
-- live (demo Sheet polls on 2026-10-21; the review branch on 2026-10-22). This makes live match.
--
-- Safe to re-run: each column is converted only while it is still text, and each foreign key is
-- added only when the column has none. Converting fails (and rolls back) if a value is not a valid
-- uuid / number; on 2026-10-22 every production value was checked valid before this ran.

set local lock_timeout = '10s';

do $$
declare
  col record;
begin
  for col in
    select * from (values
      ('lead_action_requests', 'request_id',        'uuid'),
      ('lead_action_requests', 'lead_id',           'uuid'),
      ('lead_action_requests', 'practice_id',       'bigint'),
      ('integration_outbox',   'outreach_event_id', 'bigint')
    ) as v(tbl, name, target)
  loop
    if exists (select 1 from information_schema.columns
               where table_schema = 'public' and table_name = col.tbl and column_name = col.name
                 and data_type = 'text') then
      execute format('alter table public.%I alter column %I type %s using nullif(%I, '''')::%s',
                     col.tbl, col.name, col.target, col.name, col.target);
    end if;
  end loop;
end $$;

-- References to rows deleted while there was no foreign key: clear them, exactly what the foreign key's
-- `on delete set null` would have done (the demo had a few; production had none on 2026-10-22).
update public.lead_action_requests a set lead_id = null
 where a.lead_id is not null and not exists (select 1 from public.leads l where l.id = a.lead_id);
update public.integration_outbox o set outreach_event_id = null
 where o.outreach_event_id is not null
   and not exists (select 1 from public.outreach_events e where e.id = o.outreach_event_id);

do $$
begin
  if not exists (select 1 from pg_constraint c
                 join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
                 where c.conrelid = 'public.lead_action_requests'::regclass and c.contype = 'f'
                   and a.attname = 'lead_id') then
    alter table public.lead_action_requests add constraint lead_action_requests_lead_id_fkey
      foreign key (lead_id) references public.leads(id) on delete set null;
  end if;
  if not exists (select 1 from pg_constraint c
                 join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
                 where c.conrelid = 'public.lead_action_requests'::regclass and c.contype = 'f'
                   and a.attname = 'practice_id') then
    alter table public.lead_action_requests add constraint lead_action_requests_practice_id_fkey
      foreign key (practice_id) references public.practices(id) on delete restrict;
  end if;
  if not exists (select 1 from pg_constraint c
                 join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
                 where c.conrelid = 'public.integration_outbox'::regclass and c.contype = 'f'
                   and a.attname = 'outreach_event_id') then
    alter table public.integration_outbox add constraint integration_outbox_outreach_event_id_fkey
      foreign key (outreach_event_id) references public.outreach_events(id) on delete set null;
  end if;
end $$;
