# tests — pytest suite

Run from the repo root:

```bash
~/.local/bin/uv run --frozen python -m pytest -q
~/.local/bin/uv run --frozen ruff check src tests
```

Most tests need no database or network: they fake the psycopg connection (small `Connection` / `_Rows`
classes that match on SQL text) and monkeypatch provider clients. Follow that pattern for new tests.

Optional suites, skipped by default:
- `test_database_integration.py`, `test_number_block_integration.py`, `test_stride_import_integration.py` — need
  `TEST_DATABASE_URL` (a disposable Postgres with migrations applied, never production). Without Docker, the
  `pgserver` pip package runs an embedded Postgres 16; it lacks `pgcrypto`, which migration 001 only creates
  (`gen_random_uuid` is built in), so register an empty `pgcrypto` extension in that install first.
- `test_real_provider_contracts.py` — needs `RUN_REAL_PROVIDER_TESTS=1` and real sandbox credentials.

| File | Covers |
| --- | --- |
| `test_worker.py` | Cadence materialization, claim rules, dispatch, sweepers |
| `test_dashboard.py` | Dashboard API: roles, board stages, cadence versions, activity feed, lead create/delete |
| `test_n8n_integration.py` | Sheet actions (start/restart/DNC/booked, cooldown), profile sync, Sheet snapshots |
| `test_google_sheet.py`, `test_sheet_direct.py`, `test_sheet_direct_integration.py` | Direct Sheet sync with `fake_sheet.FakeSheet`: header mapping, moved-row safety, n8n rules and labels; Action empties once a command is handled (run, refused, re-picked N times, staff edit not wiped, a Restart on a review row sent and refused, an old waiting Restart emptied with the refusal, never run) and old commands from before 2026-10-25 are emptied, never run (unit tests use the `old` fixture in place of `lead_action_requests`); end to end on a real schema (`TEST_DATABASE_URL`) |
| `test_delivery.py`, `test_unspoken_call.py`, `test_last_decision.py` | Vapi end-of-call settlement and outcomes; `declined` only on the patient's own refusal (demo call 01a1795f), confirmation yes / slot matching helpers |
| `test_hang_up_booking_integration.py` | Yes to the booking confirmation then a hang-up (real schema, fake Stride): booked once, re-sent report books nothing more, hang-up before the confirmation / "no" / a question = not booked and not declined, slot taken or birthday changed = Needs Review, booking tool already called = nothing extra, booking off = no-op; real report shape (logged tool call that never ran + waiting line still books), "Yes. Bye." / natural yes, wording variants, cut-short / "maybe" / unparsable -> review, bare "no" to the confirmation not declined, a booking made meanwhile kept without a card, last name kept as spoken; the human copy's waiting lines after the yes still book |
| `test_hang_up_waiting_lines.py`, `test_vapi_booking_style.py` | Every tool waiting line (each line in `config/vapi_booking_tools.json` must be in the constant; the pre-2026-10-27 lines stay recognised) and Vapi's stock fillers are skipped when finding Elliot's last line; Elliot's human style keeps the 3f2 confirmation word for word, waiting-line variants, short closing |
| `test_review_pause.py`, `test_callback_timing.py` | Review/pause behaviour, callback scheduling |
| `test_lead_review.py`, `test_lead_review_integration.py` | Review codes/choices/Sheet words; every group's choices end to end, no Resume/board shortcut, history, phone change closes, calls only never texts; Booked / Do Not Contact typed on a flagged Sheet row run, Restart is refused at once; restart choices on the card (name, text, call, technical, other; cooldown both ways; none while another review is open); Restart refused on front desk; Location / Case / Date Of Birth reviews (use new, keep old written back, kept remembered, restart); email edit in Activity; every Sheet command in Activity; keep old name writes the old name back (no new review, no sync loop, a third name is kept) |
| `test_demo_review_integration.py` | Demo-only review groups: Stride cancel (front desk / restart / booking link, rebook still books), agent booking not confirmed (front desk only, Stride file books it), calls only stops the confirmation text |
| `test_booking_check_integration.py` | Unclear booking check with a fake Stride: not before a minute, time still open = not made, found in Stride's file = booked, backoff and stop after 30 min, run twice |
| `test_slot_booking_integration.py`, `test_call_outcome_lock_order.py` | Booking engine on a real schema with a fake Stride; failed bookings reach Needs Review (FOLLOW_UP, TAKEN after IN_PROGRESS; not TAKEN / bad details / staff), the same patient's second request waits for the first (booked → ALREADY_BOOKED, failed → front desk or other times, still running → IN_PROGRESS marked by lead), an end-of-call report waits for the booking (lead locked before event, no deadlock; unit test without a DB) |
| `test_keap_sync_worker.py`, `test_keap_sync_integration.py` | keap-worker commits new keap_sync rows before planning and `mark_booked` locks `for no key update` (a booking waited 58 s behind the hourly plan, 2026-10-26); Keap phase 2B sync against a real schema and `fake_keap` |
| `test_live_column_types_integration.py` | Sheet paths on the live text types of `lead_action_requests` |
| `test_lead_list_integration.py` | Leads page on a real schema: `STAGE_SQL` equals `stage_of()` for every status x cadence state x review x events combination, list paging / search / owner / location / source and Stride rows (unlinked patients only, view only), board per-column totals and paging, snapshot counts, owners and leads-created-per-day over every lead and old flagged leads kept; `test_dashboard.py` checks bad lead-list / lead-board input is a 422 |
| `test_security.py` | Dashboard headers, HMAC, Vapi/Twilio auth |
| `test_api_contract.py`, `test_vapi_contract.py`, `test_vapi_tool_config.py` | HTTP and Vapi tool contracts |
| `test_availability_offers.py`, `test_providers.py`, `test_mock_server.py` | Booking slots, provider clients, mocks |
| `test_agent.py` | Staff assistant |
| `test_stride_import.py`, `test_stride_import_integration.py` | Stride CSV parsing; import edge cases and lead booking/cancel on a real schema (`TEST_DATABASE_URL`) |
| others | parsing, retry, observability, usage report, fixtures, test-lead reset |

Every bug fix should add a regression test here that fails without the fix.
