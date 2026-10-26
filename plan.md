# Google Sheets + n8n integration plan

## 1. Goal

Clinic staff manage leads in Google Sheets. n8n sends staff commands to the AWS API. AWS and Supabase remain
the source of truth. Calls and SMS run in the existing cadence worker. A separate Sheet worker sends completed
results back through n8n.

```text
Google Sheet -> n8n intake -> HTTPS/Caddy -> AWS API -> Supabase
                                                       |
                                                       v
                                           cadence worker (30 seconds)
                                                       |
                                            Vapi calls / Twilio SMS
                                                       |
                                           provider callbacks -> AWS
                                                       |
                                             integration_outbox
                                                       |
                                           Sheet worker (30 seconds)
                                                       |
                                        n8n webhook -> Google Sheet
```

The Sheet never talks directly to Supabase, Vapi, or Twilio.

## 2. Exact Sheet columns

```text
Lead ID | Name | Phone Number | Email | Date Of Birth | Location | Case | Action |
Action Status | Cadence Day | Cadence events | Call Outcome | Message Outcome | Email Outcome |
Needs Review | Transcript Link | Callback At |
Action Request ID | Action Started At
```

| Column | Purpose | Owner |
|---|---|---|
| Lead ID | Supabase `leads.id` UUID | n8n intake/recovery |
| Name | Patient name; one word is allowed | Staff |
| Phone Number | Contact number and duplicate check | Staff |
| Email | Optional email | Staff |
| Gender | Optional dropdown M / F / Other (2026-10-20). Saved on the lead; the Keap pelvic tag uses it before Stride's gender. Other = male tag for now | Staff |
| Date Of Birth | Staff enters `MM/DD/YYYY` (US, month first, since 2026-10-21); stored as a date | Staff |
| Location | Location text | Staff |
| Case | Any non-empty case/lead type | Staff |
| Action | `Start cadence`, `Restart cadence`, `Do not contact`, or `Booked` | Staff pick it; direct mode only empties it once the command is handled (2026-10-25) |
| Action Status | Processing, success, duplicate, review, or error result | n8n |
| Cadence Day | Most recently completed cadence day, for example `Day 5` | AWS status sync |
| Cadence events | `Call`, `SMS`, or `Call + SMS` | AWS status sync |
| Call Outcome | Latest call result for the current cadence day | AWS status sync |
| Message Outcome | Latest SMS result for the current cadence day | AWS status sync |
| Email Outcome | Reserved for future email outreach; blank today | AWS status sync |
| Needs Review | Shows `Needs Review` when staff action is required | AWS status sync |
| Transcript Link | Dashboard calls page for the Lead ID | AWS status sync |
| Callback At | Readable Pacific time | AWS status sync |
| Action Request ID | Unique UUID for safe retry/idempotency. Direct mode: filled while a command runs, emptied with Action once it is handled (2026-10-25) | n8n / Sheet worker |
| Action Started At | Time used to detect a stuck request | n8n |

Protect the system-owned columns. Hide `Action Request ID` and `Action Started At`. Format Phone Number as
plain text.

### Identity rules

- `Lead ID` identifies the database lead.
- Phone Number detects an existing lead, but it must not be used to choose a Sheet row because duplicate rows
  can have the same phone.
- n8n uses the trigger's internal `row_number` for the first writes to the exact row. It is not a Sheet column.
- Later AWS status updates find exactly one row by `Action Request ID`. This avoids updating the wrong row
  when duplicate Sheet rows contain the same phone or Lead ID.
- `Action Request ID` makes API retries safe and identifies the exact Sheet submission. `Lead ID` still
  identifies the database lead.

## 3. Authentication design

Production keeps timestamped HMAC-SHA256 authentication over HTTPS. The real secret is never sent. Each
request carries a signature calculated from the timestamp and exact JSON body. Use a different secret for
each direction so leaking one does not open both endpoints:

```text
n8n -> AWS: N8N_INTAKE_SECRET
AWS -> n8n: N8N_SHEET_WEBHOOK_SECRET
```

Keep these non-authentication IDs:

- `X-Request-ID`: stable Action Request ID used for idempotency and safe retries.
- `X-Trace-ID`: correlation ID carried through n8n, AWS, workers, provider requests, responses, and logs.

