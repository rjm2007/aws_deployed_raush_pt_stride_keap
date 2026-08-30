-- Persistent staff-created lead intake and dashboard classification fields.

alter table public.leads
  add column if not exists lead_type text,
  add column if not exists referred_by text,
  add column if not exists location text,
  add column if not exists owner text;

alter table public.leads
  drop constraint if exists leads_lead_type_check,
  add constraint leads_lead_type_check check (
    lead_type is null or lead_type in ('Physical Therapy','Wellness')
  ),
  drop constraint if exists leads_referred_by_length_check,
  add constraint leads_referred_by_length_check check (
    referred_by is null or char_length(referred_by) <= 200
  ),
  drop constraint if exists leads_location_length_check,
  add constraint leads_location_length_check check (
    location is null or char_length(location) <= 120
  ),
  drop constraint if exists leads_owner_length_check,
  add constraint leads_owner_length_check check (
    owner is null or char_length(owner) <= 200
  );
