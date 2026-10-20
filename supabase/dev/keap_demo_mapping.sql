-- DEMO ONLY (Supabase itdlqobtyxshlopkhzfw): Keap sandbox tags for the demo's Stride visit types, so a
-- booking on the demo always shows a tag in Keap (owner, 2026-10-20). The demo Stride and Mock Stride only
-- have these three types, so each stands in for one real kind of visit until Rausch's real types are known:
--   1452 Initial Evaluation  (what the voice agent and Mock Stride book by default) -> PT
--   1451 Follow-up           (pick it in Mock Stride)                                 -> PT Premium
--   1453 Progress Note       (pick it in Mock Stride)                                 -> Pelvic, Female/Male by gender
-- Never run this on production. Needs migration 046 (male_booked_tag_id).
insert into public.keap_appointment_types
  (practice_id, stride_appointment_type_id, label, booked_tag_id, male_booked_tag_id,
   completed_tag_id, date_field_id, time_field_id, clinician_field_id)
select p.id, v.type_id, v.label, v.booked, v.male, v.done, '6', '8', '10'
from public.practices p,
  (values (1452, 'PT (demo: Initial Evaluation)', '184', null,  '176'),  -- PT Appt Booked / Completed
          (1451, 'PT Premium (demo: Follow-up)',  '166', null,  '164'),  -- PT Premium Booked / Completed
          (1453, 'Pelvic (demo: Progress Note)',  '162', '160', '158'))  -- Pelvic Booked F / M; Completed
       as v(type_id, label, booked, male, done)
where p.slug = 'rausch-pt'
on conflict (practice_id, stride_appointment_type_id) do update set
  label = excluded.label, booked_tag_id = excluded.booked_tag_id,
  male_booked_tag_id = excluded.male_booked_tag_id, completed_tag_id = excluded.completed_tag_id,
  date_field_id = excluded.date_field_id, time_field_id = excluded.time_field_id,
  clinician_field_id = excluded.clinician_field_id, is_active = true, updated_at = now();
