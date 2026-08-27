-- Constraints required by deterministic seed upserts and provider idempotency.
-- Empty duplicate checks are implicit in unique-index creation; on a populated
-- database this fails transactionally rather than deleting an arbitrary row.

create unique index if not exists idx_cadence_steps_practice_key
  on public.cadence_steps(practice_id,key);
create unique index if not exists idx_message_templates_practice_key
  on public.message_templates(practice_id,key);
create unique index if not exists idx_appointments_booking_key
  on public.appointments(booking_key)
  where booking_key is not null;
create unique index if not exists idx_appointments_stride_id
  on public.appointments(stride_appointment_id)
  where stride_appointment_id is not null;
