-- The Stride patient an appointment was booked for (demo booking engine; production has none yet).
-- The live demo DB already has this column (added by hand); the agent never filled it, so an
-- appointment row showed its Stride appointment and case but not the patient (row 258, 2026-10-22).
-- Re-runnable; on the live demo DB it changes nothing.
alter table public.appointments add column if not exists stride_patient_id bigint;
