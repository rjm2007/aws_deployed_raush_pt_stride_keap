-- Stride sends CSV exports over SFTP: one full load, then only changed rows.
-- Every row carries Action = Upsert | Delete. These tables mirror what Stride
-- sent so later work (Keap, lead matching) reads one local copy.
--
-- Conventions shared by every stride_* table:
--   * Keyed by (practice_id, stride_id). Stride ids are only unique per practice.
--   * raw keeps the full CSV row, so a field we ignore today can be used later
--     without a schema change or a re-import.
--   * stride_modified_at is Stride's "Modified Date Time"; an older row never
--     overwrites a newer one (files can arrive out of order).
--   * A Delete never removes the row: deleted_at is set (a tombstone), so a late,
--     older Upsert cannot bring it back.
--   * updated_at changes only when the content really changed; it is the
--     "what changed since" cursor for later sync work.
--   * No foreign keys between stride_* tables: an appointment may arrive before
--     its patient, and rejecting it would lose data.

create table if not exists public.stride_import_files (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  file_name text not null,
  entity text not null,
  exported_at timestamptz,
  sha256 text not null,
  size_bytes bigint not null,
  -- pending: an attempt started (recorded before work, so a crash still counts);
  -- retrying: failed for a temporary reason, still in the inbox;
  -- rejected: moved to the error folder, needs a person.
  status text not null default 'pending'
    check(status in ('pending','processed','retrying','rejected')),
  attempts integer not null default 0,
  started_at timestamptz,
  row_count integer not null default 0,
  applied_count integer not null default 0,
  unchanged_count integer not null default 0,
  issue_count integer not null default 0,
  -- PHI-free: row number, Stride id and a short reason only.
  issues jsonb not null default '[]'::jsonb,
  error text,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  unique(file_name, sha256)
);

create table if not exists public.stride_patients (
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_id bigint not null,
  external_id text,
  first_name text,
  last_name text,
  date_of_birth date,
  gender text,
  personal_email text,
  work_email text,
  preferred_email text,
  -- Phones normalized to E.164; the original text stays in raw.
  mobile_phone text,
  home_phone text,
  work_phone text,
  preferred_phone text,
  address_1 text,
  address_2 text,
  city text,
  state text,
  zip text,
  marketing_opt_in boolean,
  referral_source text,
  first_appointment_at timestamptz,
  latest_appointment_at timestamptz,
  lead_id uuid references public.leads(id) on delete set null,
  lead_match_status text not null default 'unmatched'
    check(lead_match_status in ('unmatched','matched','ambiguous')),
  lead_matched_at timestamptz,
  stride_created_at timestamptz,
  stride_modified_at timestamptz,
  deleted_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  import_file_id bigint references public.stride_import_files(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(practice_id, stride_id)
);

create table if not exists public.stride_cases (
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_id bigint not null,
  patient_stride_id bigint,
  title text,
  status text,
  referring_provider_id bigint,
  referring_provider_name text,
  last_appointment_at timestamptz,
  stride_created_at timestamptz,
  stride_modified_at timestamptz,
  deleted_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  import_file_id bigint references public.stride_import_files(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(practice_id, stride_id)
);

create table if not exists public.stride_appointments (
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_id bigint not null,
  patient_stride_id bigint,
  case_stride_id bigint,
  appointment_type_id bigint,
  appointment_type text,
  category text,
  status text,
  -- Stride's Date/Start/End are clinic-local wall times; *_utc are derived with
  -- the practice timezone.
  starts_at_local timestamp,
  ends_at_local timestamp,
  start_utc timestamptz,
  end_utc timestamptz,
  location_id bigint,
  location_name text,
  clinician_id bigint,
  clinician_name text,
  place_of_service text,
  payment_type text,
  stride_created_at timestamptz,
  stride_modified_at timestamptz,
  deleted_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  import_file_id bigint references public.stride_import_files(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(practice_id, stride_id)
);

create table if not exists public.stride_users (
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_id bigint not null,
  first_name text,
  last_name text,
  email text,
  credentials text,
  npi text,
  is_active boolean,
  stride_created_at timestamptz,
  stride_modified_at timestamptz,
  deleted_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  import_file_id bigint references public.stride_import_files(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(practice_id, stride_id)
);

create table if not exists public.stride_locations (
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_id bigint not null,
  name text,
  address_1 text,
  address_2 text,
  city text,
  state text,
  zip text,
  phone text,
  is_active boolean,
  stride_created_at timestamptz,
  stride_modified_at timestamptz,
  deleted_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  import_file_id bigint references public.stride_import_files(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(practice_id, stride_id)
);

-- Notes, patient insurances, payers and referring providers: kept whole, not
-- used yet. Promote one to its own table if a feature needs its fields.
create table if not exists public.stride_other_records (
  practice_id bigint not null references public.practices(id) on delete restrict,
  entity text not null check(entity in ('notes','patient_insurances','payers','providers')),
  stride_id bigint not null,
  patient_stride_id bigint,
  stride_created_at timestamptz,
  stride_modified_at timestamptz,
  deleted_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  import_file_id bigint references public.stride_import_files(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key(practice_id, entity, stride_id)
);

-- Each background service reports "I am alive" plus what only it can see (for
-- stride-worker: files waiting in the SFTP inbox, disk space). The dashboard
-- turns stale or bad values into alerts.
create table if not exists public.service_heartbeats (
  service text primary key,
  last_seen_at timestamptz not null default now(),
  details jsonb not null default '{}'::jsonb
);

create index if not exists idx_stride_import_files_recent
  on public.stride_import_files(practice_id, received_at desc);
create index if not exists idx_stride_patients_match
  on public.stride_patients(practice_id, lower(last_name), date_of_birth)
  where deleted_at is null;
create index if not exists idx_stride_patients_unmatched
  on public.stride_patients(practice_id) where lead_id is null and deleted_at is null;
create index if not exists idx_stride_patients_lead
  on public.stride_patients(lead_id) where lead_id is not null;
create index if not exists idx_stride_patients_updated
  on public.stride_patients(practice_id, updated_at);
create index if not exists idx_stride_cases_patient
  on public.stride_cases(practice_id, patient_stride_id);
-- Delete rows carry no Appointment Id; Stride allows one appointment per case
-- per start time, so this is how a Delete finds its row.
create index if not exists idx_stride_appointments_case_start
  on public.stride_appointments(practice_id, case_stride_id, starts_at_local);
create index if not exists idx_stride_appointments_patient
  on public.stride_appointments(practice_id, patient_stride_id, start_utc);
create index if not exists idx_stride_appointments_updated
  on public.stride_appointments(practice_id, updated_at);
create index if not exists idx_stride_other_patient
  on public.stride_other_records(practice_id, patient_stride_id);
create index if not exists idx_leads_match_name
  on public.leads(practice_id, lower(last_name), date_of_birth)
  where stride_patient_id is null;

alter table public.stride_import_files enable row level security;
alter table public.stride_patients enable row level security;
alter table public.stride_cases enable row level security;
alter table public.stride_appointments enable row level security;
alter table public.stride_users enable row level security;
alter table public.stride_locations enable row level security;
alter table public.stride_other_records enable row level security;
alter table public.service_heartbeats enable row level security;
