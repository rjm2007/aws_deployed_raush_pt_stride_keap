-- DEMO ONLY. Booking setup for the Stride demo practice ("perform_demo",
-- Reference/Reference_sheet_old): Stride locations 3169 Queens PT and 3170
-- Fulton PT, clinicians 5980-5982, appointment type 1452 Initial Evaluation.
-- The real clinic list (who sees which case at which location) comes from the
-- client and replaces this. Re-runnable: updates in place, never duplicates.

with practice as (select id from public.practices where slug = 'rausch-pt')
insert into public.booking_locations(practice_id, name, stride_location_id, timezone)
select practice.id, v.name, v.stride_location_id, 'America/New_York'
from practice, (values
  ('Laguna Niguel', 3170),
  ('Dana Point', 3170),
  ('Mission Viejo', 3169)
) as v(name, stride_location_id)
on conflict(practice_id, lower(name)) do update
  set stride_location_id = excluded.stride_location_id, timezone = excluded.timezone,
      is_active = true, updated_at = now();

with practice as (select id from public.practices where slug = 'rausch-pt')
insert into public.case_types(practice_id, name, stride_appointment_type_id, duration_minutes, is_default)
select practice.id, v.name, 1452, 60, v.is_default
from practice, (values
  ('Physical Therapy', true),
  ('Physical Therapy - Pelvic', false)
) as v(name, is_default)
on conflict(practice_id, lower(name)) do update
  set stride_appointment_type_id = excluded.stride_appointment_type_id,
      duration_minutes = excluded.duration_minutes, is_default = excluded.is_default,
      is_active = true, updated_at = now();

with practice as (select id from public.practices where slug = 'rausch-pt')
insert into public.clinicians(practice_id, stride_user_id, display_name)
select practice.id, v.stride_user_id, v.display_name
from practice, (values
  (5980, 'Joe Smith'),
  (5981, 'Thao Nguyen'),
  (5982, 'Alan Rome')
) as v(stride_user_id, display_name)
on conflict(practice_id, stride_user_id) do update
  set display_name = excluded.display_name, is_active = true, updated_at = now();

insert into public.clinician_assignments(clinician_id, booking_location_id, case_type_id)
select c.id, bl.id, ct.id
from (values
  (5980, 'Laguna Niguel', 'Physical Therapy'),
  (5981, 'Laguna Niguel', 'Physical Therapy'),
  (5982, 'Laguna Niguel', 'Physical Therapy'),
  (5981, 'Dana Point', 'Physical Therapy'),
  (5982, 'Dana Point', 'Physical Therapy'),
  (5980, 'Mission Viejo', 'Physical Therapy'),
  (5982, 'Laguna Niguel', 'Physical Therapy - Pelvic'),
  (5982, 'Dana Point', 'Physical Therapy - Pelvic')
) as v(stride_user_id, location_name, case_type_name)
join public.practices p on p.slug = 'rausch-pt'
join public.clinicians c on c.practice_id = p.id and c.stride_user_id = v.stride_user_id
join public.booking_locations bl on bl.practice_id = p.id and lower(bl.name) = lower(v.location_name)
join public.case_types ct on ct.practice_id = p.id and lower(ct.name) = lower(v.case_type_name)
on conflict(clinician_id, booking_location_id, case_type_id) do update set is_active = true;
