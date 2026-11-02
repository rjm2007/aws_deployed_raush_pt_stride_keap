# SMS agent ("Elliot by text")

A patient texts the clinic number; Elliot answers by text, answers questions and books the Initial
Evaluation with the same business rules as voice Elliot, but written for text. Plan, decisions and owner
questions: `../../SMS-Agent-Plan.md` (workspace `demo/SMS-Agent-Plan.md`). Demo only; production later.

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
| Agent core | `services/sms_agent.py` (loop), `services/sms_agent_context.py` (lead view + history), `services/sms_agent_tools.py` (live + sandbox tools), `services/sms_agent_texts.py` (editable texts), small changes in `services/slot_booking.py`, `services/lead_status.py`, `services/lead_review.py` |
| Inbound + worker | `routes/twilio.py`, `services/sms_inbound.py` (queue + "should we reply"), `sms_agent_worker.py`, `docker-compose*.yml`, `sender` on existing SMS inserts (`worker.py`, `routes/dashboard.py` manual SMS, `services/delivery.py`) |
| Test chat tab | `routes/sms_agent_test.py` (+ `api.py`, `routes/__init__.py`), `services/sms_agent_sandbox_setup.py` (booked visit + open review), frontend `../rpt_frontend` page; real-model scenario run: workspace `test/sms-agent-eval-2026-11-02/` (`run_eval.py`, `scenarios.json`; the AI patient gpt-5.4-mini is costed apart, `patient_sim`; each reply gets the earlier replies' actions as `prior_actions` like the tab; harness-only flags `force_booking_failure`, `slot_gone`, `slot_taken_at_booking` (after the card, its slot is marked booked in the local DB so `book_visit` gets TAKEN: book-09), expect `booked_clinic` (np-03: no pelvic therapist at Dana Point, so the patient picks Laguna Niguel); x-04's AI patient has a first name in its persona (Dana) while the lead's first name on file stays empty (round 3: with none it answered with its last name)) |

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
    kind: str                        # 'find_times' | 'list_therapists' | 'confirm_visit' | 'book' | 'report' | 'hand_off' | 'emergency' | 'fixed_text'
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
now, Front desk phone, Booking link, Person texting, Name on file, Birthday on file, Clinic, Service,
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
the reply still says "Searched tomorrow". Other `when` values and other codes are left as they were.
The model's final answer `NO_REPLY` (or a copy like "(empty answer)") = no text, but only when the code
guards below allow it.

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
| `hand_off` | `reason` (insurance, different_case, wants_person, other_language, unknown_question, booking_problem, other), `notes` | opens a Needs Review card (new code `sms_front_desk`, group call, source `text`, choices Front desk will handle / Continue cadence); no second card when this job already opened one for the same thing (`different_case` after `report_outcome case_change`, `booking_problem` after a failed booking); reply = `sms_agent.front_desk` (or `english_only`) word for word. insurance, wants_person, other_language, unknown_question and other do not stop booking (Rules) | "Would open a Needs Review card: ..." |
| `flag_emergency` | — | reply = the fixed emergency text (code, not editable): 911, and 988 (Suicide and Crisis Lifeline) for thoughts of harming themselves; + Needs Review card (`sms_front_desk`, reason emergency: stops booking) | "Would flag an emergency" |
| `send_intro` | — | reply = `sms_agent.intro` (`new_person_intro` for a new person) word for word | same |

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
book_visit asks for the first name once, then book_visit again.

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
`SMS_AGENT_DAILY_REPLY_CAP` AI replies today -> nothing + one Needs Review card · media only (MMS) ->
`text_only` text · otherwise the agent. Declined / closed / link sent / cadence finished leads get the
agent (owner). A patient's text does not pause the cadence yet (Emma decides; build it as a setting later).

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
- Emergency text (owner, 2026-11-02): "If this is a medical emergency, please call 911 right away. If you're
  thinking about harming yourself, call or text 988 any time. Our front desk team will also reach out to you."
  (`sms_agent_texts.EMERGENCY_TEXT`, in code, never editable).
- Never log message bodies, names, phone numbers or dates of birth (observability.py redaction); actions
  store tool + result code only.
- Never send a text from the sandbox; never write to the database from the sandbox.
- Unit tests fake the OpenAI client and the connection; database tests use a local throwaway Postgres
  (pgserver, see `scripts/check_hangup_rule.py`), never the `.env` database.

## Open (owner decisions and follow-ups, 2026-11-02)

- Done 2026-11-02 (owner): Elliot's own front-desk card for insurance, parking, "call me" and the like no
  longer blocks booking by text (Rules).
- Identity check by text names the month and day on file ("is your birthday March 14?"), like voice. Owner:
  or ask for the date of birth and compare in code.
- Done 2026-11-02 (owner): the emergency text also gives 988 (Rules).
- `book_visit` could take only name and date of birth (code reads the rest from the card).
- Reasoning effort / verbosity are not set on the model call; measure in the test chat (35 s overall,
  20 s per call with one retry on a timeout, up to 5 rounds). When set, only for reasoning models (`is_reasoning_model`).
- A tool ERROR (a database error in a tool) is logged only; no Needs Review card yet.
- No "Intro sent" Context line: history holds the last 20 texts only.
- Holidays are written in the prompt (to Dec 2027) and also in `practice_settings.holidays`.
- The test chat looks the practice up by slug (`rausch-pt`); fine with one practice.
