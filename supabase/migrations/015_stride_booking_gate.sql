alter table public.practice_settings
  add column if not exists stride_booking_enabled boolean not null default false;

comment on column public.practice_settings.stride_booking_enabled is
  'Enable only after location, clinician, appointment type, duration, and timezone are verified in Stride.';
