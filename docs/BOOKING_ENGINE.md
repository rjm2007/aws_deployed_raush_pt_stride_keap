# Booking engine — voice-agent find-slots / book-appointment

Status (2026-10-08): built and deployed for the Stride **demo** practice. The voice agent does not use
it yet.

## Flow

```text
client's list ──> booking_locations, case_types, clinicians, clinician_assignments
booking-worker ──Stride availability, 90 days in 31-day chunks──> availability_slots
               (chunk 0 every 5 min, later chunks every 30 min and on a new day)
Vapi find_slots ──> lead's location + case type ──> clinicians ──> 3 nearest open slots (slot_offers)
Vapi book_appointment ──> hold slot ──> live re-check ──> patient ──> case ──> appointment ──> booked
```

## Tables (migration `033_booking_engine.sql`)

| Table | One row per | Links |
| --- | --- | --- |
| `booking_locations` | clinic as leads name it, e.g. `Laguna Niguel` → Stride location id + timezone | |
| `case_types` | e.g. `Physical Therapy`, `Physical Therapy - Pelvic`: Stride appointment type, visit minutes, one default | |
| `clinicians` | Stride user we book for | |
| `clinician_assignments` | clinician sees case type at location (from the client) | clinician, location, case type |
| `availability_slots` | open Stride slot: clinician + location + minutes + start; `open` / `gone` / `booked` | clinician, location |
| `availability_sync_state` | last refresh per location + minutes + chunk (migration 051; chunk 0 = days 1-31) | location |
| `slot_offers` | slot offered on a call (so booking needs only date + time) | lead, slot, case type |
| `slot_holds` | slot reserved while booking; one active per slot (unique index), 5-minute expiry | slot, lead |
| `appointments` (+ columns) | the booking record: `slot_id`, `slot_hold_id`, `case_type_id`, `booking_location_id`, `stride_case_id`, `call_id` | |

A lead's `location` matches `booking_locations.name`; its `lead_type` matches `case_types.name`,
otherwise the default case type is used.

## Tools (Vapi custom tools, `POST`, Vapi auth)

**`/api/v1/tools/find-slots`** — read-only.
- `when` (required): `earliest`, `today`, `tomorrow`, `this_week`, `next_week`, `in_two_weeks`,
  `specific_date`, `next_month`, `specific_month`.
- Optional: `specific_date` (YYYY-MM-DD), `month` (YYYY-MM, with `specific_month`), `time_of_day`
  (`morning` / `afternoon` / `evening`), `clinician_name`, `location` (only if the caller changes clinic).
- Default searches (`earliest` ... `in_two_weeks`, time of day, therapist) and their fallbacks look only at the
  first `BOOKING_HORIZON_DAYS` (31). A named date or month looks up to `BOOKING_CACHE_DAYS` (90) (2026-10-23).
  A month with the pinned calendar moves like a day ("December" said in October -> August).
- OPENINGS / NOTHING_IN_RANGE end with `Calendar today: ... Last bookable day: <day 90>; searches without a
  named date or month look up to <day 31>.`
- The lead comes from the call (`variableValues.lead_id`), never from the model.
- Reply starts with a code the prompt can branch on: `OPENINGS`, `NOTHING_IN_RANGE`,
  `NO_SUCH_THERAPIST`, `NO_OPENINGS_FOR_THERAPIST`, `NO_OPENINGS`, `UNKNOWN_LOCATION`, `PAST_DATE`, `BAD_*`.
- A day (or month) past the cache (`BOOKING_CACHE_DAYS` from the booking calendar's today, so 90 days; 31 before
  2026-10-23; with `BOOKING_TODAY_OVERRIDE` the asked day is first moved onto the pinned calendar) answers
  `NOTHING_IN_RANGE: <day> is too far ahead to book yet; we can only book up to <last day>. ...` with the
  nearest openings from today (time of day kept if possible). `book-appointment` on such a day says the
  same before its other openings. Before 2026-10-22 it answered `NO_OPENINGS` although times were open.
