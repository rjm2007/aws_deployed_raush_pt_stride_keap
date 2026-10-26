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
  named date or month look up to <day 31>.` only on a too-far reply and on searches without a named day or
  month (`earliest` ... `in_two_weeks`). A named day or month inside the window (`specific_date`,
  `specific_month`, `next_month`) leaves it out: the agent compared the real day asked with the pinned last
  day and said "too far ahead" although times were found (chat sims 2026-10-23).
- When the pin moved the asked day or month, the reply names it as the answer:
  `OPENINGS for <service> at <clinic> for the day asked (on the booking calendar: Monday, July 27): ...`
  (`for the month asked (on the booking calendar: July 2026)`; NOTHING_IN_RANGE the same before `for that time`).
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
  `PATIENT_EXISTS` (lead flagged), `UNCONFIRMED` (unclear Stride result; lead flagged, never retried),
  `FOLLOW_UP` (could not finish in time, or Stride refused for another reason; lead flagged),
  `IN_PROGRESS` (an earlier request for this patient
  is still finishing; the agent says nothing (the tool's own waiting message plays) and books again with the same details, at most
  twice, then the UNCONFIRMED line; a repeat after the first request finished answers `ALREADY_BOOKED`;
  the same patient meeting its own hold on the slot also gets `IN_PROGRESS`, only another patient's hold is
  `TAKEN`), `BAD_*`.
- A request that finds this patient's attempt still running first waits for it (`IN_PROGRESS_WAIT_SECONDS`
  = 12, polled every second in short transactions, no locks held; the tool deadline still counts from the
  request's start). Then: booked → `ALREADY_BOOKED` with day, time, therapist and clinic; unclear →
  `UNCONFIRMED`; failed as taken → the normal flow (that time is gone: other openings, or this request's
  own different time is booked); failed otherwise → `FOLLOW_UP` (lead flagged); still running →
  `IN_PROGRESS`, marked on the patient's running attempt (`in_progress_reply_at`, by lead). Before this an
  instant `IN_PROGRESS` made the agent retry at once (the waiting message three times in a few seconds).
- `ALREADY_BOOKED` names day, time, therapist and clinic so the agent's closing (3h) is right.
- Not booked (owner, 2026-10-26): for `PATIENT_EXISTS`, `UNCONFIRMED`, `FOLLOW_UP` and a twice-failed tool
  the agent says "Our front desk team will take care of your appointment and reach out to you shortly"
  (never "we'll text you"), and the lead is always in Needs Review, "Booking" group (front desk only).
  `FOLLOW_UP` uses code `booking_failed` with `details.on_call` ("Booking did not go through on the call;
  book with the patient (why)"). `TAKEN` / `NOT_AVAILABLE` and validation replies are not flagged (the agent
  offers other times / asks again), except a `TAKEN` on an attempt that already answered `IN_PROGRESS`
  (`appointments.in_progress_reply_at`, migration 052): the patient may have heard the front desk line.
  Voice agent only; a staff calendar booking shows its error to staff. A booking later on the call (or in
  Stride's next file) settles the review (`mark_booked` clears it). The route's own unexpected-error reply
  flags `booking_unclear` and starts with `UNCONFIRMED:`.

Both always answer HTTP 200 with `{"results":[{"toolCallId","result"|"error"}]}`, one line. `error` is
used only when the tool itself failed (no lead on the call, booking switched off, Stride down).

## Yes, then a hang-up (owner, 2026-10-27)

A "yes" to Elliot's confirmation books the visit even if the patient hangs up before the booking tool
ran. The confirmation question is in `config/vapi_booking_assistant_prompt.md` (step 3f2; on the demo
booking assistant since 2026-10-27): "So that's [day] the [date] at [time] with [therapist first name] at our [clinic]
office. Shall I book it?"

`services/hang_up_booking.py` (`book_confirmed_time_after_hang_up`) runs from the end-of-call report
(`delivery.process_vapi_end_report`) for an answered call whose event is unsettled, before the post-call
summary.
- Elliot's last line (the tool's own "One moment, please, while I ..." line ignored) must be a
  confirmation: "(so) that's / that is ..." plus "shall / should / can I (go ahead and) book it / that
  (for you)", "(do you) want me to book it" or "would you like me to book it". Case and punctuation
  ignored.
- The patient's first real reply decides, with farewells and filler dropped (bye, goodbye, thanks,
  thank you, hello, I have to go, ma'am, sir, Elliot). Only yes words (yes, yeah, yep, sure, okay,
  alright, absolutely, definitely, perfect, please, correct, great, fine, uh huh, mhm, let's do it,
  book it, that works, sure thing, go for it, of course, go ahead, sounds good, please do, do it) ->
  book. A yes word plus other words, no no / change / question word ("Yes, Tuesday at nine is fine")
  -> Needs Review. After a yes, every later line is checked too (Vapi splits speech at pauses): a no,
  change word or question there -> Needs Review, never a booking. A "but", a question or a trailing
  "so" / "um" with a yes -> Needs Review. A no or change word (instead, actually, wait, another, let me
  check, call you back, ...) in the first reply, or no yes word -> nothing happens, the cadence continues.
  "No problem" is not a no; "why not" is a yes.
- Whether the booking already ran is decided from the database: an appointment for the lead in booking /
  scheduled / unknown (or booked elsewhere) -> nothing here. A `book_appointment` call in the report
  (`artifact.messages` role `tool_calls` / `tool_call_result`, `artifact.messagesOpenAIFormatted`
  `tool_calls`) is only logged: Vapi lists calls that never reached us when the patient hung up.
- Practice booking enabled (production: never runs).
- The spoken day / date / time / therapist must match exactly one slot this call was offered
  (`slot_offers` by the Vapi call id); the name is the lead's (a missing last name from the reply to
  "And what's your last name?", kept as spoken, e.g. "O'Brien", only when clear); Elliot checked the
  birthday on file and the patient said yes.

Then `book_slot` (voice_agent, call id, slot id, event id), so every normal rule applies. ALREADY_BOOKED /
IN_PROGRESS with a booking now on the lead (two reports at once) -> that booking stands, no card. Anything
else after a yes (a cut-short confirmation, a "maybe" reply, no single slot, no last name, birthday
changed, TAKEN, any reply but BOOKED): the call settles `manual` and the lead goes to Needs Review, code
`booking_said_yes`, Booking group: "Patient said yes to <day time> and hung up before the booking
finished; book by hand", with the reason in plain words ("that time was no longer free"). A re-sent
report books nothing more.

If no booking was made but the post-call summary still says `booked` (the patient asked about or picked
a time and hung up before confirming), the lead goes to Needs Review with code `booking_hung_up`, Call
group: "Patient asked to book, then hung up"; staff read the transcript, then continue the cadence or
have the front desk call (owner, 2026-10-27).

Check it all before a deploy: `uv run --frozen python scripts/check_hangup_rule.py` (16 cases, PASS / FAIL,
own throwaway Postgres on 127.0.0.1; see `../Hang-up-Booking-Tests.md`).

A post-call summary's `declined` is applied only when the patient's own lines contain a refusal
(`delivery.REFUSAL_PHRASES`; not inside a question), or a bare "no" / "nope" / "nah" as the answer to the
introduction ("Do you have a moment?"). A bare "no" to the clinic, a time or the confirmation means "not
that one". Otherwise the call counts as no answer and outreach goes on.

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
8. Lock order: lead, then the call's outreach event (as `_finish_booking`, `report_lead_status`,
   `apply_call_outcome` and the end-of-call no-outcome fallback do since 2026-10-26). An end-of-call report
   arriving while the booking finishes waits for it; the booking wins (the call stays Booked, no review).

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
