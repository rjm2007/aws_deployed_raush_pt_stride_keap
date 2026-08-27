update public.practice_settings
set stride_location_timezone = 'America/Los_Angeles'
where stride_location_timezone is null or btrim(stride_location_timezone) = '';

alter table public.practice_settings
  alter column stride_location_timezone set default 'America/Los_Angeles',
  alter column stride_location_timezone set not null;

comment on column public.practice_settings.stride_location_timezone is
  'IANA timezone used for appointment confirmations; required to avoid ambiguous local times.';