HMAC authenticates the caller and proves the body was not changed. Request ID prevents duplicate work. Trace
ID helps diagnose the flow.

Rules:

- Generate both secrets randomly with at least 32 bytes.
- Store them only in AWS `.env` and private n8n workflow configuration/code.
- Never put real values in Git, exported workflow JSON, documentation, logs, or error messages.
- Sign `timestamp + "." + exactRawBody` and reject mismatched signatures with HTTP 401.
- Reject timestamps older than five minutes to prevent replay.
- HTTPS remains mandatory. The secret must never travel over HTTP.
- Google access continues to use the n8n Google OAuth credential.
- Vapi and Twilio keep their existing provider-specific webhook authentication.

Live verification passed: an invalid AWS-to-n8n signature returned 401, while a valid backend-generated
signature passed authentication and reached the expected 404 lookup for a nonexistent test Lead ID.

## 4. Endpoints

### Health

```text
GET /health
GET /ready
```

`/health` proves the API process is alive. `/ready` also checks required dependencies such as the database.

### n8n lead action intake

```text
POST /api/v1/integrations/n8n/lead-actions
```

Headers:

```text
Content-Type: application/json
X-RPT-Key-Id: <intake key ID>
X-RPT-Timestamp: <current Unix timestamp>
X-RPT-Signature: sha256=<HMAC signature>
X-Request-ID: <Action Request ID UUID>
X-Trace-ID: <one correlation ID for this execution>
```

Example body:

```json
{
  "action": "start_cadence",
  "lead_id": null,
  "lead": {
    "full_name": "Example",
    "phone": "+15555550123",
    "email": "example@example.com",
    "date_of_birth": "1990-12-31",
    "location": "Example location",
    "title": "Sports Rehab"
  }
}
```

Actions:

- `start_cadence`: Lead ID may be null for a new row.
- `restart_cadence`: Lead ID is required.
- `do_not_contact`: Lead ID is required.

Main responses:

| HTTP | Meaning |
|---|---|
| 201 | New lead and cadence created |
| 200 | Existing linked lead action completed or idempotent retry returned |
| 401 | Wrong shared secret |
| 409 `lead_already_exists` | Blank-ID row uses a phone already present in the practice |
| 409 `outreach_unresolved` | Restart while a call or text is still going out. Direct mode Sheet text: `Error: a call or text is going out right now. Pick Restart cadence again in a minute.` |
| 409 `restart_needs_review` / `restart_booked` / `restart_closed` | Direct mode only (`sheet_rules`, 2026-10-25): Restart on a lead in review, booked, or closed by a decision (`closed_reason`). Sheet texts in section 5 |
| 409 `outreach_due_soon` | Restart while a call or text is due: one the worker would send now, or one due within 5 minutes (30 s for TEST_MODE test leads), 2026-10-26; body has `retry_at` (due time, or now if late, + that window). Direct mode Sheet text: `Error: a call or text is due in the next few minutes. Pick Restart cadence again after 3:52 PM PT.` |
| 409 `restart_too_soon` | Restart within 10 minutes of the last Sheet restart; body has `retry_at` (ISO time a new Restart is accepted). Direct mode Sheet text: `Error: restarted less than 10 minutes ago. Pick Restart cadence again after 3:52 PM PT.` (Pacific) |
| 200 `already_booked` | Blank-ID row is the same person (phone + name + birthday) as a lead already booked; no new lead, no calls, `lead_id` empty (Sheet shows `Already booked`) |
| 409 `name_changed_needs_review` | Linked row's name differs from its database lead |
| 409 `phone_changed_needs_review` | Linked row's phone differs from its database lead |
| 409 `name_and_phone_changed_needs_review` | Both linked identity fields differ |
| 422 | Invalid request, phone, UUID, DOB, or required field |
| 429/5xx/timeout | Temporary failure; recovery may retry |

### Existing-lead profile synchronization

```text
POST /api/v1/integrations/n8n/lead-sync
```

Uses the same intake secret and a new `X-Request-ID`. Body:

