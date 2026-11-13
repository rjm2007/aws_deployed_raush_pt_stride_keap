-- SMS agent (Emma's test, 2026-11-11), Studio texts like the other sms_agent.* texts (054, 055):
-- - booking_problem: a failed booking by text sent the front desk text and Elliot's own "I couldn't
--   finish booking that by text..." at the same time; now one text says both.
-- - reply_limit: at the daily reply limit the patient got no answer at all; now one closing text.
-- Same drafts as services/sms_agent_texts.py DRAFTS. Safe to re-run.
insert into public.message_templates(practice_id, key, channel, body)
select p.id, t.key, 'sms', t.body
from public.practices p
cross join (values
  ('sms_agent.booking_problem',
   'I couldn''t finish booking that by text, so our front desk will finish setting up your visit. You can also call them at {front_desk_phone}.'),
  ('sms_agent.reply_limit',
   'I''ve passed your messages to our front desk. They''ll reach out to you, or you can call them at {front_desk_phone}.')
) as t(key, body)
where p.slug = 'rausch-pt'
  and not exists (
    select 1 from public.message_templates mt
    where mt.practice_id = p.id and mt.key = t.key and mt.cadence_step_id is null
  );
