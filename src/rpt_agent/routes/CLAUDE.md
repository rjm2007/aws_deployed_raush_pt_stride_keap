# routes — FastAPI routers

Thin HTTP layer: authenticate, validate with pydantic, call a service, shape the response. Business logic
belongs in `services/`. Every router is exported from `__init__.py` and registered in `api.py`.

| File | Prefix | Caller | Endpoints |
| --- | --- | --- | --- |
| `health.py` | — | load balancer / ops | `GET /health` (liveness), `GET /ready` (config + DB + Stride gate) |
| `availability.py` | `/api/v1/tools` | Vapi assistant | `POST /check-availability` — live Stride slots (read-only) |
| `appointments.py` | `/api/v1/tools` | Vapi assistant | `POST /create-appointment` — real Stride booking, idempotent (old single-clinic flow) |
| `booking_tools.py` | `/api/v1/tools` | Vapi assistant | `POST /find-slots`, `POST /book-appointment` — booking engine (see `docs/BOOKING_ENGINE.md`) |
| `leads.py` | `/api/v1/webhooks/vapi` | Vapi assistant | `POST /lead-status` — record a call outcome |
| `vapi.py` | `/api/v1/vapi` | Vapi | `POST /tools` (compat tool endpoint), `POST /webhook` (end-of-call report, stored durably then processed) |
| `twilio.py` | `/api/v1/twilio` | Twilio | `POST /inbound-sms` (replies, STOP), `POST /message-status` (delivery callbacks); signature-checked |
| `n8n.py` | `/api/v1/integrations/n8n` | n8n (Google Sheet) | `POST /lead-actions` (start / restart / DNC / booked), `POST /lead-sync` (profile fields); HMAC + `X-Request-ID` |
| `dashboard.py` | `/api/v1/dashboard` | Next.js proxy | snapshot, paged Leads page (`GET /lead-list`, `GET /lead-board`, `GET /stride-patients/{id}`, via `services/lead_list.py`), lead CRUD, cadence pause/resume, contact rules, board stage moves, review preview + resolve (`GET /review/{id}`, `POST /review/{id}/resolve` with a choice), outreach reschedule, cadence versions/steps, message templates, manual SMS |
| `dashboard_calendar.py` | `/api/v1/dashboard` | Next.js proxy | `GET /appointment-calendar` (every booking, ours and Stride's, via `services/appointment_views.py`, plus `open_slots` per clinician, clinic and day from the availability cache, never lead-filtered, and `practitioners` with `role`; days, slot clock times and `timezone` are the clinic's (`practices.timezone`, fallback California), never `stride_location_timezone`; also feeds the Analytics appointment counts), `GET /appointment-approvals` (our visits waiting for approval in Stride, upcoming first), `GET /appointment-slots`, `POST /appointments` (staff booking) |
| `tool_request.py` | — | helper | shared Vapi tool-request authentication |

The staff assistant router (`/api/v1/dashboard/assistant`) lives in `../agent/router.py`.

## dashboard.py essentials (~1800 lines)

- `POST /leads` (Add lead) is off unless `DASHBOARD_LEAD_CREATE_ENABLED=true`: leads come only from the Google Sheet (client, 2026-10-15).
- `_stage(row)` (from `services/lead_stage.py`) decides the board column (new / cadence / attention / booked / closed). Order of checks
  matters: booked → attention → closed statuses → new/pending → paused stays `cadence` → spent cadence = closed.
- `_lead(row)` shapes a lead for the board (`_LEAD_SELECT` / `_lead_rows`, shared with the paged list and board); `GET /snapshot` returns the newest 250 leads plus every lead in review, real stage `counts` and `counts_by_location` (over every lead, `STAGE_SQL`), `owners` (every owner on any lead) and `created_by_day` (clinic day x clinic, last 15 days), + metrics; `GET /lead-list` (source all/sheet/stride, stage, owner, location, q, offset ≤ 10M, limit ≤ 500: rows + total + per-source counts + `overall`) and `GET /lead-board` (per column 25 cards + real totals; `stage` + `offset` + `limit` ≤ 500 for more or a one-column refresh) are the Leads page (2026-10-28); text filters with a NUL get 422; `GET /leads/{id}`
  returns detail with events, calls, messages, appointments, history and the `activity` feed.
- `_audit(...)` writes `dashboard_audit_log` with actor name and lead id; call it for every staff mutation.
- `_require_super_admin` guards global cadence/template writes and deletes (call it before opening a
  transaction).
- `POST /leads/{id}/stage` drives board drag-and-drop (including restart from Day 0 when moved to New).
  Booked is final (owner, 2026-10-26): a booked lead (status `booked`, any source) moved to any other column
  gets 409 `BOOKED_IS_FINAL_DETAIL`; Resume on a booked lead gives the same text. The only way out is the
  visit cancelled in Stride (`stride_leads.flag_cancelled_bookings` -> Appointment review). Do Not Contact
  (`/contact-rules`) on a booked lead stays allowed.

A lead with an open review (`needs_review`) cannot be resumed or moved to In cadence (409): it goes through
`resolve_review` (`services/lead_review.py`), which records who chose what.

## When adding a dashboard endpoint

Also add its path to `ALLOWED_PATH` in `rpt_frontend/app/api/dashboard/[[...path]]/route.ts`, or the
frontend proxy rejects it with "dashboard path not allowed".
