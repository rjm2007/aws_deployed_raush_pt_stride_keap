-- Voice-agent booking engine: which clinician can see which case at which
-- location, a local cache of their open Stride slots, and short holds so two
-- calls can never book the same slot.
--
-- Stride itself cannot answer "who treats knees at Laguna Niguel?" (users have
-- no location or speciality), so that mapping is ours, entered from the
-- client's list. Stride stays the source of truth for the calendar: the cache
-- only decides what to offer, and every booking re-checks the slot live first.

-- A clinic as leads name it ('Laguna Niguel') mapped to its Stride location.
create table if not exists public.booking_locations (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  name text not null check(length(trim(name)) > 0),
  stride_location_id bigint not null,
  timezone text not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists idx_booking_locations_name
  on public.booking_locations(practice_id, lower(name));

-- What the patient is coming in for ('Physical Therapy - Pelvic', 'Massage').
-- Ortho/pelvic is part of the name. One default catches leads whose case
-- matches no name.
create table if not exists public.case_types (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  name text not null check(length(trim(name)) > 0),
  stride_appointment_type_id bigint not null,
  duration_minutes integer not null check(duration_minutes between 15 and 240),
  is_default boolean not null default false,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists idx_case_types_name
  on public.case_types(practice_id, lower(name));
create unique index if not exists idx_case_types_one_default
  on public.case_types(practice_id) where is_default and is_active;

-- Clinicians we book for (Stride "users"). Kept separately from the
-- stride_users import mirror so booking works before that import is on.
create table if not exists public.clinicians (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_user_id bigint not null,
  display_name text not null check(length(trim(display_name)) > 0),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(practice_id, stride_user_id)
);

-- The client's list: this clinician sees this case type at this location.
create table if not exists public.clinician_assignments (
  id bigint generated always as identity primary key,
  clinician_id bigint not null references public.clinicians(id) on delete cascade,
  booking_location_id bigint not null references public.booking_locations(id) on delete cascade,
  case_type_id bigint not null references public.case_types(id) on delete cascade,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  unique(clinician_id, booking_location_id, case_type_id)
);
create index if not exists idx_clinician_assignments_lookup
  on public.clinician_assignments(booking_location_id, case_type_id) where is_active;

-- Open Stride slots, one row per clinician + location + visit length + start.
-- open: last sync saw it. gone: a later sync no longer saw it, or Stride
-- refused it. booked: we booked it.
create table if not exists public.availability_slots (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  clinician_id bigint not null references public.clinicians(id) on delete cascade,
  booking_location_id bigint not null references public.booking_locations(id) on delete cascade,
  duration_minutes integer not null,
  start_utc timestamptz not null,
  end_utc timestamptz not null,
  local_date date not null,
  local_time time not null,
  timezone text not null,
  status text not null default 'open' check(status in ('open','gone','booked')),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(clinician_id, booking_location_id, duration_minutes, start_utc)
);
create index if not exists idx_availability_slots_search
  on public.availability_slots(booking_location_id, duration_minutes, start_utc)
  where status = 'open';

-- When each location + visit length was last refreshed, so a search can tell
-- a fresh cache from a stale one.
create table if not exists public.availability_sync_state (
  booking_location_id bigint not null references public.booking_locations(id) on delete cascade,
  duration_minutes integer not null,
  window_start date,
  window_end date,
  last_success_at timestamptz,
  last_attempt_at timestamptz,
  last_error text,
  slot_count integer not null default 0,
  primary key(booking_location_id, duration_minutes)
);

-- The slots a call was offered, so the agent can book by saying the date and
-- time the patient chose instead of repeating an opaque id.
create table if not exists public.slot_offers (
  id bigint generated always as identity primary key,
  call_id text not null,
  lead_id uuid not null references public.leads(id) on delete cascade,
  slot_id bigint not null references public.availability_slots(id) on delete cascade,
  case_type_id bigint not null references public.case_types(id) on delete cascade,
  offered_at timestamptz not null default now()
);
create index if not exists idx_slot_offers_call on public.slot_offers(call_id, offered_at desc);

-- A slot reserved while it is being booked. The partial unique index is the
-- race guard: two calls inserting an active hold on one slot, one wins.
create table if not exists public.slot_holds (
  id bigint generated always as identity primary key,
  slot_id bigint not null references public.availability_slots(id) on delete cascade,
  lead_id uuid not null references public.leads(id) on delete cascade,
  call_id text,
  status text not null default 'active' check(status in ('active','booked','released','expired')),
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  released_at timestamptz
);
create unique index if not exists idx_slot_holds_one_active
  on public.slot_holds(slot_id) where status = 'active';
create index if not exists idx_slot_holds_expiry
  on public.slot_holds(expires_at) where status = 'active';

-- The appointment row is the booking record (state booking -> scheduled /
-- failed / unknown). These say which slot, hold and case it came from; the
-- Stride case id lets a retry reuse a case that has no visit on it yet
-- (Stride allows only one Initial Evaluation per case).
alter table public.appointments
  add column if not exists slot_id bigint references public.availability_slots(id) on delete set null,
  add column if not exists slot_hold_id bigint references public.slot_holds(id) on delete set null,
  add column if not exists case_type_id bigint references public.case_types(id) on delete set null,
  add column if not exists booking_location_id bigint references public.booking_locations(id) on delete set null,
  add column if not exists stride_case_id bigint,
  add column if not exists call_id text;

alter table public.booking_locations enable row level security;
alter table public.case_types enable row level security;
alter table public.clinicians enable row level security;
alter table public.clinician_assignments enable row level security;
alter table public.availability_slots enable row level security;
alter table public.availability_sync_state enable row level security;
alter table public.slot_offers enable row level security;
alter table public.slot_holds enable row level security;
