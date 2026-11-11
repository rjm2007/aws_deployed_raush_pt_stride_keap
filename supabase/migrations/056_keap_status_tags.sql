-- Keap status tags from the clinic's Keap admin (Michelle, reply of 2026-11-10):
--   sent to the front desk  -> "STATUS: Handed off to Human"
--   Do Not Contact          -> "STATUS: Abandoned Lead"   (was: kept out of Keap)
--   wrong number            -> "STATUS: Bad Phone"        (was: kept out of Keap)
--   cancel                  -> "STATUS: Cancelled"        (was: Pending back)
--   no-show                 -> "STATUS: NoShow"           (was: Booked kept)
-- A null id keeps the old rule for that case. Ids differ per Keap account (fill from
-- `rpt keap-discover`).
--
-- "You don't need to remove tags. Keap is already set up to remove tags when the following tag
-- is applied." keap_remove_old_tags=false: we only apply a tag when it is new for that person
-- (not in what we last sent), never remove one, and never re-apply a tag Keap's automations took
-- off (that would fire the automation again).
alter table public.practice_settings
  add column if not exists keap_handed_off_tag_id text,
  add column if not exists keap_abandoned_tag_id text,
  add column if not exists keap_bad_phone_tag_id text,
  add column if not exists keap_cancelled_tag_id text,
  add column if not exists keap_no_show_tag_id text,
  add column if not exists keap_remove_old_tags boolean not null default false;