- Nothing on the asked day or after it (e.g. the last day of the window): `NOTHING_IN_RANGE: no ... openings
  for that time or later. Nearest openings: ...` from today; `NO_OPENINGS` only when the whole window is empty.
  With a pinned day, a real past date (Oct 1 with June 15 pinned) is `PAST_DATE`.

**`/api/v1/tools/book-appointment`** — creates real Stride records.
- `date` (YYYY-MM-DD) and `time` the caller chose, `first_name`, `last_name`, `date_of_birth`
  (YYYY-MM-DD); optional `email`, `clinician_name`, `location`.
- Server fills phone, case title (the lead's case), default address, appointment type, clinician,
  location, start/end.
- Replies: `BOOKED`, `ALREADY_BOOKED`, `TAKEN` / `NOT_AVAILABLE` (with 3 new openings),
  `PATIENT_EXISTS` (lead flagged; team calls back), `UNCONFIRMED` (unclear Stride result; lead flagged,
  never retried), `FOLLOW_UP` (could not finish in time), `IN_PROGRESS`, `BAD_*`.

Both always answer HTTP 200 with `{"results":[{"toolCallId","result"|"error"}]}`, one line. `error` is
used only when the tool itself failed (no lead on the call, booking switched off, Stride down).

## Booking rules

1. Hold the slot (unique index: two calls cannot hold one slot).
2. Re-check that one slot live in Stride; gone → offer 3 others.
3. Patient → case → appointment; each Stride id is saved at once so a retry reuses it.
4. `is_pending=true`: Stride itself refuses an overlap.
5. Stride allows one Initial Evaluation per case (undocumented 403): a fresh case is made and the
   booking retried once.
6. Timeout after Stride may have saved → `unknown`, lead flagged, never retried.
7. On success: lead booked (calls/texts stop), confirmation SMS queued, Sheet updated, Keap handoff
   queued (not for test leads).

Speed: Stride is called with a 6 s timeout and no retries inside a call; the tool stops before 24 s.

## Settings (`.env`)

`BOOKING_TODAY_OVERRIDE` (demo only — pins "today"; Stride demo slots exist 2026-05-15 → 2026-07-29),
`BOOKING_HORIZON_DAYS=31` (default search window), `BOOKING_CACHE_DAYS=90` (cached and bookable; fetched in
Stride requests of at most 31 days), `BOOKING_MIN_NOTICE_MINUTES=120`, `BOOKING_SYNC_SECONDS=300` (chunk 0),
`BOOKING_FAR_SYNC_SECONDS=1800` (later chunks), `BOOKING_CACHE_MAX_AGE_SECONDS=900` (a search refreshes chunk 0
itself past this; a later chunk past this + `BOOKING_FAR_SYNC_SECONDS`), `BOOKING_HOLD_SECONDS=300`, `BOOKING_STRIDE_TIMEOUT_SECONDS=6`,
`BOOKING_TOOL_DEADLINE_SECONDS=24`, `BOOKING_DEFAULT_ADDRESS_1/CITY/STATE/ZIP`.
Booking also needs `practice_settings.stride_booking_enabled=true`.

## Operate

```bash
docker compose -f docker-compose.prod.yml run --rm api rpt booking-demo-seed   # DEMO data only
docker compose -f docker-compose.prod.yml run --rm api rpt booking-sync        # refresh once
docker compose -f docker-compose.prod.yml logs -f booking-worker
```

## Before production

- Replace the demo seed with the client's real clinic / case type / clinician list.
- Remove `BOOKING_TODAY_OVERRIDE`.
- Add the voice agent tools and prompt (not done; waiting for go-ahead).

## Test the voice agent (one command)

```bash
uv run --frozen python scripts/test_booking_call.py --phone +91XXXXXXXXXX --name "Dev Chaudhary"
#   options: --clinic "Dana Point" | "Mission Viejo"   --case "Physical Therapy"   --keep
```

Creates one test lead (is_test, cadence paused, texts off), has Vapi call the number now with the new
booking assistant (`config/vapi_booking_tools.json`), prints the transcript, tool calls and booking result,
then deletes the test lead (unless `--keep`). Uses the database and Vapi from `.env`; the old outreach
assistant and real leads are never touched.
