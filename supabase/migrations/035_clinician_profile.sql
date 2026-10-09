-- What the voice agent may say about a clinician when a caller asks: credentials
-- ("PT, DPT") and one short plain sentence (focus areas). Entered from the client's
-- list; the agent answers only from these fields and never invents anything else.
alter table public.clinicians
  add column if not exists credentials text,
  add column if not exists bio text check (bio is null or length(bio) <= 300);
