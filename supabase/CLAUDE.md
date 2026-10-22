# supabase — database schema

The app talks to hosted Supabase Postgres directly through `SUPABASE_DB_URL` (psycopg, no ORM, no Supabase
client).

| Path | Purpose |
| --- | --- |
| `migrations/NNN_name.sql` | Ordered, idempotent schema changes (001 … 048). 048 (`lead_reviews`) is byte-identical to production's; demo-only review codes come from Python. `rpt migrate` applies any file not yet recorded in `public.schema_migrations`, in filename order. |
| `seed.sql` | Fresh-sandbox seed (`rpt seed`): the `rausch-pt` practice, settings, default cadence. Leaves `stride_booking_enabled=false`. |
| `dev/reset_test_lead_by_name.sql` | Dev/test only. Used by `rpt test-lead` to reset synthetic leads; can only match `is_test` dashboard leads. Remove before a real production release (see `docs/FUTURE_DEPLOYMENT.md`). |

## Main tables

- `practices`, `practice_settings` — one practice (`slug='rausch-pt'`); business hours, caps, Vapi assistant
  and phone number IDs (override `.env`), Stride location/case settings, booking gate.
- `leads`, `lead_status_history` — lead state and every status transition (with `source`).
- `cadence_versions`, `cadence_steps`, `message_templates` — global cadence definitions; `outreach_events` —
  one row per scheduled call/SMS for a lead.
- `call_logs`, `call_transcripts`, `sms_messages`, `notification_log` — provider results.
- `appointments` — Stride bookings. `suppressed_numbers` — per-phone do-not-contact list.
- `provider_events`, `integration_events`, `integration_outbox` — durable webhook intake and outbound work
  (Sheet snapshots, Keap handoff, confirmation SMS).
- `lead_action_requests` — idempotency for Sheet commands. `sheet_row_state` — profile fingerprint per linked Sheet lead (direct Sheet sync). Trigger `leads_sheet_stage_change` (045) queues a Sheet update whenever a Sheet lead's status / cadence_state / needs_review changes. `dashboard_audit_log`,
  `dashboard_sms_requests` — staff actions.
- `test_usage_ledger` — provider cost for test runs (kept on lead delete for billing).
- `stride_import_files`, `stride_patients`, `stride_cases`, `stride_appointments`, `stride_users`,
  `stride_locations`, `stride_other_records` — mirror of Stride's SFTP CSV exports (migration 032); full row in
  `raw`, `deleted_at` tombstones, `updated_at` moves only on real change. `stride_patients.lead_id` links a lead.
- `booking_locations`, `case_types`, `clinicians`, `clinician_assignments`, `availability_slots`,
  `availability_sync_state`, `slot_offers`, `slot_holds` — voice-agent booking engine (migration 033,
  `docs/BOOKING_ENGINE.md`). Demo data: `dev/booking_demo_seed.sql` via `rpt booking-demo-seed`.
- `lead_message_overrides` and lead-scoped `cadence_versions` — legacy personalization, no longer written.

## Rules

- Never edit a migration that has been applied; add the next number. Two files share 016 and 023 for
  historical reasons; ordering is by full filename.
- Write migrations to be re-runnable (`if not exists`, `drop constraint if exists`, guarded updates).
- Some invariants are enforced by triggers/constraints (for example migration 025's worker claim protocol and
  028–030 review-pause fallback, 048 review rows and close-on-clear); read the migration before changing related Python.
- `lead_reviews` (048) — one row per review flag (code, message, details, source) and its resolution
  (who, choice, note). `leads.sms_disabled` = calls only; `leads.sheet_name_accepted` = Sheet name staff kept;
  `sheet_row_state.sheet_name` = the Name the Sheet worker last read (fills pre-048 name reviews).
- The live demo DB stores `lead_action_requests.request_id` / `lead_id` / `practice_id` as text (migrations: uuid /
  uuid / bigint). Compare them as text in SQL; `tests/test_live_column_types_integration.py` covers it.