```json
{
  "lead_id": "existing-lead-uuid",
  "lead": {
    "full_name": "Updated name",
    "phone": "+15555550123",
    "email": "example@example.com",
    "date_of_birth": "1990-12-31",
    "location": "Updated location",
    "title": "Updated title"
  }
}
```

Behavior:

- Same Lead ID, name, and normalized phone: update non-identity profile fields.
- Changed name or phone: preserve both stored identity fields, pause outreach, flag review, and return the
  matching name-only, phone-only, or combined `*_changed_needs_review` code.
- Blank Lead ID is invalid for this endpoint.
- Profile updates never create or restart cadence.
- Direct mode (owner, 2026-10-25; n8n mode unchanged): Location, Case and Date Of Birth edits go to review
  like a Name (`location_changed` / `case_changed` / `dob_changed`, Sheet text `Needs review: location changed`
  etc.; the lead keeps its value). Choices: `Use new <field> & continue`, `Keep old <field> & continue`, the
  two `... & restart from Day 0` variants, `Front desk will handle`. Keep old writes the old value back into
  the cell (dates as MM/DD/YYYY), like keep old name: only over the reviewed value, fingerprint stored, no new
  review, no loop. A finished lead keeps its value and is not reviewed. An Email edit is applied at once and
  noted in the lead's Activity ("Email changed in Google Sheet"). A phone change still closes the lead.
  A wrong email (e.g. "maria@gmail") is only a warning (owner, 2026-10-26): the cadence starts / goes on, no
  review, the wrong email is not stored ("Cadence started; email looks wrong, please fix it" /
  "Email looks wrong, please fix it"); fixing it applies the email and takes the warning off.

### Staff guidance for the Sheet (2026-10-25)

- Pick a command in Action; it runs once and the cell empties. Pick it again to run it again.
- Action must be chosen from the drop-down (owner, 2026-10-26). Any other text (e.g. "please call", or
  "restart cadence" / "booked" typed in another case) runs nothing: after the same 60 s settle the row says
  `Error: action must be chosen from the drop-down.` and Action empties. Only extra spaces are forgiven.
- A lead in Needs Review or closed is handled on the dashboard (Resolve review offers restart from
  Day 0); the Sheet's Restart refuses it and says why. Booked is final (owner, 2026-10-26): the dashboard
  board cannot move a booked lead either; cancel the visit in Stride (the lead comes back for review), or
  add a new row.
- Don't delete rows, don't copy-paste whole rows (the hidden Lead ID / Action Request ID would come along),
  and don't edit the hidden columns (Lead ID, Action Request ID, Action Started At). A new patient, or a new
  phone number, goes in a new row.
- Every Sheet command (and its refusal, with the reason) shows in the lead's Activity as "Google Sheet".

### AWS Sheet update webhook in n8n

```text
POST <N8N_SHEET_WEBHOOK_URL>
```

The Sheet worker sends `X-RPT-Key-Id`, `X-RPT-Timestamp`, `X-RPT-Signature`, and `X-Trace-ID` with a JSON
payload. n8n verifies the HMAC before reading or updating Google Sheets:

```json
{
  "event_id": "stable-outbox-event-id",
  "occurred_at": "2026-09-20T10:00:00Z",
  "lead_id": "lead-uuid",
  "sheet": {
    "action_status": "Booked",
    "cadence_day": "Day 0",
    "cadence": "Call + SMS",
    "call_outcome": "No answer",
    "message_outcome": "Delivered",
    "email_outcome": null,
    "needs_review": "",
    "transcript_link": "https://rpt-frontend-pi.vercel.app/leads/lead-uuid/conversations/calls",
    "callback_at": null
  }
}
```

n8n verifies the secret, finds exactly one Sheet row by Lead ID, updates only the supplied system fields, and
returns 200 only after Google confirms the write.

### Existing provider callbacks used by Sheet status updates

```text
POST /api/v1/vapi/webhook
POST /api/v1/vapi/tools
POST /api/v1/twilio/message-status
POST /api/v1/twilio/inbound-sms
```

Vapi sends call completion, outcome, transcript, callback, and tool information. Twilio sends SMS delivery
states and inbound replies. These endpoints update Supabase first; they never call Google directly.

## 5. Intake workflow

