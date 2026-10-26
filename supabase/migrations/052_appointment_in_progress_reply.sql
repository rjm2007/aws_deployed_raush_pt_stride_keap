-- When the voice agent was told IN_PROGRESS about this booking attempt (demo booking engine;
-- production has none yet). After an IN_PROGRESS reply the agent may already have told the patient
-- "our front desk team will take care of your appointment", so if this attempt then fails, even
-- with "time taken", the lead goes to Needs Review (owner, 2026-10-26). Cleared when a failed
-- attempt is reused. Re-runnable.
alter table public.appointments add column if not exists in_progress_reply_at timestamptz;
