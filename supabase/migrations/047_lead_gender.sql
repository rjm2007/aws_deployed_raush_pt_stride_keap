-- Gender from the Google Sheet's "Gender" column (M / F / Other, owner 2026-10-20). Used for the Keap
-- pelvic Booked tag: the Sheet's value wins; without it, the Stride patient's gender is used.
-- Null = not given. Stored as M, F or O.
alter table public.leads add column if not exists gender text;
do $$ begin
  alter table public.leads add constraint leads_gender_check check (gender in ('M', 'F', 'O'));
exception when duplicate_object then null;
end $$;
