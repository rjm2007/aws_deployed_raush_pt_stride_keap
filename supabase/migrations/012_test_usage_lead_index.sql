create index if not exists idx_test_usage_ledger_lead
  on public.test_usage_ledger(lead_id)
  where lead_id is not null;

comment on index public.idx_test_usage_ledger_lead is
  'Supports lead-linked usage auditing and efficient ON DELETE SET NULL maintenance.';
