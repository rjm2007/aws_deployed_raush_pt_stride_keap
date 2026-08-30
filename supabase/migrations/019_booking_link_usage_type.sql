-- delivery.py records a booking-link SMS as usage_type 'booking_link_sms', but the
-- ledger only permitted 'call', 'cadence_sms' and 'booking_confirmation_sms'. The
-- insert therefore raised a CheckViolation *after* Twilio had already accepted the
-- message, rolling back the row that marks the notification as sent and leaving the
-- lead flagged for review with no booking link recorded.

alter table public.test_usage_ledger
  drop constraint if exists test_usage_ledger_usage_type_check,
  add constraint test_usage_ledger_usage_type_check check (
    usage_type in ('call','cadence_sms','booking_confirmation_sms','booking_link_sms')
  );

comment on column public.test_usage_ledger.usage_type is
  'call, cadence_sms, booking_confirmation_sms, or booking_link_sms.';
