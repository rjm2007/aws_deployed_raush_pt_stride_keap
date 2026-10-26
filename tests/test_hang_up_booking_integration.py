"""A patient's "yes" to Elliot's booking confirmation books the visit, even after a hang-up.

Owner's rule (2026-10-27): yes to "So that's ... Shall I book it?" -> booked, even if the patient
hangs up before the book_appointment tool ran (services/hang_up_booking.py, from the end-of-call
report). No yes -> not booked, the cadence continues, never "declined" unless the patient said no.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied). Stride is fake.
"""

# ruff: noqa: F811 - `env` is the shared booking fixture, injected by name

import pytest
from test_slot_booking_integration import (  # noqa: F401 - the shared booking fixture
    DAY1,
    DAY2,
    env,
    open_days,
)

from rpt_agent.services import slot_booking
from rpt_agent.services.delivery import process_vapi_end_report

pytestmark = pytest.mark.integration

CONFIRM = ("So that's Tuesday the sixteenth at nine in the morning with Thao at our Laguna Niguel "
           "office. Shall I book it?")
OPENING = [
    ("bot", "Hi, am I speaking with Rpt Tester?"),
    ("user", "Yes."),
    ("bot", ("Hi Rpt, it's Elliot from Rausch Physical Therapy. We got your referral, and I'd love "
            "to get your first visit on the calendar. Do you have a moment?")),
    ("user", "Sure."),
    ("bot", ("So this is for your knee visit, at our Laguna Niguel office. Is that location good "
            "for you?")),
    ("user", "Yes."),
    ("bot", "And do mornings or afternoons usually work better for you?"),
    ("user", "Mornings."),
    ("tool", "find_slots"),
    ("bot", "Tuesday the sixteenth I've got nine or ten with Thao. Would either of those work?"),
    ("user", "Nine works."),
]
BIRTHDAY_YES = [
    ("bot", ("And just so I pull up the right file, your birthday's April twenty-sixth, nineteen "
            "ninety?")),
    ("user", "Yes."),
]


WAIT_BOOK = "Thank you. One moment, please, while I confirm your appointment."


def _messages(turns):
    items = [{"role": "system", "message": "<prompt>"}]
    for role, words in turns:
        if role == "logged":  # a tool call Vapi logged that never reached us (no result)
            items.append({"role": "tool_calls", "message": "", "toolCalls": [
                {"id": f"tc-{len(items)}", "type": "function",
                 "function": {"name": words, "arguments": "{}"}}]})
        elif role == "tool":
            items.append({"role": "tool_calls", "message": "", "toolCalls": [
                {"id": f"tc-{len(items)}", "type": "function",
                 "function": {"name": words, "arguments": "{}"}}]})
            items.append({"role": "tool_call_result", "name": words, "result": "OPENINGS ...",
                          "toolCallId": f"tc-{len(items) - 1}"})
        else:
            items.append({"role": role, "message": words})
    return items


def _report(lead, event, call_id, turns, structured=None):
    transcript = "\n".join(f"{'AI' if r == 'bot' else 'User'}: {w}" for r, w in turns
                           if r in ("bot", "user"))
    return {"message": {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "call": {"id": call_id, "assistantOverrides": {
            "variableValues": {"lead_id": lead, "outreach_event_id": str(event)}}},
        "artifact": {
            "messages": _messages(turns),
            "transcript": transcript,
            "structuredOutputs": {"so": {"result": structured or {
                "status": "declined", "summary": "The patient said they are all set."}}},
        },
    }}


@pytest.fixture
def call(env, monkeypatch):
    """One answered cadence call with times offered on it (9:00 and 10:00 on DAY1, Thao)."""
    monkeypatch.setattr(slot_booking, "_fast_providers", lambda settings: env.stride)
    open_days(env, c5981={DAY1: ["09:00:00", "10:00:00"], DAY2: ["08:00:00"]})
    env.sync()
    lead = env.lead()
    call_id = f"vapi-hangup-{lead[:8]}"
    event = env.db.execute(
        "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for,vapi_call_id) "
        "values(%s,1,'call','in_flight',now(),%s) returning id", (lead, call_id),
    ).fetchone()["id"]
    assert env.find(lead, call_id=call_id, time_of_day="morning").startswith("OPENINGS")
    return lead, event, call_id


