# SMS agent ("Elliot by text")

A patient texts the clinic number; Elliot answers by text, answers questions and books the Initial
Evaluation with the same business rules as voice Elliot, but written for text. Plan, decisions and owner
questions: `../../SMS-Agent-Plan.md` (workspace `demo/SMS-Agent-Plan.md`). Demo only; production later.

Status (2026-11-03): birthday step rebuilt (below, "Birthday step"), not committed or deployed.
Status (2026-11-02): built and integrated on branch `feature/own-postgres`, not committed, not deployed,
off by default (`SMS_AGENT_ENABLED`, `SMS_AGENT_TEST_ENABLED`). Unit and local-Postgres tests pass; the
prompt has not been run against a real model yet (next: scenario tests in the test chat). This file is the
build contract: module names, interfaces and rules every part codes against. Keep it true when something
changes.

## Parts and owners (one owner per file)

| Part | Files |
| --- | --- |
| Settings, dependency, entry point, schema | `config.py` (`SMS_AGENT_*`, `CLINIC_FRONT_DESK_PHONE`), `pyproject.toml` (`openai`, `rpt-sms-agent-worker`), `supabase/migrations/054_sms_agent.sql` — done, do not change without need |
| Prompt and tool descriptions | `config/sms_agent_prompt.md`, `config/sms_agent_tools.json` |
| Agent core | `services/sms_agent.py` (loop), `services/sms_agent_context.py` (lead view + history), `services/sms_agent_tools.py` (live + sandbox tools), `services/sms_agent_birthday.py` (birthday step, 2026-11-03), `services/sms_agent_texts.py` (editable texts), small changes in `services/slot_booking.py`, `services/lead_status.py`, `services/lead_review.py` |
| Inbound + worker | `routes/twilio.py`, `services/sms_inbound.py` (queue + "should we reply"), `sms_agent_worker.py`, `docker-compose*.yml`, `sender` on existing SMS inserts (`worker.py`, `routes/dashboard.py` manual SMS, `services/delivery.py`) |
| Test chat tab | `routes/sms_agent_test.py` (+ `api.py`, `routes/__init__.py`), `services/sms_agent_sandbox_setup.py` (booked visit + open review), frontend `../rpt_frontend` page; real-model scenario run: workspace `test/sms-agent-eval-2026-11-02/` (`run_eval.py`, `scenarios.json`; the AI patient gpt-5.4-mini is costed apart, `patient_sim`; each reply gets the earlier replies' actions as `prior_actions` like the tab; a scenario with `patient.script` sends those fixed texts with no AI patient (dob-01) and can check `max_cards` / `must_book` / `dob_asks`; harness-only flags `force_booking_failure`, `slot_gone`, `slot_taken_at_booking` (after the card, its slot is marked booked in the local DB so `book_visit` gets TAKEN: book-09), expect `booked_clinic` (np-03: no pelvic therapist at Dana Point, so the patient picks Laguna Niguel); x-04's AI patient has a first name in its persona (Dana) while the lead's first name on file stays empty (round 3: with none it answered with its last name)) |

## Flow (real texts, phase 1)

```text
Twilio -> POST /api/v1/twilio/inbound-sms
  store (sender 'patient', idempotent on MessageSid) -> STOP/START/HELP/CALL handled by code
  else services.sms_inbound.queue_reply(conn, phone, lead_id, inbound_id)   [pending job, due = now + quiet]
sms-agent-worker (own container, one instance, advisory lock):
  claim due job -> lock lead (lead row first, same order as bookings) -> should_reply()
  -> reply via sms_agent.reply_live(...) -> TwilioService.send_sms -> sms_messages (sender 'ai')
```

Phase 1: only a phone that belongs to a lead gets the agent. A phone with no lead gets the editable
`sms_agent.unknown_number` text, at most once per 24 h per phone, never the AI.
STOP from a phone with no lead puts the number on `suppressed_numbers` (like every other whole-number
opt-out; nothing removes it automatically, START does not) and, for any phone, closes its pending job
(`outcome='opted_out'`, `sms_inbound.stop_number`).

A job answers the texts up to its `last_inbound_id`. When the worker hands a job to the agent it first
moves `last_inbound_id` to the phone's newest inbound text (lead row locked), so a text that came in after
the claim is answered by this job, and the job that text queued finds nothing new and skips
(`nothing_to_answer`). The agent then reads the patient's texts only up to that `last_inbound_id`
(`reply_live` -> `load_live(up_to_inbound_id=...)`): a text that commits after the move (one that
waited on the lead lock commits right then) is left to the job it queued. Never two replies to the
same text.

## Core interface (`services/sms_agent.py`)

```python
@dataclass
class LeadView:                      # everything the model may know about the person texting
    is_new_person: bool              # True only in the test tab ("New person", phase 2 flow)
    first_name: str; last_name: str  # "" when unknown
    date_of_birth: date | None
    clinic: str                      # booking_locations.name or ""
    service: str                     # case type name, e.g. "Physical Therapy"; "" when unknown
    referral_word: str               # e.g. "knee"; "" when unknown
    status: str                      # leads.status
    cadence_state: str
    needs_review: bool; review_reason: str
    booked_visit: str                # e.g. "Tue Nov 3 at 9:00 AM with Alan at Laguna Niguel", or ""
    recent_calls: list[str]          # one line per call: when + outcome (+ short summary)

@dataclass
class Turn:
    role: Literal["patient", "elliot", "cadence", "staff"]
    text: str
    at: datetime | None

@dataclass
class AgentAction:                   # shown in the test tab; kinds below
    kind: str                        # 'find_times' | 'list_therapists' | 'check_birthday' | 'confirm_visit' | 'book' | 'report' | 'hand_off' | 'emergency' | 'fixed_text'
    summary: str                     # plain words, e.g. "Would book Tue Nov 3 9:00 AM with Alan"
    result_code: str                 # first word of the tool reply (OPENINGS, BOOKED, ...), except
                                     # ToolResult.action_code: REPORTED_<STATUS> (REPORTED_CASE_CHANGE,
                                     # REPORTED_DECLINED, ...) for a recorded report_outcome, and
                                     # HANDED_OFF_PAUSED for a hand_off that stops booking (different_case,
                                     # booking_problem); the model itself still reads REPORTED / HANDED_OFF
    reason: str | None = None        # a hand-off's reason (insurance, ...); None for other kinds

@dataclass
class AgentReply:
    texts: list[str]                 # usually one text; never empty unless no_reply
    actions: list[AgentAction]
    no_reply: bool = False           # e.g. "ok"/"thanks" after a booking
    error: str | None = None         # model timeout etc.: nothing sent, staff flagged (live)
    opted_out: bool = False          # Elliot opted the patient out in this reply: the worker still
                                     # sends this one reply although the lead is opted out by then

def run_agent(*, lead: LeadView, history: list[Turn], tools: "AgentTools", now: datetime,
              front_desk_open: bool, settings: Settings, client=None, model: str | None = None) -> AgentReply

def reply_live(trace, *, lead_id: str, job_id: int, settings=None, client=None, providers=None,
               now=None) -> AgentReply
    # builds LeadView + history from the DB, LiveTools, runs the agent
def reply_sandbox(*, setup: SandboxSetup, history: list[Turn], settings=None, client=None,
                  trace=None, prior_actions: list[dict] | None = None) -> AgentReply
    # test tab: made-up lead, SandboxTools, writes nothing; prior_actions = the earlier replies'
    # actions [{kind, result_code, reason?}], oldest first (see Test chat tab)
```

`client` is an injectable OpenAI client (tests pass a fake). Model: `settings.sms_agent_model` unless
`model` is given (test tab model choice). Use the **Responses API** with function tools, `store=False`
(nothing kept at OpenAI), and pass reasoning items back between tool rounds
(`include=["reasoning.encrypted_content"]`) per OpenAI's function-calling guide. The include (and any
reasoning parameter) goes only to reasoning models: gpt-5*, gpt-6*, o-series (`o3`, `o4-mini`; a
`provider/` prefix is ignored), `sms_agent.is_reasoning_model`; gpt-4.1 and the like get neither (a 400
otherwise). Max `sms_agent_max_rounds` tool rounds, whole run under `sms_agent_timeout_seconds` (default
35 s since round 2, 2026-11-02; was 20). One model call waits at most `sms_agent.CALL_TIMEOUT_SECONDS`
(20 s); a call that times out is tried once more when at least `RETRY_MIN_SECONDS` (8 s) of the overall
time is left (fact-09; log `sms_agent_model_retry`), never past the overall cap. Other model errors are
not retried.
Any exception inside a tool goes back to the model as `ERROR: ...` (log: tool + error category only) and
the run goes on, so a booked text made earlier in the same reply is never lost (`sms_agent_tools.call_tool`).

