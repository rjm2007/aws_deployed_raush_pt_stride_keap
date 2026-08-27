-- Explicit refusal semantics: declining this outreach is not the same as
-- opting out of calls or asking for global suppression.
alter table public.leads drop constraint if exists leads_last_call_outcome_check;
alter table public.leads add constraint leads_last_call_outcome_check
  check(last_call_outcome is null or last_call_outcome in (
    'booked','not_interested','no_answer','voicemail','callback','transferred','manual',
    'call_opt_out','do_not_contact'
  ));

alter table public.outreach_events drop constraint if exists outreach_events_outcome_check;
alter table public.outreach_events add constraint outreach_events_outcome_check
  check(outcome is null or outcome in (
    'booked','not_interested','no_answer','voicemail','callback','transferred','manual',
    'call_opt_out','do_not_contact'
  ));
