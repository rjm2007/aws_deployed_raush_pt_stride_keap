-- Soft-delete cadence versions while preserving their immutable history.

alter table public.cadence_versions
  drop constraint if exists cadence_versions_status_check;
alter table public.cadence_versions
  add constraint cadence_versions_status_check
  check(status in ('draft','active','archived','deleted')),
  add column deleted_at timestamptz;

update public.cadence_versions
set name='Personalized plan v' || version_number
where lead_id is not null and name like 'Local override v%';

create index idx_cadence_versions_deleted
  on public.cadence_versions(practice_id,deleted_at desc) where status='deleted';
create index idx_cadence_versions_source
  on public.cadence_versions(source_version_id) where source_version_id is not null;

comment on column public.cadence_versions.deleted_at is
  'Soft-deletion time. Steps and templates remain available for audit history.';