Messages to the model: the system/developer prompt from `config/sms_agent_prompt.md` (static, so it caches),
then a developer "Context" block built by code (lead view, today and clinic time, front desk open now, the
clinic facts that change rarely stay in the prompt), then the history as user (patient) / assistant
(elliot) turns; cadence and staff texts are given as labelled context lines, not as assistant turns.
Patient and Elliot turns start with their clinic time, `[Mon Nov 2, 3:14 PM] `, so old texts read as old;
a copied stamp is removed before sending. Context labels (the prompt relies on them; `tests/test_sms_agent_config.py` checks the list): Now, Tomorrow, Front desk open
now, Front desk phone, Booking link, Person texting, Name on file, Birthday on file ("yes" only, never the
date, since 2026-11-03), Date of birth confirmed in this chat (below, "Birthday step"), Clinic, Service,
Referral is for, Services we book, Other words for services, Lead status, Cadence, Needs review, Booked
visit, Recent calls. "Needs review" is `yes (<reason>)`, `no`, or `front desk only (<reasons>; booking by text still
works)` when every open review is Elliot's own front-desk card that allows booking (see Rules); the Lead status
then reads "front desk will reach out about a question (calls paused)" instead of "waiting for staff review".
"Tomorrow" is the next day spelled out with its YYYY-MM-DD (book-12 searched "tmrw" as today). Code
backs it up (round 2): `find_times` with `when="today"` searches "tomorrow" when the patient's latest
text has tomorrow / tmrw / tmr / tmw / tomoro / 2morrow (whole word, any case) and not "today"
(`sms_agent_tools.search_when`, live and sandbox). Round 3 (book-12: the day moved but the reply did not
say so, and the model texted "nothing this afternoon"): an OPENINGS / NOTHING_IN_RANGE reply to a today or
tomorrow search names the day searched right after its code word, "NOTHING_IN_RANGE: Searched tomorrow, Thu
Nov 5 (the patient said tomorrow). Name that day in your text ("Thu Nov 5"); never "today" ... " (the
bracket only when `search_when` moved the day; `sms_agent_tools.name_search_day`). The code word stays
first (the prompt and the side panel branch on it). The day is read from the reply's own "Calendar today"
note (`calendar_today`, the pinned demo calendar included), so no extra database read; without that note
the reply still says "Searched tomorrow". Other `when` values and other codes are left as they were. Eval
2026-11-03 (book-12 again: "tmrw aftrnoon" came as `when="specific_date"` with today's date): a
`specific_date` equal to the calendar's today counts as a search for today (`search_todays()`).
The model's final answer `NO_REPLY` (or a copy like "(empty answer)") = no text, but only when the code
guards below allow it.

### Booking steps run by code (owner, 2026-11-04)

Yajat's test chat: "yes" to the card got a front-desk hand-off and no booking (gpt-5.5 chose wrong in 1 of 8
replays). The must-not-fail steps now run in code before the model (`sms_agent.code_steps`); the model
reads a "Code step" note with each tool's reply, those tools are taken out of its list for that reply, and
a call to them anyway gets ALREADY_CALLED without running:
- A plain yes to the card (`said_yes_to_card`), lead with both names on file: `book_visit` for the card
  (date from `texts.card_date`); IN_PROGRESS = once more; still IN_PROGRESS or ERROR = `hand_off`
  booking_problem. After BOOKED the model cannot hand off booking_problem.
- `confirm_visit` before a confirmed date of birth texts our question naming the slot
  (`texts.birthday_question`: "For Mon Jul 20 at 5:45 PM with Alan Rome at our Dana Point office, to confirm
  it's you, what's your date of birth?"), never twice in a row. The patient's next text, once the chat has
  a confirmed date of birth and the text changes nothing (no day, time, "instead", no), gets the card for
  that slot from code (`card_after_birthday`). A date that differs from the file stays the model's
  (double-check).
- The model asking for the date of birth in its own words before any such question is sent back once
  (guard `birthday_question`, forces `confirm_visit`); a second time it goes out.
- The card's first line is "Here's your visit:" (was "Great choice! Here's your visit:"; old cards still
  parse).

### Code guards on the model's own text (gpt-5.5 eval, 2026-11-02)

The eval (`../../test/sms-agent-eval-2026-11-02/`, 25 of 63 passed) showed a model that ignores the
prompt: "All set! You're booked" with no booking, its own card text ("Reply YES and I'll book it",
"I'm holding ..."), silence after a yes. `sms_agent._check_answer` runs on every final answer (never on
a fixed text from a tool):

