-- Record the authenticated CRM operator's consent attestation without exposing
-- consent-method and reference fields in the Add Lead form.

alter table public.leads
  drop constraint if exists leads_consent_source_check,
  add constraint leads_consent_source_check check (
    consent_source is null or consent_source in (
      'web_form',
      'intake_paper',
      'verbal_recorded',
      'referral_partner',
      'imported',
      'dashboard_staff_attestation'
    )
  );
