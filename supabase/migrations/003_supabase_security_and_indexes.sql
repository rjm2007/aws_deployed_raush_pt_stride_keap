-- Resolve actionable Supabase advisor findings without removing empty-project
-- indexes merely because they have not accumulated usage statistics yet.

alter function public.set_updated_at() set search_path = '';
do $$ begin
  if to_regprocedure('public.rls_auto_enable()') is not null then
    revoke execute on function public.rls_auto_enable() from public, anon, authenticated;
  end if;
end $$;

alter view if exists public.v_dashboard_stats set (security_invoker = true);
alter view if exists public.v_lead_cadence_progress set (security_invoker = true);
alter view if exists public.v_lead_contacts_today set (security_invoker = true);

create index if not exists idx_appointments_outreach_event
  on public.appointments(outreach_event_id);
create index if not exists idx_message_templates_practice
  on public.message_templates(practice_id);
create index if not exists idx_outreach_cadence_step
  on public.outreach_events(cadence_step_id);
do $$ begin
  if to_regclass('public.stride_busy_blocks') is not null then
    execute 'create index if not exists idx_stride_busy_appointment '
      'on public.stride_busy_blocks(appointment_id)';
  end if;
  if exists (
    select 1 from information_schema.columns where table_schema='public'
    and table_name='appointments' and column_name='rescheduled_from_id'
  ) then
    execute 'create index if not exists idx_appointments_rescheduled_from '
      'on public.appointments(rescheduled_from_id)';
  end if;
end $$;
