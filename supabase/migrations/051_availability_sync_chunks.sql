-- Demo booking engine: the slot cache now holds 90 days (BOOKING_CACHE_DAYS), fetched from Stride in
-- chunks of at most 31 days (Stride's limit per availability request). Chunk 0 (the first 31 days)
-- refreshes every BOOKING_SYNC_SECONDS, later chunks every BOOKING_FAR_SYNC_SECONDS, so each chunk
-- keeps its own refresh time: one row per location + visit length + chunk (owner, 2026-10-23).
-- Existing rows become chunk 0, which is what they described. Re-runnable.
alter table public.availability_sync_state
  add column if not exists chunk smallint not null default 0;

-- The primary key is looked up by catalog, not by name, in case a hand-made table named it otherwise.
do $$
declare
  pk text;
begin
  if not exists (
    select 1 from pg_index i
    join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
    where i.indrelid = 'public.availability_sync_state'::regclass and i.indisprimary
      and a.attname = 'chunk'
  ) then
    select conname into pk from pg_constraint
     where conrelid = 'public.availability_sync_state'::regclass and contype = 'p';
    if pk is not null then
      execute format('alter table public.availability_sync_state drop constraint %I', pk);
    end if;
    alter table public.availability_sync_state
      add primary key (booking_location_id, duration_minutes, chunk);
  end if;
end $$;
