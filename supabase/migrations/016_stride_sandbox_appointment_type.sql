-- Stride sandbox appointment type IDs (confirmed by Stride, 2026-08):
--   Follow-up 1451, Initial Evaluation 1452, Progress Note 1453, Reevaluation 1454,
--   Recertification 1455, Consultation 1456, Event 1457, Meeting 1458, PTO 1459, Lunch 1460.
-- The voice agent only ever books the first visit, so booking uses Initial Evaluation (1452).

alter table public.practice_settings
  alter column stride_appointment_type_id set default 1452;

update public.practice_settings ps
set stride_appointment_type_id = 1452, updated_at = now()
from public.practices p
where p.id = ps.practice_id
  and p.slug = 'rausch-pt'
  and ps.stride_appointment_type_id = 8;

comment on column public.practice_settings.stride_appointment_type_id is
  'Stride appointment type id sent as appointment_type on POST /v1/appointments/. Sandbox: 1452 = Initial Evaluation.';