def _state(env, lead, event):
    return {
        "lead": env.one("select status,cadence_state,needs_review from leads where id=%s", lead),
        "event": env.one("select status,outcome from outreach_events where id=%s", event),
        "appointments": env.one("select count(*) n from appointments where lead_id=%s and "
                                "state='scheduled'", lead)["n"],
        "review": env.one("select code,message,details from lead_reviews where lead_id=%s "
                          "and resolved_at is null order by id desc limit 1", lead),
        "texts": env.one("select count(*) n from notification_log where lead_id=%s and "
                         "notification_type='sms_appointment_booked'", lead)["n"],
    }


def test_yes_then_hang_up_books_once_and_a_resent_report_books_nothing_more(env, call):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yes, please.")]
    body = _report(lead, event, call_id, turns)
    assert process_vapi_end_report(env.trace, body) == "booked"
    state = _state(env, lead, event)
    assert state["lead"]["status"] == "booked" and not state["lead"]["needs_review"]
    assert state["event"] == {"status": "delivered", "outcome": "booked"}
    assert state["appointments"] == 1 and state["texts"] == 1
    appointment = next(p for name, p in env.stride.calls if name == "appointments")
    assert appointment["primary_attendee"] == 5981
    patient = next(p for name, p in env.stride.calls if name == "patients")
    assert (patient["first_name"], patient["last_name"], patient["date_of_birth"]) == \
        ("Rpt", "Tester", "1990-04-26")
    row = env.one("select a.call_id,s.local_time::text t from appointments a "
                  "join availability_slots s on s.id=a.slot_id where a.lead_id=%s", lead)
    assert row == {"call_id": call_id, "t": "09:00:00"}
    # Vapi re-sends the report (or the durable webhook is reprocessed): still one booking.
    assert process_vapi_end_report(env.trace, body) == "booked"
    assert env.stride.count("appointments") == 1
    assert _state(env, lead, event)["appointments"] == 1
    assert env.one("select count(*) n from appointments where lead_id=%s", lead)["n"] == 1


def test_hang_up_after_naming_a_time_before_the_confirmation_is_not_booked(env, call):
    lead, event, call_id = call
    # The patient picked nine; Elliot was checking the birthday when they hung up.
    turns = OPENING + BIRTHDAY_YES[:1]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "no_answer"
    state = _state(env, lead, event)
    assert state["lead"]["status"] == "in_progress" and state["lead"]["cadence_state"] == "active"
    assert not state["lead"]["needs_review"] and state["appointments"] == 0
    assert env.stride.count("appointments") == 0


def test_booked_summary_after_a_hang_up_while_choosing_is_a_hung_up_card(env, call):
    # Owner, 2026-10-27: the summary said booked, but the patient hung up after picking a time and
    # before confirming. Needs Review, in plain words; nothing booked.
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES[:1]
    process_vapi_end_report(env.trace, _report(lead, event, call_id, turns, {
        "status": "booked", "summary": "The patient chose nine on Tuesday."}))
    state = _state(env, lead, event)
    assert state["lead"]["needs_review"] and state["appointments"] == 0
    assert state["review"]["code"] == "booking_hung_up"
    assert state["review"]["message"] == ("Patient asked to book, then hung up before confirming; "
                                          "nothing was booked")
    assert env.stride.count("appointments") == 0


def test_no_to_the_confirmation_is_not_booked(env, call):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "No, wait.")]
    outcome = process_vapi_end_report(
        env.trace, _report(lead, event, call_id, turns, {"summary": "Patient hung up."}))
    assert outcome == "no_answer"
    state = _state(env, lead, event)
    assert state["lead"]["status"] == "in_progress" and state["appointments"] == 0
    assert not state["lead"]["needs_review"] and env.stride.count("appointments") == 0


def test_a_question_after_the_confirmation_is_not_a_yes(env, call):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yes, which office?")]
    process_vapi_end_report(env.trace, _report(lead, event, call_id, turns, {"summary": "x"}))
    assert _state(env, lead, event)["appointments"] == 0 and env.stride.count("patients") == 0


def test_slot_taken_at_auto_booking_goes_to_needs_review(env, call):
    lead, event, call_id = call
    env.stride.open = {5981: {DAY1: ["10:00:00"]}}  # nine is gone in Stride's live re-check
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yeah.")]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "manual"
    state = _state(env, lead, event)
    assert state["appointments"] == 0 and state["lead"]["needs_review"]
    assert state["lead"]["status"] == "needs_attention"
    assert state["event"] == {"status": "delivered", "outcome": "manual"}
    assert state["review"]["code"] == "booking_said_yes"
    assert state["review"]["message"].startswith(
        "Patient said yes to Tuesday, June 16 at 9:00 AM with Thao Nguyen and hung up")
    assert state["review"]["message"].endswith("book by hand")
    assert state["review"]["details"]["why"] == "that time was no longer free"


