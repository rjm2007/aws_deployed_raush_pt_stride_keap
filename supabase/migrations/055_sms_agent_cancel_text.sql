-- SMS agent (Yajat's test, 2026-11-05): a patient who cancels or changes a booked visit by text gets
-- its own fixed text (hand_off reason cancel_visit), editable in the Studio like the other
-- sms_agent.* texts (054). The general front desk text never said the visit is still booked.
-- Same draft as services/sms_agent_texts.py DRAFTS["cancel_visit"]. Safe to re-run.
insert into public.message_templates(practice_id, key, channel, body)
select p.id, t.key, 'sms', t.body
from public.practices p
cross join (values
  ('sms_agent.cancel_visit',
   'Got it, I''ve passed that to our front desk. Your visit stays booked until they confirm the change with you. You can also call them at {front_desk_phone}.')
) as t(key, body)
where p.slug = 'rausch-pt'
  and not exists (
    select 1 from public.message_templates mt
    where mt.practice_id = p.id and mt.key = t.key and mt.cadence_step_id is null
  );
