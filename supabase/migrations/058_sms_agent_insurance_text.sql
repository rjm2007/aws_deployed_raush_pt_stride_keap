-- SMS agent (Emma's retest, 2026-11-13): an insurance question gets its own front desk text that names
-- insurance (hand_off reason insurance). The general front desk text never said so, so the question
-- looked unanswered. Same draft as services/sms_agent_texts.py DRAFTS["insurance"]. Safe to re-run.
insert into public.message_templates(practice_id, key, channel, body)
select p.id, t.key, 'sms', t.body
from public.practices p
cross join (values
  ('sms_agent.insurance',
   'Our front desk can answer your insurance question. You can call them at {front_desk_phone}, or they''ll reach out to you as soon as they''re available.')
) as t(key, body)
where p.slug = 'rausch-pt'
  and not exists (
    select 1 from public.message_templates mt
    where mt.practice_id = p.id and mt.key = t.key and mt.cadence_step_id is null
  );