def test_birthday_changed_on_the_call_goes_to_review_without_booking(env, call):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES[:1] + [
        ("user", "No, it's May first."),
        ("bot", "No problem, what's the right one?"),
        ("user", "May first, nineteen ninety."),
        ("bot", CONFIRM), ("user", "Yes."),
    ]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "manual"
    state = _state(env, lead, event)
    assert state["appointments"] == 0 and state["review"]["code"] == "booking_said_yes"
    assert state["review"]["details"]["why"] == "birthday on file not confirmed on the call"
    assert env.stride.calls == [c for c in env.stride.calls if c[0] == "availability"]


def test_booking_tool_already_called_and_booked_adds_nothing(env, call):
    lead, event, call_id = call
    assert env.book(lead, call_id=call_id, date=DAY1, time="9:00 AM").startswith("BOOKED")
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yes."),
                                      ("tool", "book_appointment")]
    process_vapi_end_report(env.trace, _report(lead, event, call_id, turns, {"summary": "x"}))
    assert env.stride.count("appointments") == 1
    assert env.one("select count(*) n from appointments where lead_id=%s", lead)["n"] == 1
    state = _state(env, lead, event)
    assert state["lead"]["status"] == "booked" and not state["lead"]["needs_review"]


def test_booking_switched_off_does_nothing(env, call):
    lead, event, call_id = call
    env.db.execute("update practice_settings set stride_booking_enabled=false where practice_id=%s",
                   (env.practice_id,))
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yes.")]
    process_vapi_end_report(env.trace, _report(lead, event, call_id, turns, {"summary": "x"}))
    state = _state(env, lead, event)
    assert state["appointments"] == 0 and not state["lead"]["needs_review"]
    assert state["lead"]["status"] == "in_progress" and env.stride.count("patients") == 0


def test_demo_call_01a1795f_hang_up_while_checking_times_is_not_declined(env, call):
    # The exact demo transcript: "Bone works for me" passed the old "more than a yes" check and
    # the invented summary closed the lead as declined.
    lead, event, call_id = call
    turns = [
        ("bot", "Hi, am I speaking with Rpt Tester?"),
        ("user", "Yep."),
        ("bot", ("Hi Rpt, it's Elliot from Rausch Physical Therapy. We got your referral, and I'd "
                "love to get your first visit on the calendar. It only takes a couple of minutes. "
                "Do you have a moment?")),
        ("user", "Yeah."),
        ("bot", ("So this is for your knee visit, at our Laguna Niguel office. Is that location "
                "good for you?")),
        ("user", "Yes."),
        ("bot", "And do mornings or afternoons usually work better for you?"),
        ("user", "Bone works for me."),
        ("bot", "One moment, please, while I check our available"),
    ]
    structured = {"status": "declined", "summary": "The patient said they are all set and do not "
                  "need to schedule an appointment at this time."}
    assert process_vapi_end_report(
        env.trace, _report(lead, event, call_id, turns, structured)) == "no_answer"
    state = _state(env, lead, event)
    assert state["lead"]["status"] == "in_progress" and state["lead"]["cadence_state"] == "active"
    assert state["event"] == {"status": "delivered", "outcome": "no_answer"}


def _booked(env, lead, event):
    state = _state(env, lead, event)
    return state["lead"]["status"] == "booked" and state["appointments"] == 1 and \
        env.stride.count("appointments") == 1


@pytest.mark.parametrize("reply", ["Yes. Bye.", "Uh huh.", "Yeah, book it for me.", "Yes ma'am."])
def test_natural_yes_with_a_goodbye_books(env, call, reply):
    lead, event, call_id = call
    turns = [("user", "Hello?")] + OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", reply)]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "booked"
    assert _booked(env, lead, event)


def test_logged_tool_call_that_never_ran_still_books(env, call):
    # Real report shape (demo 11307): the tool call is logged, then the tool's waiting line as a
    # separate bot item; the patient hung up and the request never reached us (no appointment row).
    lead, event, call_id = call
    turns = [("user", "Hello?")] + OPENING + BIRTHDAY_YES + [
        ("bot", CONFIRM), ("user", "Yes."), ("logged", "book_appointment"), ("bot", WAIT_BOOK)]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "booked"
    assert _booked(env, lead, event)


def test_other_confirmation_wording_books(env, call):
    lead, event, call_id = call
    confirm = ("Perfect. That is Tuesday the sixteenth at nine in the morning with Thao at our Laguna "
               "Niguel office. Should I go ahead and book it for you?")
    turns = OPENING + BIRTHDAY_YES + [("bot", confirm), ("user", "Sure.")]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "booked"
    assert _booked(env, lead, event)


