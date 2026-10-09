# Booking engine — voice-agent find-slots / book-appointment

Status (2026-10-08): built and deployed for the Stride **demo** practice. The voice agent does not use
it yet.

## Flow

```text
client's list ──> booking_locations, case_types, clinicians, clinician_assignments
booking-worker (every 5 min) ──Stride availability (31 days)──> availability_slots
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
| `availability_sync_state` | last refresh per location + minutes | location |
| `slot_offers` | slot offered on a call (so booking needs only date + time) | lead, slot, case type |
| `slot_holds` | slot reserved while booking; one active per slot (unique index), 5-minute expiry | slot, lead |
| `appointments` (+ columns) | the booking record: `slot_id`, `slot_hold_id`, `case_type_id`, `booking_location_id`, `stride_case_id`, `call_id` | |

A lead's `location` matches `booking_locations.name`; its `lead_type` matches `case_types.name`,
otherwise the default case type is used.

## Tools (Vapi custom tools, `POST`, Vapi auth)

**`/api/v1/tools/find-slots`** — read-only.
- `when` (required): `earliest`, `today`, `tomorrow`, `this_week`, `next_week`, `in_two_weeks`,
  `specific_date`.
- Optional: `specific_date` (YYYY-MM-DD), `time_of_day` (`morning` / `afternoon` / `evening`),
  `clinician_name`, `location` (only if the caller changes clinic).
- The lead comes from the call (`variableValues.lead_id`), never from the model.
- Reply starts with a code the prompt can branch on: `OPENINGS`, `NOTHING_IN_RANGE`,
  `NO_SUCH_THERAPIST`, `NO_OPENINGS_FOR_THERAPIST`, `NO_OPENINGS`, `UNKNOWN_LOCATION`, `PAST_DATE`, `BAD_*`.

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
`BOOKING_HORIZON_DAYS=31`, `BOOKING_MIN_NOTICE_MINUTES=120`, `BOOKING_SYNC_SECONDS=300`,
`BOOKING_CACHE_MAX_AGE_SECONDS=900`, `BOOKING_HOLD_SECONDS=300`, `BOOKING_STRIDE_TIMEOUT_SECONDS=6`,
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

## Test the voice agent locally (one command)

`scripts/booking_agent_test.py` runs everything on a Mac against its own database, so live is never
touched (it refuses to run if `.env` points at a non-local database). Texts are simulated locally.

```bash
uv run --frozen --with pgserver python scripts/booking_agent_test.py setup        # once (or --fresh)
uv run --frozen --with pgserver python scripts/booking_agent_test.py up           # start + tunnel
uv run --frozen --with pgserver python scripts/booking_agent_test.py call --phone +91XXXXXXXXXX \
    --name "Dev Chaudhary" --clinic "Laguna Niguel"   # real call now, prints transcript + result
uv run --frozen --with pgserver python scripts/booking_agent_test.py chat --name "Dev Test"  # type as the patient
uv run --frozen --with pgserver python scripts/booking_agent_test.py status | down
```

`up` restarts anything that stopped and, when the free Cloudflare tunnel address changes, points only the
new booking assistant (and its own tools) at it; the old assistant is never modified.