- **Booking claim** (`claims_booking`: you're booked, I've booked you, your visit is booked/confirmed,
  see you on/then, confirmed for, a text starting "Booked"; "all set" / "you're set" only when no other
  outcome was reported in this run and the text is not about a call) with no tool reply BOOKED /
  ALREADY_BOOKED in this run, on a lead with no visit on the books (`has_booked_visit`: status Booked or a
  Booked visit line in Context; that visit may be restated, review 2026-11-02): the
  text is dropped (never sent, never fed back). If Elliot's last text is the card and the patient's
  latest text is a plain yes (`said_yes_to_card`; never a `clear_no`, never a yes asking for something
  else: but / instead / actually / another / earlier / later / change ...) and book_visit was not called in
  this run, the model runs once more with `tool_choice={"type":"function","name":"book_visit"}` (the
  tool's card guard still checks everything); else once more with a developer note.
- **Own card** (`looks_like_card`: reply/text YES, visit / read-back card, "send the card", "I'm
  holding", "on hold for you"; also "here's your visit" / "review this visit", except on a lead with a
  visit on the books, where that answers "when is my appointment?") with no CARD_SENT in this run:
  dropped; re-run forced to `confirm_visit` when the text names a clock time and confirm_visit was not
  called in this run, else a developer note.
- **Empty answer** with no fixed text: allowed only when the patient's latest text is a plain
  acknowledgement (`pure_acknowledgement`: ok, k, thanks, thank you, got it, sounds good, "I'll wait",
  "see you then", "I'll call them", emoji only; never "yes") and Elliot's texts just before it finished something and did not end on a
  question (`finished_before`: the booked, front desk, English-only or emergency text, the opt-out
  line, or a lead that is booked / callback / link sent / declined / in a review that stops booking).
  Else one re-run with the note "the patient expects a reply; continue the step you were on".
- **Reaction to the card** (2026-11-03): an empty answer is also fine when Elliot's last text is the card and
  the patient's latest text is a reaction to it ('Liked "..."', 'Reacted 👍 to "..."', `reacted_to_card`), or
  when confirm_visit replied CARD_ALREADY_SENT in this run.
- **Birth date** (round 2, book-11 "Got it, Aug 23, 1985."): a model text holding the patient's full date
  of birth with the year (the one on file, or a date at least a year back in the patient's own texts, as
  8/23/1985, 08-23-85, 1985-08-23, Aug 23, 1985, 23 August 1985; `writes_birth_date`) is dropped and re-run
  with `BIRTH_DATE_NOTE` (month and day only). Second miss: `error="guard:birth_date"`. Checked first.
- **Front desk promise** (round 3, x-04 "I couldn't finish booking that by text, so our front desk will
  finish setting up your visit." with no hand-off: nobody would have called): a model text that says the
  front desk / our team / staff will reach out, call, finish, set up, take care of ... or "I'll let / I've
  passed ... our front desk" (`promises_front_desk`; a pointer like "call our front desk at ..." is not one)
  is dropped and re-run with `FRONT_DESK_NOTE` ("call hand_off now with the right reason, or do not promise
  it"), unless the front desk really has it (`front_desk_has_it`): a hand_off / flag_emergency in this run,
  a REPORTED_* / HANDED_OFF* / ALREADY_* / EMERGENCY* code, FOLLOW_UP / UNCONFIRMED / PATIENT_EXISTS,
  REVIEW_OPEN, CASE_CHANGE or ERROR in this run, a lead with `needs_review` or status callback_scheduled, or
  the front desk / English-only text earlier in the history. Second miss: `error="guard:front_desk_promise"`
  (fixed texts from this run still go). Checked after the card guard, before the repeat guard.
- **Repeat** (round 2, x-02 / x-05): a model text identical, ignoring case, spaces and punctuation, to one
  of Elliot's last 3 texts in the history (`repeats_recent`) is dropped and re-run with `REPEAT_NOTE`
  ("Reply differently in one short line, or NO_REPLY"); NO_REPLY after that note is accepted. Still
  identical: nothing of the model's is sent (fixed texts from this run still go; none = no reply).
  Round 3 (x-05: "Please have Ruth text us directly or call our front desk ..." went out 3 times, each time
  inside another text): a text whose sentences (split after . ! ? and at line breaks, each compared the
  same way, `sentences`) all appeared somewhere in those last 3 texts counts as a repeat too; one new
  sentence makes it a new text.
- Fixed texts from tools are never checked by these guards.
- Each guard re-runs at most once per reply; re-runs count toward `sms_agent_max_rounds` and the
  timeout (a forced tool needs a tool round left, else note only). A second miss sends nothing
  invented: a claim, card or front desk promise -> `error="guard:booking_claim"` / `"guard:card_text"` /
  `"guard:front_desk_promise"` (fixed texts from
  this run still go; with none, the worker flags staff as for any agent error); an empty answer -> no
  reply. Log: `sms_agent_guard` with `guard` and `step` (forced_<tool> / note / dropped) only.

## Tools the model gets (`config/sms_agent_tools.json`, names and parameters fixed here)

| Tool | Parameters | Live | Sandbox |
| --- | --- | --- | --- |
| `find_times` | `when` (slot_booking.WHEN_VALUES), `specific_date` YYYY-MM-DD, `month` YYYY-MM, `time_of_day` morning/afternoon/evening, `therapist`, `clinic`, `service` | `slot_booking.find_slots` (conversation key `sms:<lead_id>`); "today" after a "tmrw" text = "tomorrow" (`search_when`); NO_CLINICIANS (no therapist for that service at that clinic) names the clinics that offer it: "NO_CLINICIANS: no Pelvic Health therapist at Dana Point. Offered at: Laguna Niguel, Mission Viejo. Ask if one of those works." (none: "..., and no other clinic offers it. Offer to text the booking link or hand off to the front desk."; np-03) | same search on the real cache, read-only: no offers remembered, no Stride refresh; same tomorrow and NO_CLINICIANS rules |
| `list_therapists` | `clinic` | `slot_booking.list_providers` | same, read-only |
| `confirm_visit` | `date`, `time`, `therapist`, `clinic`, `service` | checks the time is open in the cache; returns the read-back card text; code sends it word for word | same |
| `book_visit` | `date`, `time`, `therapist`, `first_name`, `last_name`, `date_of_birth`, `clinic`, `service` | **refused by code** unless Elliot's last text was the `confirm_visit` card for the same date/time/therapist and the patient has replied since in words (a picture or file alone, `MEDIA_ONLY`, is no answer), and, when their only text since the card is one text, it holds none of the whole words no / nope / nah / not / wait / later / cancel / change ("no problem", "not a problem", "no worries" do not count; `clear_no`). The yes itself stays the model's judgment; two or more texts since the card are left to the model. Names (round 2, x-04, `booking_names`): an existing lead is booked under the names on file, whatever the model passes; with no first name on file the model's `first_name` is used only when the patient wrote it in this chat (whole words, a patient text) and it is not the last name, else `MISSING_NAME` ("ask for their first name only", the last name is on file; with their answer `confirm_visit` again for the same time in that turn, then `book_visit` after the yes: a `book_visit` right after the name is NOT_CONFIRMED because the card is no longer the latest text; `confirm_visit` says "No first name is on file" in its reply; round 3, x-04, checked end to end in the sandbox: `test_sandbox_missing_name_then_first_name_sends_the_card_again_and_books`). `load_live` takes the full name's first word as the first name only when it is not the last name on file. The booked text greets only a name on file or written by the patient (`greeting_name`), else "You're all set!". Then `slot_booking.book_slot(source="sms")`; BOOKED -> the `sms_agent.booked` text word for word (if reading the Studio text fails after the booking, the draft in code goes out instead, log `sms_agent_booked_text_fallback`, so a made visit is never reported as an ERROR) | same guard; "BOOKED (pretend)" if the time is open in the cache, else the real TAKEN-style reply with other times |
| `report_outcome` | `status` (callback_scheduled, booking_link, declined, do_not_contact, wrong_person, case_change), `notes`, `callback_type`, `delay_minutes`, `callback_datetime_iso` | `lead_status.report_lead_status(call_id="sms:<lead_id>:<job_id>", source="sms agent", queue_link_text=False)` (the reply carries the link: no separate link text); `do_not_contact` by text = SMS opt-out (`explicit_opt_out(channel="sms")`), and with notes containing "no contact at all" the call opt-out too (= Do Not Contact); the reply says "texts stopped" or "texts and calls stopped" | "Would report ..." |
| `hand_off` | `reason` (insurance, different_case, wants_person, other_language, unknown_question, booking_problem, other, cancel_visit), `notes` | opens a Needs Review card (new code `sms_front_desk`, group call, source `text`, choices Front desk will handle / Continue cadence); no second card when this job already opened one for the same thing (`different_case` after `report_outcome case_change`, `booking_problem` after a failed booking); reply = `sms_agent.front_desk` (or `english_only`; for insurance `sms_agent.insurance`: "Our front desk can answer your insurance question. ...", migration 058, which also stands in for the general text when the same reply hands off another topic (Emma's retest 2026-11-13); for booking_problem `sms_agent.booking_problem`: "I couldn't finish booking that by text, so our front desk will finish setting up your visit. You can also call them at ...", migration 057, also sent by book_visit on FOLLOW_UP / UNCONFIRMED / PATIENT_EXISTS; the model writes nothing more and code drops its own copy of that line (Emma's test 2026-11-11: two texts at once); or for cancel_visit `sms_agent.cancel_visit`: "Got it, I've passed that to our front desk. Your visit stays booked until they confirm the change with you. You can also call them at ...", migration 055, 2026-11-07) word for word. insurance, wants_person, other_language, unknown_question, other and cancel_visit do not stop booking (Rules). A repeat (ALREADY_HANDED_OFF) gives the model the front desk number for a patient who asks how to reach them | "Would open a Needs Review card: ..." |
| `flag_emergency` | `kind` (medical, self_harm) | reply = the fixed emergency text for the kind (code, not editable): medical = 911 only, self_harm = 911 and 988 (Suicide and Crisis Lifeline; 2026-11-07, Yajat's test); card note "Medical emergency" / "Thoughts of suicide or self-harm"; + Needs Review card (`sms_front_desk`, reason emergency: stops booking) | "Would flag an emergency" |
| `send_intro` | — | reply = `sms_agent.intro` (`new_person_intro` for a new person) word for word | same |
| `check_birthday` | `date_of_birth` (the patient's words as typed, or YYYY-MM-DD) | `sms_agent_birthday.check` (code, no model, no database): MATCH / NEW / CONFIRMED_DIFFERENT / DIFFERENT / NEED_YEAR / NOT_POSSIBLE / UNREADABLE (below) | same |

`service` (find_times, confirm_visit, book_visit; strict, nullable, enum "Physical Therapy", "Physical
Therapy - Pelvic", null): only for someone new who chose a service; always null for a lead with a referral
(their Service is used; another one is a case change). A repeated hand-off for the same topic replies
ALREADY_HANDED_OFF (nothing sent); the prompt answers anything else or NO_REPLY.

Prompt shape (2026-11-02, after the gpt-5.5 eval: 25 of 63 passed): the booking is a fixed sequence
(time picked -> identity: birthday check, or name + date of birth + clinic + service for someone new ->
`confirm_visit` in the same turn as the birthday yes or the given date of birth -> clear yes to the card ->
`book_visit` at once). The model never writes a card, a hold or a booked text; NO_REPLY only after an
acknowledgement or a fixed text that covers the turn; one hand-off / case change / emergency per topic.
Examples show tool calls on their own `Tool:` lines (an example in `[...]` was copied into a real text).
`tests/test_sms_agent_config.py` checks the shipped prompt and tools: strict schemas, only real tools and
Context labels, no example that answers the birthday yes with silence or writes a card or booking.

Prompt fixes after eval round 2 (2026-11-02, `tests/test_sms_agent_prompt_round2.py`): a service case
change names the Service on the referral ("Your referral is for our Physical Therapy service, so our front
desk will set up the pelvic visit with you.", ho-06); a callback on a clinic holiday names it ("We're closed
for Thanksgiving on Thu Nov 26.") and keeps the patient's clock time on the next open day when it is inside
callback hours, else 8:00 AM (holidays listed with names, cb-04); "can I see Alan?" is a search, and a plain
yes / sure / any after the day question or a therapist list searches when=earliest at once (book-06); a date
of birth is only ever written as month and day (book-11); NO_CLINICIANS with "Offered at" offers those clinics
in one line and searches again (no hand-off), without it hand_off booking_problem (np-03); MISSING_NAME on
book_visit asks for the first name once, then book_visit again. (Since 2026-11-03 MISSING_NAME comes from
confirm_visit, before the card: see Birthday step, "Fixes after that eval".)

Prompt fixes after eval round 3 (2026-11-02, `results-gpt-5.5-round3.json`, also in
`tests/test_sms_agent_prompt_round2.py`): MISSING_NAME asks for the first name only (never the last name, on
file), then confirm_visit for the same time and book_visit after the yes, never a hand-off for it (x-04); the
front desk is promised only after hand_off (or a tool reply that says the front desk text went) (x-04); the
same request again after it was answered gets one short new line or NO_REPLY, never the same sentences
(x-05, also in "Someone else on the lead's phone"); a today / tomorrow search reply's named day is the day
said in the text (book-12; `find_times` description too).

Tool replies start with a code word (like the voice tools: OPENINGS, NOTHING_IN_RANGE, BOOKED, TAKEN, ...),
so the prompt can branch on it. The lead always comes from the job / sandbox setup, never from the model.

`service` in code (2026-11-02, np-03): passed to the booking engine as the case type (live
`find_slots(case_type_name=...)`, `load_context`, `book_slot(arguments["case_type"])`; sandbox
`sandbox_context`), aliases resolved there. For a lead with a referral a service that resolves to another
case type than `LeadView.service` is refused with `CASE_CHANGE: ...` (never booked by text; the same
service by another word is fine). The card gets a service line under the therapist when the service is
not the practice's default (`case_types.is_default`): `confirm_card(..., service=)`, `parse_card` returns
`service` (None = default). `book_visit` books the card's service; a `service` that differs from it is
NOT_CONFIRMED.

Therapist names (2026-11-02, "Alan Rome" for Alan Brooks): `confirm_visit` resolves the name against the
clinicians at that clinic for that service (`resolve_therapist`): a name that matches as given is kept;
else one of its words must fit exactly one clinician (that one is used), none -> `NO_SUCH_THERAPIST`,
several -> `WHICH_THERAPIST`. The card prints the slot's real name. `find_times` retries a
NO_SUCH_THERAPIST search once with the one clinician the name can mean (`closest_therapist`). The
booking guard accepts a model therapist that shares a word with the card's (`same_therapist`); the
booking always uses the card's therapist.

Repeats in code (2026-11-02, ho-01..06, x-02, fact-05, sf-02): a `hand_off` for the same reason already
done in this reply or (live) a `sms_front_desk` card with that reason still open and flagged in the last
24 h, a `report_outcome case_change` with a `case_change_asked` card still open and flagged in the last
24 h (a resolved card is no repeat: asking again after staff closed it is a new request, review
2026-11-02; a
`different_case` hand-off counts that too), and a `flag_emergency` with an emergency card in the last
hour open no new card, change nothing and send no fixed text. Replies: `ALREADY_HANDED_OFF` /
`ALREADY_REPORTED` / `ALREADY_SENT` ("answer the new text briefly or NO_REPLY for thanks"; for an
emergency "one short kind line"), the same codes in the side panel; booking stays paused as before.
This job's own cards (`details.conversation` / `details.call_id` = `sms:<lead>:<job>`) do not count, so a
case change and its different_case hand-off in one reply still send the front desk text once
(`LiveTools._look_back`, one query per key per reply). Test chat: from `prior_actions` and the history
(`EarlierInChat.done`: HANDED_OFF -> `hand_off:front_desk`, HANDED_OFF_PAUSED -> `hand_off:paused`,
REPORTED_CASE_CHANGE, EMERGENCY_SENT, the front desk / English-only text in the history); the side
panel's codes carry no reason, so there a hand-off counts by its kind (booking goes on / stops).
2026-11-02 (x-02): the route and the tab carry an optional hand-off `reason` (below, Test chat tab). The
agent core sets it (`ToolResult.reason` -> `AgentAction.reason`, on HANDED_OFF, HANDED_OFF_PAUSED and
ALREADY_HANDED_OFF), and `earlier_in_chat` turns a known reason into `hand_off:<reason>`, so the test chat
compares the reason like live (insurance then wants_person is a new hand-off). An action without a reason
(or an unknown one) still counts by its kind.

## Birthday step (owner, 2026-11-03)

Test chat that started it (demo booking calendar pinned to 2026-07-15, test lead born 2026-11-01): "is
your birthday November 1?" -> "yep" -> card -> "yes" -> "Could you send your date of birth with the
month, day and year?" -> "nov 1, 2026" -> "Got it, Nov 1." + the same card -> "yes" -> "Could you send
your date of birth again with the year?" ... never booked. Cause: `slot_booking.validate_patient` compared
the birthday with the pinned calendar's today (2026-11-01 > 2026-07-15 = BAD_DATE_OF_BIRTH "not
possible"), and the prompt let Elliot ask again and send the same card again.

Owner decision: Elliot asks once for the complete date of birth ("To confirm it's you, what's your date
of birth?"), never reads the date on file, and once a valid date was given in the chat never asks again.

- `services/sms_agent_birthday.py` (deterministic): `parse_birth_date` reads the first date in the
  patient's words: 3/14/1988, 03-14-1988, 3.14.88, March 14 1988, Mar 14, 1988, March 14th, 1988,
  14 March 1988, 1988-03-14, mar 14 '88; month first, a first number over 12 is the day; a 2-digit year
  is 20xx only when that is at least a year back, else 19xx, except last year to 2 years ahead ("26",
  "27" in 2026): a visit day like "can I come 11/20/26?", read as no date (review: it was 1926 and
  settled the date of birth). A month and day alone = need year. Times
  are never years ("Nov 20 10:30", "Nov 20, 10am"). `possible`: at least 1 and at most 120 years old,
  against the REAL clinic date (`real_today`), never `BOOKING_TODAY_OVERRIDE`.
- `check_birthday` codes: MATCH (= on file), NEW (no date on file / someone new), DIFFERENT (a possible
  date that is not on file: Elliot asks once, "Could you double-check that date of birth for me?"),
  CONFIRMED_DIFFERENT (settled after the double-check: the patient's own date is used, like voice; the
  booking summary says it differs and `appointments.patient_dob` keeps it for staff), NEED_YEAR,
  NOT_POSSIBLE, UNREADABLE (no date, or a date the model worked out that the patient never typed in a
  readable form). Tool replies never hold the date on file.
- `birthday_state(lead, history)` reads only the patient's own texts (live and test chat alike), and only
  a date given as a date of birth (`given_as_birth_date`, review 2026-11-03: "I hurt my knee on 3/2/2024"
  settled someone new's date of birth and they were never asked): the text names it (birthday, born,
  DOB, birth...), our latest text before it asked about it, or the text has at most 3 other words
  ("Ana Ruiz 8/23/1985"). check_birthday on any other date replies UNREADABLE with the DOB question. A
  date equal to the file (or any possible date when none is on file) confirms; a different one is pending
  until the patient types it again, says yes right after the double-check text, or gives a second date
  (that one is used). A later explicit correction replaces a confirmed date. `run_agent` writes
  "Date of birth confirmed in this chat: yes (never ask for it again)" / "no" / "not yet: ... ask once to
  double-check it" (`context_line`; through the tools' `birthday_state`, so Context and booking agree).
  Live gives the model the last 20 texts, but the birthday step reads the last 200
  (`BIRTHDAY_HISTORY_LIMIT`, `LiveContext.birthday_history` -> `LiveTools(birthday_history=...)`), so a
  date of birth given further back is still never asked again (review 2026-11-03).
- Year sent alone (eval x-07, 2026-11-03: "June 3", "Could you send the year too?", "1989" was UNREADABLE
  and the whole question came again): a patient text that is only a year ("1989", "'89", "it's 89")
  right after their month-and-day text, when our text before it asked about the year or the date of
  birth, is that full date (`month_day_only` + `year_for` in `birthday_state`). A "10" after "nov 20"
  answering "what time?" is not. `check_birthday` with only the year (or the joined date) then gives
  MATCH / NEW like a full date.
- Never asked again, in code (review 2026-11-03): when the chat has a confirmed date of birth, a model
  text that asks for one (`asks_birth_date`: date of birth / birthday / DOB / born with a "?" or send /
  type / text / share / tell / confirm / what's / give, in one sentence) is dropped and re-run with
  `ASKED_BIRTH_DATE_NOTE`; second miss: `error="guard:asked_birth_date"`. Checked right after the birth
  date guard.
- `confirm_visit` sends no card until the chat has a confirmed date of birth (`NEED_BIRTH_DATE`, with the
  double-check wording when one is pending). `book_visit` books with the confirmed date whatever the
  model passes (else the model's, else the one on file).
- Booking engine: `slot_booking.birth_date_today(context)` = the real clinic date; `book_slot` (voice and
  text) checks the date of birth against it, so BAD_DATE_OF_BIRTH is only a truly impossible date (the
  future, or over 120 years). The same fix is right for voice: a date of birth is a real-world date and
  production has no pinned day (no change there). The sandbox checks the same way.
- Never the same card twice in a row: when Elliot's latest text is that same card, `confirm_visit` replies
  `CARD_ALREADY_SENT` (nothing sent). A new step in between (an answer, a name question) allows it again.
  A reaction to the card gets NO_REPLY.
- Loop stop: book_visit refusals BAD_DATE_OF_BIRTH, NOT_CONFIRMED, MISSING_NAME, BAD_NAME, BAD_EMAIL
  (`sms_agent_tools.REPEATED_FAILURES`) are counted per code over this reply and earlier in the chat (live:
  `sms_agent_actions` rows `tool='book'` for the lead in the last 24 h; test chat: `prior_actions` with
  kind `book`, `EarlierInChat.failures`). The second one for the same code does `hand_off booking_problem`
  in code (front desk text + card, booking paused) and replies `HANDED_OFF` (side panel
  `HANDED_OFF_PAUSED`, reason booking_problem; ALREADY_HANDED_OFF when the front desk already has it). Log
  `sms_agent_booking_gave_up` (code only).
- Test chat setup: the birthday must be at least 1 year back (route 422 "at least 1 year ago", tab
  `setupProblems` / `latestBirthday`, date input `max`).
- Tests: `tests/test_sms_agent_birthday.py` (parser, 2-digit visit years, dates not given as a date of
  birth, a year sent alone after the month and day, state, codes, tools, loop, never asked again, live reads further back, engine date),
  `tests/test_sms_agent_integration.py` (the test chat end to end on the pinned calendar, engine accepts a
  birthday after the pinned day, live hand-off after a stored failure, live double-checked date books and
  the card is not sent twice, a date of birth more than 20 texts back still counts), route and tab tests.
- Eval (2026-11-03, gpt-5.5, `test/sms-agent-eval-2026-11-02/results-gpt-5.5-dob.json`): the 22
  scenarios that reach the birthday step plus dob-01 (the reported loop: "nov 1, 2026" then the real
  date) and dob-02 (the date without a year, then with it). Existing patients: asked once, "To confirm
  it's you, what's your date of birth?", never read back, never asked again, booked. The model often
  goes straight from the typed date to `confirm_visit` without `check_birthday`: safe, since the card
  and the booking use `birthday_state` from the patient's texts.
- Fixes after that eval (2026-11-03, 4 failures; tests in brackets):
  - np-03 (someone new wrote "I'd like to book. Brooke Tanner, 02/09/1993"; the date was not counted and
    Elliot asked for it again, against the owner rule): `given_as_birth_date` also counts a possible full
    date that can only be a date of birth, unless the text is about something that happened (hurt,
    injury, surgery, accident, fell, broke, sprain, tore, since, happened, operation, diagnosed:
    `_EVENT_WORDS`): one at least 10 years back (`ONLY_BIRTH_YEARS`), one right after a name (the name on
    file or a capitalised first + last name, not a month / weekday / "Labor Day": `next_to_name`), or one
    sent while an earlier date of birth question is still unanswered (`asked_before`). Still not counted:
    "can I come 11/20/26?", "I hurt my knee on 3/2/2024", "after Labor Day 9/1/2025"
    (`test_a_date_that_can_only_be_a_date_of_birth_counts`, `test_np03_check_birthday_is_new_not_asked_again`).
  - book-12 ("tmrw aftrnoon" searched as `when="specific_date"` with today's date): `search_when` also
    moves a `specific_date` equal to the calendar's today (`search_todays()`: the clinic's real date and
    the demo's pinned day; the test chat's clock) to "tomorrow", and the reply names the day like the
    when="today" case (`test_search_when_a_specific_date_today_after_tmrw_is_tomorrow`,
    `test_find_times_specific_date_today_after_tmrw_searches_tomorrow_and_names_it`).
  - x-04 (no first name on file: after the yes Elliot asked "what's your last name?" and booked the last
    name as first_name): the first name now comes BEFORE the card. `confirm_visit` replies MISSING_NAME
    (`MISSING_FIRST_NAME_CARD`, "ask for their first name only", no card) until a first name is in the
    chat: the model's `first_name` when the patient wrote it and it is not the last name, or the patient's
    short answer right after "What's your first name?" (`first_name_in_chat`; also used by
    `booking_names`, so the last name passed as first_name books the answer). `book_visit` keeps its own
    MISSING_NAME guard. A model text asking for the last name when it is on file (`asks_last_name`) is
    dropped and re-run once with `ASKED_LAST_NAME_NOTE`; second miss `error="guard:asked_last_name"`
    (`test_no_first_name_on_file_asks_for_it_before_the_card`, `test_first_name_in_chat`,
    `test_asks_last_name`, `test_a_lead_with_no_first_name_books_only_under_a_name_it_wrote`).
  - dob-01 (the AI patient sent the date of birth before Elliot asked, so the loop was never tested):
    `run_eval.py` takes a scripted patient (`"patient": {"script": [...]}`: fixed texts in order, no AI
    patient) and optional checks `max_cards`, `must_book`, `dob_asks` ({turn: [min, max]} Elliot texts
    asking for the date of birth). dob-01 now sends "Hi, I'd like to book my first visit, mornings
    please", "the first one", "nov 1, 2026", "nov 1, 1990", "yes" and expects one card, a booking, the
    question once after the pick and once after 2026, never after 1990. The prompt also says never to
    remark on whether the date matches our records ("identity confirmed", "that matches")
    (`test_no_remark_on_whether_the_date_of_birth_matches_the_file`).

## Editable texts (`services/sms_agent_texts.py`)

`message_templates` rows with `cadence_step_id is null` and key `sms_agent.*` (seeded by 054; staff edit
them in the SMS Template Studio). `render(conn_or_none, key, **values)` reads the active row, falls back to
the same draft in code. Placeholders: `{first_name} {booking_link} {front_desk_phone} {day} {time}
{therapist} {clinic} {address}`; an unknown placeholder is left as is, never an exception. Keys:
`intro, new_person_intro, booked, front_desk, english_only, text_only, unknown_number`. The booking link is
`practice_settings.booking_link_url`; the front desk phone `CLINIC_FRONT_DESK_PHONE`. Clinic addresses: the
three approved addresses (voice prompt "Clinic facts").

## Should we reply (`services/sms_inbound.should_reply`) — order matters, first match wins

`SMS_AGENT_ENABLED` off -> store only · lead `do_not_contact` / SMS opt-out / number suppressed -> nothing ·
`SMS_AGENT_TEST_LEADS_ONLY` and not `is_test` -> nothing · wrong person (closed as wrong number) -> nothing ·
lead replaced by a newer lead on the phone -> use the active lead · staff texted the lead in the last
`SMS_AGENT_STAFF_QUIET_HOURS` -> nothing · a call to the lead in flight -> retry the job in 60 s ·
`SMS_AGENT_DAILY_REPLY_CAP` (default 25) replies today to the phone (jobs that sent a text, not text parts;
never for `is_test` leads) -> the `reply_limit` text once + one Needs Review card, then nothing that day
(Emma's test 2026-11-11: 10 text parts over her test leads left her questions unanswered) · media only (MMS) ->
`text_only` text · otherwise the agent. Declined / closed / link sent / cadence finished leads get the
agent (owner).

A patient's text holds the cadence (client's option A, Emma's test 2026-11-13): while Elliot answers the lead
(`SMS_AGENT_ENABLED`, and `SMS_AGENT_TEST_LEADS_ONLY` off or a test lead), a call or cadence text that comes due
within `CADENCE_HOLD_AFTER_TEXT_HOURS` (24) of the patient's last text to any lead on the phone is skipped, never
sent later (`worker.skip_steps_held_by_texting`, just before the claim). Later steps go out on their normal times;
each new text starts the hold again. `TEST_MODE` test leads use `CADENCE_HOLD_AFTER_TEXT_TEST_MINUTES` (10). 0 = off.
Activity: "Call skipped: patient was texting with Elliot". The `call_in_progress` wait above is unchanged.

Just before sending, `hard_stop` is checked again (opt-out, suppressed, calls only, staff texted). Two
exceptions (owner, 2026-11-02): a booking made in this reply (`book` action with result BOOKED) always
gets its `sms_agent.booked` text, whatever the stop; the rest of the reply is dropped (job outcome
`booked_text_only:<stop>`). Elliot's own opt-out in this reply (`AgentReply.opted_out`) sends only his own
words ("We won't text you again.") plus a booked text from the same reply, never an intro, card, hand-off
or emergency text, even when the booked text no longer matches its template (then it counts as
his own words). The worker tells the texts apart by their templates (`sms_agent_texts`).

Failure cards (agent error, send failure, reply cap; codes `sms_agent_failed` / `sms_agent_reply_cap`):
a lead in the cadence is flagged like any review (paused). A booked, declined, closed or cadence-finished
lead gets the card without a pause or status change (`details.finished`, like Elliot's front-desk card on
such a lead): its only choice is Front desk will handle, which closes the card and never changes the
lead's status (no Continue cadence or restart, which would un-book a booked lead). An unsent booked text
says so on the card ("call the patient and confirm the day and time"). A lead opted out of texts (SMS opt-out, Do Not Contact, calls only) gets an Activity line
only, no card, unless the unsent text was a booking confirmation (`details.booked_text_unsent`).

## Test chat tab (`/api/v1/dashboard/sms-agent-test/*`)

Stateless sandbox: `POST /reply` with `{setup, history, message, prior_actions?}` -> `{texts, actions, no_reply, model, ms}`;
`GET /options` -> clinics, services, models, default model, statuses, max_turns (60), max_text_chars (1600), max_chat_chars (40,000). Writes nothing to the database, never calls
Twilio or Stride's API (reads the cached open times only). `SandboxSetup`: `who` (`existing` | `new`),
`status`, `clinic`, `service`, `referral_word`, `first_name`, `last_name`, `date_of_birth`, `front_desk`
(`auto` | `open` | `closed`), `clock` (ISO local time or null = now), `model`, and (2026-11-02 eval
follow-up, bk-02 / ls-01) `booked_visit` and `open_review`. The request's `booked_visit` is
`{at, therapist?, clinic?}` (clinic wall time; clinic '' = the lead's), only for an existing lead with
status `booked` (else 422); the route turns it into the Context line live writes ("Tue Nov 3 at 9:00 AM with
Alan at Laguna Niguel", `services/sms_agent_sandbox_setup.booked_visit_text`) and passes that string as
`SandboxSetup.booked_visit`. `open_review`: `auto` (default, as before: status Needs attention = a full
review) | `none` | `front_desk` (only Elliot's own front-desk card: `front_desk_only`, booking by text goes on,
Lead status reads "front desk will reach out about a question") | `full` (a review that stops booking); a
new person only `auto` / `none` (else 422). `sms_agent_sandbox_setup.apply_setup_extras(lead, setup)` maps
them to the LeadView like `load_live` does for a real lead (a card pauses a lead in the cadence:
needs_attention / paused; a final status is kept). The route passes these two fields only when
`SandboxSetup` has them; a chosen visit or review the agent's `SandboxSetup` does not know yet is a 501,
never silently dropped. `sms_agent.SandboxSetup` has `booked_visit: str = ""` and
`open_review: str = "auto"`, and `sandbox_lead` ends with `apply_setup_extras` (done 2026-11-02). Chats live in the viewer's
browser only (several chats, each with its own history). Off unless `SMS_AGENT_TEST_ENABLED`.
Any dashboard user may use it; rate-limited by `SMS_AGENT_TEST_REQUESTS_PER_MINUTE`.
What Elliot did earlier in the chat (2026-11-02 review: a case change on turn 1, "book me" on turn 2 booked in
the test chat where live refuses): `prior_actions` (optional) = every earlier reply's `actions` as
`[{kind, result_code, reason?}]`, oldest first (the tab sends every action in that chat's side panel, `replyRequestFor` in `../rpt_frontend/app/sms-agent-test-data.ts`; `kind` up to 40 characters, `result_code` up to 60, `reason` up to 40 letters / underscores, else 422); the route passes it to `reply_sandbox(prior_actions=...)`.
`reason` (2026-11-02, x-02: the codes alone cannot tell a repeat of the same hand-off from a new one):
each `/reply` action has `reason` (a hand-off's reason, e.g. `insurance`; `null` when the agent gives none,
read as `getattr(action, "reason", None)`); the tab keeps it with the action (also after a reload) and sends
it back as given; the route trims and lower-cases it and passes it on only when set (an action without one
reaches the agent as `{kind, result_code}`, as before). The sandbox
rebuilds the lead from it and from Elliot's own texts in the history (`sms_agent.earlier_in_chat` /
`apply_earlier`): BOOKED, or the `sms_agent.booked` text in the history -> Booked; REPORTED_CASE_CHANGE,
REPORTED_WRONG_PERSON, HANDED_OFF_PAUSED, EMERGENCY_SENT (or the emergency text in the history),
FOLLOW_UP / UNCONFIRMED / PATIENT_EXISTS -> a card that stops booking (lead in review, paused);
HANDED_OFF -> Elliot's front-desk card, booking goes on; REPORTED_DECLINED / CALLBACK_SCHEDULED /
BOOKING_LINK -> that status; REPORTED_DO_NOT_CONTACT -> no reply any more (like live). Without
`prior_actions` (an older tab) only the history cases work.
Models (owner, 2026-11-02): `SMS_AGENT_MODEL` first (the default), then gpt-5.5, gpt-6.1-terra, gpt-6-luna,
gpt-6.1-sol, gpt-5.4-mini (`TEST_MODELS`); any other model is a 422. Limits: the tab reads them from
`/options`, warns 10 texts (or 10% of the characters) before the limit, stops sending at it and offers
"Start a new chat with the same setup" (a 422 there cannot be retried). A text over max_text_chars is refused
in words; nothing is trimmed.

### Live test lead (2026-11-14, `services/sms_live_test.py`)

The sandbox cannot show the real system (the cadence hold, several texts answered together, a booking on the
dashboard), and our tester's Indian phone cannot text the US number. So the tab's third choice plays the patient
of a real **test** lead (`is_test`, same practice; anything else is a 404):
- `GET /live/leads` -> the newest 50 test leads with a phone (`id, name, phone_last4, status, status_label,
  cadence_state, needs_review, created_at`).
- `POST /live/leads/{id}/texts` `{text}` -> runs the Twilio webhook's own handler (`routes/twilio._handle_inbound_sms`:
  keywords, stored as a patient text, reply queued) with a `dashboard-test:` sid. 409 if another lead now holds the
  phone (the text would be filed there) or Elliot is off. Shares the per-user rate limit.
- `GET /live/leads/{id}/chat` (read only) -> `lead`, the last 200 `messages` (`from_dashboard`), the cadence `steps`
  (a hold skip has note "patient was texting with Elliot"), Elliot's `actions`, `elliot_typing` (a pending or
  running job), `hold_until`.
- The sms-agent-worker saves a reply whose job answers a dashboard text (`sms_inbound.answers_dashboard_text`:
  the job's newest text) as an `ai` text with status `delivered`, **never texted**. A real text from the phone
  after it is answered by text as usual. Calls and schedule texts still go to the lead's phone; a booking is
  real (demo Stride).

## Rules

- Booking while a review is open (owner, 2026-11-02): any open review stops `confirm_visit` / `book_visit`
  (REVIEW_OPEN), live and sandbox, except Elliot's own front-desk card (`sms_front_desk`) with reason
  insurance, wants_person, other_language, unknown_question or other (`review.FRONT_DESK_BOOKABLE_REASONS`,
  `review_allows_booking`, SQL `BOOKABLE_REVIEW_SQL`; `LeadView.front_desk_only` / `booking_paused`). Still
  stops booking: a case change, `sms_front_desk` for different_case, booking_problem or emergency, failed or
  unclear bookings, name / detail reviews, wrong person, every other code, and `needs_review` with no open
  review row. A card opened earlier in the same reply counts too (`paused_by_hand_off`, plus a failed booking
  or a case change in that reply). The card still pauses the cadence (status needs_attention) like any review.
  A booking made while it is open keeps it open (`lead_status.mark_booked`, `KEPT_ON_BOOKING_SQL`, like the
  case change card): the lead is Booked, cadence completed, `needs_review` stays true, the card offers Front
  desk will handle only (closes it, lead stays Booked), the Sheet's Outcome Status is "Booked; needs review:
  patient needs the front desk (by text)" (`SHEET_BOOKED_FRONT_DESK`), and a Stride cancel is still seen
  (`stride_leads._BOOKED_WATCHED`). A booking settles an `sms_front_desk` card for booking_problem.
- An outcome by text (or on a call) while a review is open (2026-11-02 review). declined, already booked
  and wrong_person close Elliot's own `sms_front_desk` cards (not an emergency one) with the note "Closed: the
  patient declined before the front desk reached out" (`lead_status.settle_front_desk_cards`; resolution
  `closed`, by "Elliot (text)"); with no other card left `needs_review` clears, so a declined lead is simply
  Declined. Any other card stays open, like voice (the decline is recorded: status declined, cadence
  terminated, `needs_review` stays), but on a declined or Do Not Contact lead no choice that restarts
  outreach is offered or accepted (Continue / resume, restart, send booking link; `lead_review.DECIDED_STATUSES`):
  what is left (Front desk will handle, or Booked by front desk on a case change) closes the card and keeps
  the status. wrong_person adds its own card. "No contact at all" (Do Not Contact) clears every card, as
  before; a texts-only opt-out keeps the card (calls may go on). A callback or booking link while a card is
  open: the card stays and the status is recorded (the worker sends nothing while `needs_review`).
- Emergency texts (in code, never editable; split 2026-11-07 after Yajat's test, where chest pain got the 988
  line): medical "Please call 911 right now if this is an emergency. Our team has been alerted too."
  (`EMERGENCY_TEXT`); self-harm "If you're in danger, please call 911 right now. You can also call or text 988
  (Suicide and Crisis Lifeline) any time. Our team has been alerted too." (`SELF_HARM_TEXT`). The one text
  before (911 + 988, owner 2026-11-02) is still recognised in a chat's history (`is_emergency_text`).
- Test chat repeats (2026-11-07): with the side panel's codes, an earlier hand-off counts only for its own
  reason, like live. The front desk text alone counts for every bookable reason only in a chat without codes
  (it made a later cancel "already handed off" in Yajat's test).
- Never log message bodies, names, phone numbers or dates of birth (observability.py redaction); actions
  store tool + result code only.
- Never send a text from the sandbox; never write to the database from the sandbox.
- Unit tests fake the OpenAI client and the connection; database tests use a local throwaway Postgres
  (pgserver, see `scripts/check_hangup_rule.py`), never the `.env` database.

## Open (owner decisions and follow-ups, 2026-11-02)

- Done 2026-11-02 (owner): Elliot's own front-desk card for insurance, parking, "call me" and the like no
  longer blocks booking by text (Rules).
- Done 2026-11-03 (owner): the identity check asks for the date of birth and compares it in code
  (Birthday step). Voice still asks "is your birthday March 14?".
- Done 2026-11-02 (owner): the emergency text also gives 988 (Rules).
- `book_visit` could take only name and date of birth (code reads the rest from the card).
- Reasoning effort / verbosity are not set on the model call; measure in the test chat (35 s overall,
  20 s per call with one retry on a timeout, up to 5 rounds). When set, only for reasoning models (`is_reasoning_model`).
- A tool ERROR (a database error in a tool) is logged only; no Needs Review card yet.
- No "Intro sent" Context line: history holds the last 20 texts only.
- Holidays are written in the prompt (to Dec 2027) and also in `practice_settings.holidays`.
- The test chat looks the practice up by slug (`rausch-pt`); fine with one practice.
