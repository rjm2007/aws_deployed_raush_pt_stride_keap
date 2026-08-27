-- DEVELOPMENT/TEST ONLY.
-- Parameters are supplied by rpt test-lead through psycopg; do not interpolate strings.
-- This intentionally cannot match real/imported leads: both is_test and source_system are required.
with target_leads as materialized (
  select l.id
  from public.leads l
  join public.practices p on p.id = l.practice_id
  where p.slug = %(practice_slug)s
    and l.is_test is true
    and l.source_system = 'synthetic_test'
    and lower(btrim(coalesce(l.first_name, ''))) = lower(btrim(%(first_name)s))
    and lower(btrim(coalesce(l.last_name, ''))) = lower(btrim(%(last_name)s))
  for update of l
),
target_appointments as materialized (
  select a.id
  from public.appointments a
  join target_leads t on t.id = a.lead_id
),
deleted_provider_events as (
  delete from public.provider_events pe
  using target_leads t
  where pe.provider = 'vapi'
    and t.id::text in (
      coalesce(pe.payload #>> '{message,call,assistantOverrides,variableValues,lead_id}', ''),
      coalesce(pe.payload #>> '{message,call,variableValues,lead_id}', ''),
      coalesce(pe.payload #>> '{message,variableValues,lead_id}', ''),
      coalesce(pe.payload #>> '{variableValues,lead_id}', ''),
      coalesce(pe.payload #>> '{variables,lead_id}', '')
    )
  returning pe.id
),
deleted_outbox as (
  delete from public.integration_outbox io
  using target_appointments a
  where io.aggregate_id = a.id::text
  returning io.id
),
deleted_notifications as (
  delete from public.notification_log n
  using target_leads t
  where n.lead_id = t.id
  returning n.id
),
deleted_sms as (
  delete from public.sms_messages sm
  using target_leads t
  where sm.lead_id = t.id
  returning sm.id
),
deleted_appointments as (
  delete from public.appointments a
  using target_leads t
  where a.lead_id = t.id
  returning a.id
),
deleted_leads as (
  delete from public.leads l
  using target_leads t
  where l.id = t.id
  returning l.id
)
select
  (select count(*) from deleted_leads) as deleted_leads,
  (select count(*) from deleted_appointments) as deleted_appointments,
  (select count(*) from deleted_notifications) as deleted_notifications,
  (select count(*) from deleted_sms) as deleted_sms,
  (select count(*) from deleted_outbox) as deleted_outbox_events,
  (select count(*) from deleted_provider_events) as deleted_provider_events;
