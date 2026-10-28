-- The post-call structured output Vapi sends with every answered call (artifact.structuredOutputs),
-- kept on the call log so staff and debugging can see why a cut call moved a lead (2026-10-29).
-- The review of 2026-10-28 found it was never stored. "if not exists": a database that already has
-- the column keeps it as it is.
alter table public.call_logs add column if not exists structured_output jsonb;