1. Google Sheets Trigger sees an edited row.
2. Continue only when Action is Start cadence, Restart cadence, Do Not Contact, or Booked and Action Status is blank. This prevents
   the workflow from reprocessing its own Sheet update forever.
   Exception (2026-10-07): a status ending in "in dashboard" ("Unblocked in dashboard", "Cadence resumed in
   dashboard", "Cadence restarted in dashboard") is written by AWS when an admin unblocks a number. Such a row
   is processed unless Action still reads Do Not Contact, so the stale command cannot block the patient again
   while a changed Action (Restart cadence, Booked) still goes through.
   Direct mode, a row in review (2026-10-22; Outcome Status "Needs review: ..." or "Number blocked; needs
   review"): Booked and Do Not Contact run as on any row and close the review ("booked" / "do_not_contact").
   Restart cadence on a lead in review is refused at once (owner, 2026-10-25; checked in the database, not
   the row): `Error: this lead needs review. Resolve it on the dashboard; you can restart it from there.`,
   and Action empties. It no longer waits in Action (the 2026-10-22..25 "parked" Restart is gone); the review
   card offers the restart instead. A row still holding such a waiting Restart ("...; resolve it on the
   dashboard first, then the restart runs") is emptied with that refusal text on the first poll, never run.
   **Action column rule (direct mode, owner 2026-10-25): staff pick a command, it runs, the cell empties.**
   Once a command is handled (it ran, the backend refused it, or the row could not run it) the Sheet worker
   empties Action, and the Action Request ID with it, in the same write as the result. The result stays in
   Outcome Status ("Cadence started", "Cadence restarted", "Booked", "Do Not Contact applied", "Error: ...");
   Lead Status as before. A command in Action is therefore always new, whatever Outcome Status says, and staff
   run the same command again by picking it again (a patient can be restarted any number of times; the
   10-minute restart cooldown still applies). The empty write only lands if the cell still holds the command
   that was handled: a command staff picked meanwhile is kept and runs next. An empty Action is never a command,
   and the worker never writes anything else into Action. Commands match the drop-down values exactly (only
   extra spaces forgiven, as n8n did); any other text, also another case, once settled, gets
   `Error: action must be chosen from the drop-down.` and Action empties the same way, nothing runs (owner,
   2026-10-26). Booked and Do Not Contact run at once, also on a lead
   in review. Restart guardrails (owner, 2026-10-25): the Sheet never undoes a dashboard decision. Refusals say
   what to do:
   - `Error: this lead needs review. Resolve it on the dashboard; you can restart it from there.`
   - `Error: this lead is booked. To start again, cancel the visit in Stride, or add a new row.` (2026-10-26;
     n8n mode still restarts a booked lead)
   - `Error: this lead is closed (<why>). Use the dashboard.` where <why> is `handled by front desk`,
     `a newer row has this phone number`, `not interested` or `closed on the dashboard` (moved to Closed on the
     board). Do Not Contact, wrong number / different person and phone changed keep their own refusals. A
     cadence that only ran out (Closed, no response; booking link sent; transferred) still restarts from the
     Sheet (2026-10-20).
   - `Error: a call or text is going out right now. Pick Restart cadence again in a minute.`
   - `Error: a call or text is due in the next few minutes. Pick Restart cadence again after <h:mm AM/PM> PT.`
     (2026-10-26: a step due within 5 minutes, 30 s for test leads; time = due time + that window, rounded up)
   - `Error: restarted less than 10 minutes ago. Pick Restart cadence again after <h:mm AM/PM> PT.` (the 10
     minutes count from the last restart, from the Sheet or from a review card)
   - `Error: this row already has a lead. Pick Restart cadence to start the calls again.` (Start cadence on a
     linked row)
   - any other backend refusal or row error: `Error: <reason>`.
   Rows from before 2026-10-25 can still hold a command that was already handled (the Booked, Start or Restart
   the backend used to write, or staff's own command that ran). On the first poll that sees such a row it is
   emptied, not run: a command next to a filled Action Request ID whose request finished with the same action
   (and Outcome Status is not blank), or one the old rules would not run (Booked next to "Booked", Restart next
   to "Cadence restarted", Do Not Contact next to "Unblocked in dashboard", Start cadence on a linked row, any
   command next to "Error: ..."). The old "(clear this cell to run it ...)" words are taken off its Outcome
   Status. A command picked under the old rules and not run yet (Outcome Status cleared, or a different
   command) runs as before.
   n8n mode still has the backend write Action (`sheet.action`: "Booked" once booked, "" after a Stride
   cancel; dashboard unblock / resolution values): the n8n intake has no memory of the last command.
3. Normalize phone and DOB. Accept a one-word Name and any non-empty Case.
4. Generate one Action Request ID.
5. Using the trigger `row_number`, write `Processing: <Action>`, Action Request ID, and Action Started At to
   that exact row.
6. POST the action to AWS with the intake secret and request UUID.
7. AWS completes one database transaction.
8. On success, n8n updates that exact request's row with Lead ID and `Cadence started`, `Cadence restarted`,
   `Do not contact applied`, or `Booked`.
9. On a permanent error, n8n writes the readable error and stops.
10. On timeout, 429, or 5xx, n8n leaves recovery information and the recovery workflow retries.

### Duplicate behavior

- A new blank-ID row with an existing phone is not connected to the old lead. Its own row receives
  `Lead already exists`, and Lead ID stays blank.
- The person's name does not affect the duplicate check.
- Editing Name or Phone Number on a linked row does not change either stored identity field. The row receives
  the matching review explanation, and the backend pauses outreach for staff review.
- Name review resolved "Same person, keep old name" (direct mode, 2026-10-23): the Sheet worker writes the
  name on file back into the row's Name cell, only while the cell still shows the reviewed new name (a third
  name staff typed is left for the normal name-change review), and stores the row's new fingerprint so this
  write never triggers a profile sync or a new review; the kept-name marker is cleared, so the same new name
  typed again opens a review. "Use new name" leaves the cell as it is.

This is why intake writes cannot match by Phone Number.

## 6. Database work when cadence starts

The API transaction updates these tables:

1. `lead_action_requests`: saves the request UUID, request hash, processing state, and final response. A retry
   with the same UUID/body returns the saved result instead of creating cadence twice.
2. `leads`: creates or updates the lead and stores `source_system='google_sheets'`.
3. `lead_status_history`: records the status transition and source.
4. `outreach_events`: creates one `planned` job for every active cadence step.
5. `integration_outbox`: stores durable Google Sheet notifications when required.

The active cadence is driven by `cadence_versions`, `cadence_steps`, and `message_templates`. The expected
14-day plan is:

| Day | Jobs |
|---|---|
| 0 | Call + SMS |
| 1 | SMS |
| 3 | Call |
| 5 | Call + SMS |
| 9 | SMS |
| 13 | SMS |

That produces eight `outreach_events`. Days without a configured event create no database job.

## 7. Cadence worker

1. Runs approximately every 30 seconds.
2. Holds a PostgreSQL advisory lock, so only one updated cadence worker can dispatch patient outreach.
3. Migration 025 also prevents stale workers from claiming new jobs.
4. Selects due `outreach_events` where `status='planned'` and `scheduled_for <= now()`.
5. Claims rows safely with database locking and changes them to `in_flight` before contacting a provider.
6. Sends calls to Vapi and SMS to Twilio.
7. Other due leads can be handled in the same batch/concurrently; a long call does not make the worker wait
   for the conversation to end because Vapi reports completion later by webhook.
8. Temporary dispatch errors are retried. Permanent or exhausted failures become `failed`/`unknown` and are
   also queued for Sheet reporting.

## 8. Provider completion and Sheet columns

### Call

1. Vapi callback identifies the call using its provider reference/Vapi call ID.
2. AWS finds the related `outreach_events` row and lead.
3. AWS updates `outreach_events`, `call_logs`, `call_transcripts`, and relevant `leads` fields.
4. Transcript text remains in `call_transcripts`; the Sheet gets only the dashboard link.
5. The same transaction inserts an `integration_outbox` event.

### SMS

1. Twilio callback identifies the SMS using the provider message ID.
2. AWS updates `sms_messages` and its related `outreach_events` row.
3. Delivered, undelivered, failed, and terminal provider results are queued for Sheet reporting.

### Sheet mapping

| Backend field | Sheet column |
|---|---|
| `cadence_day` | Cadence Day |
| `cadence` | Cadence events |
| `call_outcome` | Call Outcome |
| `message_outcome` | Message Outcome |
| `email_outcome` | Email Outcome (blank until email outreach exists) |
| `needs_review` | Needs Review |
| `transcript_link` | Transcript Link |
| `callback_at` | Callback At |
| `action_status` when present | Action Status |

Action Status is normally owned by intake/recovery. The status webhook may change it only for later system
outcomes such as `Cadence completed` or `Do not contact applied`. It must not replace `Cadence started` on
every callback.

The team-owned Booked action completes the cadence. A declined call terminates it. Wrong-number/person results
and failed or undelivered cadence SMS mark Needs Review and pause it. Booking-link-sent and transferred-call
outcomes are reported but do not stop later cadence steps.

Callback At is stored as an exact timestamp in Supabase and displayed as readable Pacific time, for example
`Sep 21, 2026 at 9:00 AM PT`.

## 9. Sheet worker and outbox

The Sheet worker is separate from the cadence worker.

1. It polls `integration_outbox` every 30 seconds for pending n8n jobs.
2. It locks a small batch so two Sheet workers cannot deliver the same event simultaneously.
3. It reads the latest committed database state for that Lead ID.
4. It POSTs the snapshot and shared secret to the n8n webhook.
5. HTTP 200 marks the outbox row `delivered`.
6. Timeout, 429, or 5xx schedules a retry with backoff.
7. Permanent/exhausted failures become dead-letter work for staff review.

Migration 026 ensures these Sheet events are produced only for leads whose source is Google Sheets. Slow or
unavailable Google/n8n services never block calls, SMS, or provider callbacks.

## 10. Recovery workflow

1. Runs every five minutes.
2. Finds rows stuck in `Processing:` or `Retrying:` whose Action Request ID and start time exist.
3. Rebuilds the same action and uses the same request UUID and body.
4. If AWS originally committed but its response was lost, `lead_action_requests` returns the stored result.
5. Therefore recovery fills the missing Lead ID without creating a second lead or second cadence.
6. Retry only timeout, 408, 425, 429, and 5xx responses. Do not retry permanent 4xx responses.
7. Stop after three attempts and write `Error: backend unavailable after 3 attempts`.

## 11. Failure protections

| Failure | Protection |
|---|---|
| n8n sends the same request again | Same Action Request ID/body returns the stored result |
| AWS commits but response is lost | Recovery obtains the saved Lead ID/result |
| Duplicate Sheet phone | Backend returns `lead_already_exists`; no old row is modified |
| Linked row name or phone changed | Backend preserves both identity fields, pauses cadence, and flags review |
| Worker crash | Database leases/locks allow safe recovery |
| Two cadence workers | Advisory lock permits only one dispatcher |
| Vapi/Twilio sends duplicate callback | Provider IDs and database constraints make processing idempotent |
| n8n/Google unavailable | `integration_outbox` retries; patient outreach continues |
| Invalid key, timestamp, or HMAC | Receiver returns 401 before processing |
| Sheet Lead ID missing | n8n returns 404 and changes nothing |
| Duplicate Sheet Lead ID rows | n8n returns 409 and changes nothing |
| Provider gives final failure | Failure is stored and still reported to the Sheet |

## 12. Required n8n corrections before production

1. Intake nodes `Write Validation Error`, `Mark Row Processing`, `Write Intake Result`, and recovery result
   must update the exact triggering row/request. Do not match these writes by Phone Number.
2. The AWS status webhook must find exactly one row by Lead ID.
3. Keep the verified HMAC code in `Verify AWS Request`, with the production Sheet key ID and webhook secret.
4. Replace the profile-sync signing placeholder with the intake HMAC configuration.
5. Remove the old `Phone Created New Lead?` and `Write Replacement Lead ID` path. Changed identity fields now
   require review and never create a replacement automatically.
6. Map backend conflicts clearly:
   - `lead_already_exists` -> `Lead already exists`
   - `name_changed_needs_review` -> `Name changed - needs review`
   - `phone_changed_needs_review` -> `Phone number changed - needs review`
   - `name_and_phone_changed_needs_review` -> `Name and phone number changed - needs review`
7. Keep the Action Status blank gate before intake to prevent an infinite trigger loop.
8. Ensure the webhook responds 200 only after the Google update succeeds.
9. Export the final active workflows after these corrections, remove credential values, and replace the stale
   checked-in templates before the PR.

## 13. Production cleanup completed

1. Google Sheet intake can no longer set `is_test` or request an accelerated cadence.
2. The temporary `N8N_SHEET_LEADS_AS_TEST` setting was removed.
3. `Dockerfile.prod` copies application code and database migrations only. Tests, development reset SQL, and
   fixtures are not present in the production image.
4. Automated tests remain in Git because they protect the PR and future releases; they are not runtime files.
5. HMAC, Request ID, Trace ID, duplicate protection, worker locking, and failure recovery remain enabled.

## 14. Production environment

Required production values, with actual secrets kept outside Git:

```dotenv
APP_ENV=production
TEST_MODE=false
OUTBOUND_ENABLED=true

SUPABASE_DB_URL=postgresql://...
PUBLIC_BASE_URL=https://<production-api-domain>
DASHBOARD_PUBLIC_URL=https://rpt-frontend-pi.vercel.app

N8N_INTAKE_KEY_ID=<intake-key-id>
N8N_INTAKE_SECRET=<independent-random-secret>
N8N_INTAKE_AUTH_DISABLED=false
N8N_PRACTICE_SLUG=<production-practice-slug>
SHEET_SYNC_ENABLED=true
N8N_SHEET_WEBHOOK_URL=https://<n8n-domain>/webhook/<production-path>
N8N_SHEET_KEY_ID=<sheet-key-id>
N8N_SHEET_WEBHOOK_SECRET=<different-random-secret>
SHEET_SYNC_POLL_SECONDS=30

PROVIDER_MODE=real
VAPI_MODE=real
TWILIO_MODE=real
```

Also configure the existing Vapi/Twilio credentials and callback URLs. Recreate containers after changing
`.env`; `docker compose restart` does not reload environment values.

## 15. Production rollout checklist

1. Review and merge only intended branch changes; do not commit `.env` or exported workflows containing
   secrets.
2. Apply migrations 023 through 029 in filename order and confirm migration status. Migrations 027-029 add
   the booking-link call outcome, review-pause fail-safe, and durable `Booked` Sheet action compatibility.
3. Confirm both n8n directions use the production HMAC keys and secrets.
4. Complete the remaining active n8n workflow corrections in section 12.
5. Set `TEST_MODE=false` and `N8N_INTAKE_AUTH_DISABLED=false`.
6. Confirm Caddy exposes only HTTPS API traffic; PostgreSQL and container port 8000 stay non-public.
7. Stop any development cadence worker before starting production. The advisory lock is additional safety,
   not a reason to intentionally run two production dispatchers.
8. Rebuild/recreate `api`, `worker`, and `sheet-worker` so code and `.env` are loaded.
9. Check `/health`, `/ready`, container health, and logs.
10. Test with one approved synthetic lead:
    - Lead ID and Action Status update on its exact row.
    - Eight outreach events are created for the configured cadence.
    - One Day 0 call and SMS dispatch.
    - Vapi/Twilio callbacks settle the correct events.
    - Cadence Day, Cadence events, separate outcomes, Needs Review, Transcript Link, and Callback At update
      on the row identified by Action Request ID.
11. Test duplicate phone, changed phone, name edit, lost intake response, failed SMS, duplicate callback, wrong
    secret, and unavailable n8n.
12. Monitor `provider_events`, `integration_events`, `integration_outbox`, worker logs, and dead-letter rows
    during the initial rollout.

## 16. Current verification status

- Backend tests: `180 passed, 3 skipped`.
- Ruff: passed.
- Production Compose configuration: passed.
- Database migrations 023, 025, and 026 were applied to the configured Supabase; migration 024 was already
  registered.
- HMAC was verified live in both rejection and acceptance paths. Backend, workers, and the production image
  are ready; complete the remaining active n8n workflow items in section 12 and the deployment smoke test.