@pytest.mark.parametrize("confirm,reply,why", [
    (CONFIRM, "Yes, Tuesday at nine is fine.", "the patient's answer was not a plain yes"),
    ("So that's Tuesday the sixteenth at nine in the morning with Thao at our Laguna Niguel office",
     "Yes.", "the confirmation question was cut short"),
    ("So that's Tuesday at a time I can't parse. Shall I book it?", "Yes.",
     "could not tell which offered time was confirmed"),
])
def test_a_yes_that_cannot_book_safely_is_reviewed_never_ignored(env, call, confirm, reply, why):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", confirm), ("user", reply)]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "manual"
    state = _state(env, lead, event)
    assert state["appointments"] == 0 and env.stride.count("patients") == 0
    assert state["review"]["code"] == "booking_said_yes" and state["review"]["details"]["why"] == why
    assert state["lead"]["status"] == "needs_attention"


def test_a_bare_no_to_the_confirmation_is_not_declined(env, call):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "No.")]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "no_answer"
    state = _state(env, lead, event)
    assert state["lead"]["status"] == "in_progress" and state["lead"]["cadence_state"] == "active"
    assert state["appointments"] == 0 and not state["lead"]["needs_review"]


def test_a_booking_made_meanwhile_is_kept_without_a_card(env, call, monkeypatch):
    # Two reports at once (or the tool's own request arriving late): book_slot answers
    # ALREADY_BOOKED / IN_PROGRESS. That booking stands; no review card, the call settles booked.
    lead, event, call_id = call
    real = slot_booking.book_slot

    def racing(trace, **kwargs):
        assert real(trace, **{**kwargs, "arguments": {
            k: v for k, v in kwargs["arguments"].items() if k != "outreach_event_id"}}
        ).startswith("BOOKED")
        return "ALREADY_BOOKED: this patient already has an appointment."

    monkeypatch.setattr(slot_booking, "book_slot", racing)
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yes.")]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "booked"
    state = _state(env, lead, event)
    assert state["review"] is None and not state["lead"]["needs_review"]
    assert state["event"] == {"status": "delivered", "outcome": "booked"} and _booked(env, lead, event)


def test_last_name_from_the_call_is_kept_as_spoken(env, call):
    lead, event, call_id = call
    env.db.execute("update leads set last_name=null,full_name='Rpt' where id=%s", (lead,))
    turns = OPENING + [("bot", "And what's your last name?"), ("user", "It's O'Brien.")] + \
        BIRTHDAY_YES + [("bot", CONFIRM), ("user", "Yes.")]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "booked"
    patient = next(p for name, p in env.stride.calls if name == "patients")
    assert patient["last_name"] == "O'Brien"


@pytest.mark.parametrize("replies", [["Yes.", "Wait, actually make it eleven."], ["Yes.", "No, wait."],
                                     ["Sure, but can my wife come too?"]])
def test_a_yes_with_a_correction_or_doubt_is_reviewed_not_booked(env, call, replies):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM)] + [("user", r) for r in replies]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "manual"
    state = _state(env, lead, event)
    assert state["appointments"] == 0 and env.stride.count("patients") == 0
    assert state["review"]["code"] == "booking_said_yes"


@pytest.mark.parametrize("last_line,reply", [
    ("That's gone, but I've got Tuesday at ten with Thao open. Would that work?", "Yes."),
    (("Perfect, that's Tuesday at nine with Thao. And just so I pull up the right file, your "
      "birthday's April twenty-sixth, nineteen ninety?"), "Yes."),
    (CONFIRM, "Okay, let me check with my wife."),
])
def test_no_confirmation_or_no_yes_does_nothing(env, call, last_line, reply):
    lead, event, call_id = call
    turns = OPENING + [("bot", last_line), ("user", reply)]
    assert process_vapi_end_report(
        env.trace, _report(lead, event, call_id, turns, {"summary": "x"})) == "no_answer"
    state = _state(env, lead, event)
    assert state["review"] is None and state["appointments"] == 0
    assert state["lead"]["status"] == "in_progress" and state["lead"]["cadence_state"] == "active"


def test_no_problem_book_it_books(env, call):
    lead, event, call_id = call
    turns = OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", "No problem, book it.")]
    assert process_vapi_end_report(env.trace, _report(lead, event, call_id, turns)) == "booked"
    assert _booked(env, lead, event)
