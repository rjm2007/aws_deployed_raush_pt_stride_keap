"""SMS agent tools without a database: the booking guard, dispatch, refusals that never reach the DB,
and the new review code's card (services/lead_review.py, sms_front_desk)."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import date, datetime, time

import pytest

from rpt_agent.config import get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import lead_review
from rpt_agent.services import sms_agent_texts as texts
from rpt_agent.services.front_desk import CLINIC_TZ
from rpt_agent.services.sms_agent_context import MEDIA_ONLY, LeadView, Turn, scrub
from rpt_agent.services.sms_agent_tools import (
    LiveTools,
    SandboxTools,
    ToolResult,
    call_tool,
    card_guard,
    clear_no,
)

CARD = texts.confirm_card(date(2026, 11, 3), time(9, 0), "Alan Rome", "Laguna Niguel")


def lead(**update):
    values = {"is_new_person": False, "first_name": "Sam", "last_name": "Test",
              "date_of_birth": date(1990, 3, 14), "clinic": "Laguna Niguel",
              "service": "Physical Therapy", "referral_word": "knee", "status": "in_progress",
              "cadence_state": "active", "needs_review": False, "review_reason": "",
              "booked_visit": "", "recent_calls": []}
    values.update(update)
    return LeadView(**values)


def history(*turns):
    return [Turn(role, text, None) for role, text in turns]


ANSWERED = history(("patient", "9am tue"), ("elliot", CARD), ("patient", "YES"))


# --- the guard ------------------------------------------------------------------------------------

def test_guard_allows_the_card_that_was_answered():
    card, why = card_guard(ANSWERED, day="2026-11-03", time="9:00 AM", therapist="Alan",
                           clinic="Laguna Niguel")
    assert why == "" and card["therapist"] == "Alan Rome" and card["date"] == date(2026, 11, 3)
    # 24-hour time and the full name work too; therapist and clinic may be left out.
    assert card_guard(ANSWERED, day="2026-11-03", time="09:00", therapist="Alan Rome",
                      clinic=None)[0]
    assert card_guard(ANSWERED, day="2026-11-03", time="9 am", therapist=None, clinic=None)[0]


@pytest.mark.parametrize("turns,args,why", [
    (history(("patient", "book me tue 9")), {}, "has not sent the read-back card"),
    (history(("elliot", CARD)), {}, "has not answered"),
    (history(("elliot", CARD), ("patient", "   ")), {}, "has not answered"),
    (history(("elliot", CARD), ("patient", "yes"), ("elliot", "Great, one more thing?")), {},
     "not the read-back card"),
    (ANSWERED, {"day": "2026-11-04"}, "date"),
    (ANSWERED, {"day": "2026-11-10"}, "date"),  # same weekday, another week
    (ANSWERED, {"time": "10:00 AM"}, "time"),
    (ANSWERED, {"therapist": "Thao"}, "therapist"),
    (ANSWERED, {"clinic": "Dana Point"}, "clinic"),
])
def test_guard_refuses(turns, args, why):
    values = {"day": "2026-11-03", "time": "9:00 AM", "therapist": "Alan", "clinic": None, **args}
    card, reason = card_guard(turns, **values)
    assert card is None and why in reason


def test_guard_accepts_a_yes_to_a_card_sent_again_after_a_reaction():
    # A "Liked" tapback is not a yes: Elliot answers and sends the same card again (prompt); the
    # yes to that second card books.
    turns = history(("elliot", CARD), ("patient", 'Liked "Here’s your visit:..."'),
                    ("elliot", "Happy to!"), ("elliot", CARD), ("patient", "yes"))
    assert card_guard(turns, day="2026-11-03", time="9:00 AM", therapist="Alan", clinic=None)[0]
    # ...but not a yes to "Want me to book it?" sent after the card.
    turns = history(("elliot", CARD), ("patient", "hmm"), ("elliot", "Want me to book it?"),
                    ("patient", "yes"))
    assert card_guard(turns, day="2026-11-03", time="9:00 AM", therapist=None, clinic=None)[0] is None


def test_guard_ignores_cadence_text_after_the_card():
    turns = history(("elliot", CARD), ("cadence", "Day 2 text"), ("patient", "yes"))
    assert card_guard(turns, day="2026-11-03", time="9:00 AM", therapist=None, clinic=None)[0]


def test_book_visit_without_card_is_refused_before_any_database_access(monkeypatch):
    trace = WorkflowTrace("test", "test")
    for tools in (
        LiveTools(trace, lead_id="00000000-0000-0000-0000-000000000001", job_id=1, lead=lead(),
                  history=history(("patient", "book tue 9am"))),
        SandboxTools(trace, lead=lead(), history=history(("patient", "book tue 9am")),
                     practice_id=1, now_local=None),
    ):
        monkeypatch.setattr(tools, "_conn", lambda: pytest.fail("no DB before the guard"))
        # Only the count of earlier failed bookings reads (live: sms_agent_actions).
        monkeypatch.setattr(tools, "_earlier_failures", lambda code: 0)
        result = call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM",
                                                 "therapist": "Alan Rome", "first_name": "Sam",
                                                 "last_name": "Test",
                                                 "date_of_birth": "1990-03-14", "clinic": None})
        assert result.code == "NOT_CONFIRMED" and result.fixed_texts == []
        assert "confirm_visit" in result.output


def test_lead_in_review_is_never_booked_or_read_back():
    trace = WorkflowTrace("test", "test")
    tools = SandboxTools(trace, lead=lead(needs_review=True), history=ANSWERED, practice_id=1,
                         now_local=None)
    book = call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM"})
    card = call_tool(tools, "confirm_visit", {"date": "2026-11-03", "time": "9:00 AM"})
    assert book.code == card.code == "REVIEW_OPEN" and not book.fixed_texts


def test_front_desk_only_review_does_not_stop_booking():
    # Owner, 2026-11-02: only Elliot's own front-desk card is open (insurance, ...): the booking
    # goes on to the card guard (no card here: NOT_CONFIRMED, not REVIEW_OPEN).
    trace = WorkflowTrace("test", "test")
    view = lead(status="needs_attention", needs_review=True, front_desk_only=True)
    assert not view.booking_paused and lead(needs_review=True).booking_paused
    for tools in (
        LiveTools(trace, lead_id="00000000-0000-0000-0000-000000000001", job_id=1, lead=view,
                  history=history(("patient", "book tue 9am"))),
        SandboxTools(trace, lead=view, history=history(("patient", "book tue 9am")),
                     practice_id=1, now_local=None),
    ):
        result = call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM"})
        assert result.code == "NOT_CONFIRMED", result.output


@pytest.mark.parametrize("tool,args,paused", [
    ("hand_off", {"reason": "insurance"}, False),
    ("hand_off", {"reason": "unknown_question"}, False),
    ("hand_off", {"reason": "wants_person"}, False),
    ("hand_off", {"reason": "other_language"}, False),
    ("hand_off", {"reason": "other"}, False),
    ("hand_off", {"reason": "different_case"}, True),
    ("hand_off", {"reason": "booking_problem"}, True),
    ("flag_emergency", {}, True),
    ("report_outcome", {"status": "case_change", "notes": "spine"}, True),
])
def test_a_card_opened_in_this_reply_stops_booking_unless_front_desk_only(tool, args, paused):
    # The LeadView is from before the reply: a case change, booking problem or emergency opened now
    # stops booking at once; a front-desk card for insurance and the like does not.
    trace = WorkflowTrace("test", "test")
    tools = SandboxTools(trace, lead=lead(), history=history(("patient", "yes")), practice_id=1,
                         now_local=None)
    tools._hand_off_text = lambda reason: f"TEXT:{reason}"
    call_tool(tools, tool, args)
    result = call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM"})
    assert result.code == ("REVIEW_OPEN" if paused else "NOT_CONFIRMED")


def test_review_allows_booking_only_for_the_front_desk_card():
    from rpt_agent.services.review import review_allows_booking
    for reason in ("insurance", "wants_person", "other_language", "unknown_question", "other"):
        assert review_allows_booking("sms_front_desk", {"reason": reason})
    for reason in ("different_case", "booking_problem", "emergency", None):
        assert not review_allows_booking("sms_front_desk", {"reason": reason})
    assert not review_allows_booking("case_change_asked", {"reason": "insurance"})
    assert not review_allows_booking("booking_failed", None)


def test_sandbox_pretend_outcomes_need_no_database():
    trace = WorkflowTrace("test", "test")
    tools = SandboxTools(trace, lead=lead(), history=ANSWERED, practice_id=1, now_local=None)
    tools._hand_off_text = lambda reason: f"TEXT:{reason}"  # the Studio text is read from the DB
    handed = call_tool(tools, "hand_off", {"reason": "insurance", "notes": "Aetna"})
    assert handed.code == "HANDED_OFF" and handed.fixed_texts == ["TEXT:insurance"]
    assert handed.summary == "Would open a Needs Review card: insurance or costs"
    other = call_tool(tools, "hand_off", {"reason": "other_language"})
    assert other.fixed_texts == ["TEXT:other_language"]
    emergency = call_tool(tools, "flag_emergency", {"kind": "medical"})
    assert emergency.fixed_texts == [texts.EMERGENCY_TEXT] and emergency.code == "EMERGENCY_SENT"
    declined = call_tool(tools, "report_outcome", {"status": "declined"})
    assert declined.code == "REPORTED" and declined.summary == "Would report: declined"
    callback = call_tool(tools, "report_outcome", {"status": "callback_scheduled"})
    assert callback.code == "NOT_RECORDED"
    assert call_tool(tools, "report_outcome", {"status": "booked"}).code == "BAD_STATUS"
    assert call_tool(tools, "hand_off", {"reason": "billing"}).code == "BAD_REASON"


def test_call_tool_logs_tool_and_code_only(caplog):
    class Tools:
        def find_times(self, *, when, specific_date=None, month=None, time_of_day=None,
                       therapist=None, clinic=None):
            return ToolResult("OPENINGS: secret details", "find_times", "Looked")
    caplog.set_level(logging.INFO)
    trace = WorkflowTrace("test", "test")
    result = call_tool(Tools(), "find_times", {"when": "earliest", "therapist": "Alan Rome",
                                               "unexpected": "x"}, trace)
    assert result.code == "OPENINGS"
    logged = " ".join(str(record.__dict__) for record in caplog.records)
    assert "find_times" in logged and "OPENINGS" in logged
    assert "Alan" not in logged and "secret" not in logged


def test_result_code_is_the_first_word():
    assert ToolResult("BOOKED (pretend): x", "book", "").code == "BOOKED"
    assert ToolResult("NOT_AVAILABLE: 9:00 AM ...", "book", "").code == "NOT_AVAILABLE"
    assert ToolResult("oops", "book", "").code == "ERROR"


def test_scrub_removes_phone_numbers():
    assert "[number]" in scrub("Call her back at (949) 555-0100 please")
    assert "555" not in scrub("Reached +1 949 555 0100, left voicemail")
    assert len(scrub("x" * 500)) == 200


# --- the review card ------------------------------------------------------------------------------

def test_sms_front_desk_card_choices_and_words():
    row = {"id": 1, "code": "sms_front_desk", "details": {"reason": "insurance", "notes": "Aetna"},
           "message": "Patient texted: insurance or costs; front desk to reach out", "source": "text"}
    card = lead_review.describe(row, {"id": "x", "status": "needs_attention"})
    assert card["group"] == "call" and card["title"] == "Patient needs the front desk (by text)"
    assert [o["choice"] for o in card["options"]] == ["front_desk", "continue"]
    assert [o["label"] for o in card["options"]] == ["Front desk will handle", "Continue cadence"]
    assert "insurance or costs" in card["reason"] and "Aetna" in card["reason"]
    assert card["source_label"] == "Text message"
    assert lead_review.sheet_open_status("sms_front_desk", {}) == (
        "Needs review: patient needs the front desk (by text)")


def test_sms_front_desk_card_on_a_finished_or_booked_lead_offers_front_desk_only():
    finished = {"id": 1, "code": "sms_front_desk", "details": {"reason": "other", "finished": True}}
    assert [o["choice"] for o in lead_review.describe(
        finished, {"id": "x", "status": "declined"})["options"]] == ["front_desk"]
    booked = {"id": 1, "code": "sms_front_desk", "details": {"reason": "other"}}
    assert [o["choice"] for o in lead_review.describe(
        booked, {"id": "x", "status": "booked"})["options"]] == ["front_desk"]


def test_sms_inbound_codes_are_known():
    for code in ("sms_agent_reply_cap", "sms_agent_failed"):
        card = lead_review.describe({"id": 1, "code": code, "details": {}}, {"id": "x"})
        assert card["code"] == code and card["title"] != lead_review.TITLES["other"]


def test_booking_failed_by_text_says_by_text():
    row = {"id": 1, "code": "booking_failed",
           "details": {"on_call": True, "by_text": True, "why": "Stride error"}}
    assert "by text" in lead_review.describe(row, {"id": "x"})["reason"]


def test_sandbox_do_not_contact_says_what_was_stopped():
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=ANSWERED,
                         practice_id=1, now_local=None)
    texts_only = call_tool(tools, "report_outcome", {"status": "do_not_contact",
                                                     "notes": "stop texts only"})
    everything = call_tool(tools, "report_outcome", {"status": "do_not_contact",
                                                     "notes": "Asked for  NO contact at all."})
    assert texts_only.code == everything.code == "REPORTED"
    assert "texts stopped" in texts_only.output and "calls" not in texts_only.output
    assert "texts and calls stopped" in everything.output
    # The sandbox opts nobody out: the worker's "send the opt-out reply anyway" flag stays off.
    assert not texts_only.opted_out and not everything.opted_out


def test_live_do_not_contact_opts_out_texts_or_everything(monkeypatch):
    from rpt_agent.services import sms_agent_tools

    class Conn:
        def execute(self, sql, params=None):
            return self

        def fetchone(self):
            return {"phone_e164": "+15550001111"}

    from contextlib import contextmanager

    @contextmanager
    def fake_transaction():
        yield Conn()

    channels: list[str] = []
    monkeypatch.setattr(sms_agent_tools, "transaction", fake_transaction)
    monkeypatch.setattr(sms_agent_tools, "explicit_opt_out",
                        lambda trace, phone, channel, source: channels.append(channel))
    tools = LiveTools(WorkflowTrace("test", "test"), lead_id="lead-1", job_id=1, lead=lead(),
                      history=ANSWERED)
    result = call_tool(tools, "report_outcome", {"status": "do_not_contact",
                                                 "notes": "stop texts only"})
    assert channels == ["sms"] and result.opted_out and "texts stopped" in result.output
    channels.clear()
    result = call_tool(tools, "report_outcome", {"status": "do_not_contact",
                                                 "notes": "asked for no contact at all"})
    # Both channels off = Do Not Contact (lead_status.explicit_opt_out): calls stop too.
    assert channels == ["sms", "call"] and "texts and calls stopped" in result.output


def test_live_case_change_hand_off_does_not_open_a_second_card(monkeypatch):
    from contextlib import contextmanager

    from rpt_agent.services import sms_agent_tools

    @contextmanager
    def fake_transaction():
        yield object()

    opened: list[str] = []
    monkeypatch.setattr(sms_agent_tools, "transaction", fake_transaction)
    monkeypatch.setattr(sms_agent_tools, "report_lead_status", lambda trace, **kw: "recorded")
    monkeypatch.setattr(sms_agent_tools, "open_front_desk_card",
                        lambda conn, lead_id, reason, notes, conversation: opened.append(reason))
    tools = LiveTools(WorkflowTrace("test", "test"), lead_id="lead-1", job_id=1, lead=lead(),
                      history=ANSWERED)
    tools._hand_off_text = lambda reason: f"TEXT:{reason}"
    tools._look_back = lambda key: False  # nothing from an earlier reply
    # Without a case change report, different_case opens its card.
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).code == "HANDED_OFF"
    assert opened == ["different_case"]
    opened.clear()
    # Another reply (a second different_case hand-off in the same reply is a repeat).
    tools = LiveTools(WorkflowTrace("test", "test"), lead_id="lead-1", job_id=2, lead=lead(),
                      history=ANSWERED)
    tools._hand_off_text = lambda reason: f"TEXT:{reason}"
    tools._look_back = lambda key: False
    assert call_tool(tools, "report_outcome", {"status": "case_change", "notes": "spine"}).code == "REPORTED"
    handed = call_tool(tools, "hand_off", {"reason": "different_case"})
    # The case change card (lead_status) is the one card; the patient still gets the front desk text.
    assert handed.fixed_texts == ["TEXT:different_case"] and opened == []


# --- the guard: a photo or a clear no is not a yes (2026-11-02 review) ----------------------------

@pytest.mark.parametrize("replies", [
    [MEDIA_ONLY],
    ["hmm not sure, maybe later"],
    ["no"],
    ["Wait"],
    ["can we change the time?"],
    ["cancel that"],
    [MEDIA_ONLY, "No."],
    ["Nope, another day"],
    ["nah"],
])
def test_guard_refuses_a_photo_or_a_clear_no(replies):
    turns = history(("elliot", CARD), *[("patient", reply) for reply in replies])
    card, why = card_guard(turns, day="2026-11-03", time="9:00 AM", therapist=None, clinic=None)
    assert card is None and why


@pytest.mark.parametrize("replies", [
    ["yes"], ["Yes, no problem!"], ["yep book it"], ["Perfect, not a problem"],
    [MEDIA_ONLY, "yes"],
    # Two texts: the model judges ("wait" then a yes), the code does not guess.
    ["wait", "ok yes book it"],
])
def test_guard_lets_the_model_judge_other_answers(replies):
    turns = history(("elliot", CARD), *[("patient", reply) for reply in replies])
    assert card_guard(turns, day="2026-11-03", time="9:00 AM", therapist=None, clinic=None)[0]


def test_clear_no_words_are_whole_words():
    assert clear_no("No thanks") and clear_no("not now") and clear_no("later pls")
    assert not clear_no("yes") and not clear_no("Nothing better, book it")
    assert not clear_no("Notice: yes") and not clear_no("no worries, yes")


def test_book_visit_after_a_photo_is_refused_live_and_sandbox(monkeypatch):
    trace = WorkflowTrace("test", "test")
    turns = history(("elliot", CARD), ("patient", MEDIA_ONLY))
    for tools in (
        LiveTools(trace, lead_id="00000000-0000-0000-0000-000000000001", job_id=1, lead=lead(),
                  history=turns),
        SandboxTools(trace, lead=lead(), history=turns, practice_id=1, now_local=None),
    ):
        monkeypatch.setattr(tools, "_conn", lambda: pytest.fail("no DB before the guard"))
        # Only the count of earlier failed bookings reads (live: sms_agent_actions).
        monkeypatch.setattr(tools, "_earlier_failures", lambda code: 0)
        result = call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM"})
        assert result.code == "NOT_CONFIRMED" and "in words" in result.output


# --- tool errors and side panel codes (2026-11-02 review) -----------------------------------------

def test_any_tool_exception_is_an_error_reply_not_a_crash(caplog):
    class Broken(SandboxTools):
        def hand_off(self, *, reason, notes=None):
            raise RuntimeError("db gone")

    trace = WorkflowTrace("test", "test")
    tools = Broken(trace, lead=lead(), history=[], practice_id=1, now_local=None)
    result = call_tool(tools, "hand_off", {"reason": "insurance"}, trace)
    assert result.code == "ERROR" and result.kind == "hand_off" and not result.fixed_texts


def test_action_codes_say_which_outcome_and_whether_booking_stops():
    trace = WorkflowTrace("test", "test")
    tools = SandboxTools(trace, lead=lead(), history=[], practice_id=1, now_local=None)
    tools._hand_off_text = lambda reason: "TEXT"
    case = call_tool(tools, "report_outcome", {"status": "case_change", "notes": "spine"})
    assert case.code == "REPORTED" and case.action_code == "REPORTED_CASE_CHANGE"
    assert call_tool(tools, "report_outcome",
                     {"status": "declined"}).action_code == "REPORTED_DECLINED"
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).action_code == \
        "HANDED_OFF_PAUSED"
    assert call_tool(tools, "hand_off", {"reason": "insurance"}).action_code == "HANDED_OFF"


def test_sandbox_hand_off_reason_is_compared_like_live():
    # x-02: with the reason sent back by the tab, only the same reason is a repeat; an older tab's
    # kind-only key still counts every reason of that kind.
    trace = WorkflowTrace("test", "test")

    def tools(earlier):
        made = SandboxTools(trace, lead=lead(), history=[], practice_id=1, now_local=None,
                            earlier=earlier)
        made._hand_off_text = lambda reason: "TEXT"
        return made
    insurance = call_tool(tools({"hand_off:insurance"}), "hand_off", {"reason": "insurance"})
    assert insurance.code == "ALREADY_HANDED_OFF" and insurance.reason == "insurance"
    person = call_tool(tools({"hand_off:insurance"}), "hand_off", {"reason": "wants_person"})
    assert person.code == "HANDED_OFF" and person.reason == "wants_person"
    assert call_tool(tools({"hand_off:front_desk"}), "hand_off",
                     {"reason": "wants_person"}).code == "ALREADY_HANDED_OFF"


def test_live_booked_text_survives_a_database_error_after_the_booking(monkeypatch):
    # The visit was made in Stride, then reading the Studio "booked" text failed: call_tool turned
    # it into "ERROR: nothing was changed" and the patient never got the booked text.
    from contextlib import contextmanager

    from rpt_agent.services import slot_booking

    monkeypatch.setattr(slot_booking, "book_slot", lambda *a, **k: "BOOKED: done")

    @contextmanager
    def broken():
        raise RuntimeError("connection lost")
        yield  # pragma: no cover

    trace = WorkflowTrace("test", "test")
    tools = LiveTools(trace, lead_id="00000000-0000-0000-0000-000000000001", job_id=1,
                      lead=lead(), history=ANSWERED)
    monkeypatch.setattr(tools, "_conn", broken)
    result = call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM"}, trace)
    assert result.code == "BOOKED"
    assert len(result.fixed_texts) == 1
    assert result.fixed_texts[0].startswith("You're all set, Sam!")
    assert "Alan Rome" in result.fixed_texts[0]


# --- repeats: no second card, no second fixed text (gpt-5.5 eval, 2026-11-02) ----------------------

def sandbox(earlier=None, **lead_update):
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(**lead_update), history=[],
                         practice_id=1, now_local=None, earlier=earlier)
    tools._hand_off_text = lambda reason: f"TEXT:{reason}"
    return tools


def test_sandbox_same_hand_off_twice_sends_the_text_once():
    tools = sandbox()
    first = call_tool(tools, "hand_off", {"reason": "insurance"})
    again = call_tool(tools, "hand_off", {"reason": "insurance"})
    assert first.fixed_texts == ["TEXT:insurance"]
    assert again.code == "ALREADY_HANDED_OFF" and again.fixed_texts == []
    assert again.action_code == "ALREADY_HANDED_OFF" and "NO_REPLY" in again.output


def test_sandbox_hand_off_done_earlier_in_the_chat_is_not_repeated():
    # ho-01..06: the same front desk text went out on every later turn.
    tools = sandbox({"hand_off:front_desk"})
    assert call_tool(tools, "hand_off", {"reason": "insurance"}).code == "ALREADY_HANDED_OFF"
    assert not tools.lead.booking_paused and not tools.paused_by_hand_off
    # A hand-off of the other kind (booking stops) is new.
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).fixed_texts == [
        "TEXT:different_case"]


def test_sandbox_case_change_and_emergency_earlier_are_not_repeated():
    # x-02: case_change + different_case hand-off on every turn.
    tools = sandbox({"case_change"})
    case = call_tool(tools, "report_outcome", {"status": "case_change", "notes": "shoulder"})
    assert case.code == "ALREADY_REPORTED" and case.action_code == "ALREADY_REPORTED"
    handed = call_tool(tools, "hand_off", {"reason": "different_case"})
    assert handed.code == "ALREADY_HANDED_OFF" and not handed.fixed_texts
    assert tools.paused_by_hand_off  # booking still stops
    tools = sandbox({"emergency"})
    emergency = call_tool(tools, "flag_emergency", {})
    assert emergency.code == "ALREADY_SENT" and emergency.fixed_texts == []
    assert call_tool(tools, "book_visit", {"date": "2026-11-03", "time": "9:00 AM"}).code == \
        "REVIEW_OPEN"


def test_sandbox_case_change_then_different_case_in_one_reply_still_sends_the_text():
    tools = sandbox()
    assert call_tool(tools, "report_outcome", {"status": "case_change"}).code == "REPORTED"
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).fixed_texts == [
        "TEXT:different_case"]


def test_live_repeats_open_no_card_and_send_no_text(monkeypatch):
    from contextlib import contextmanager

    from rpt_agent.services import sms_agent_tools

    @contextmanager
    def fake_transaction():
        yield object()

    opened: list[str] = []
    reported: list[str] = []
    monkeypatch.setattr(sms_agent_tools, "transaction", fake_transaction)
    monkeypatch.setattr(sms_agent_tools, "report_lead_status",
                        lambda trace, **kw: reported.append(kw["status"]) or "recorded")
    monkeypatch.setattr(sms_agent_tools, "open_front_desk_card",
                        lambda conn, lead_id, reason, notes, conversation: opened.append(reason))
    tools = LiveTools(WorkflowTrace("test", "test"), lead_id="lead-1", job_id=2, lead=lead(),
                      history=ANSWERED)
    tools._hand_off_text = lambda reason: f"TEXT:{reason}"
    earlier = {"hand_off:insurance", "case_change", "emergency"}
    looked: list[str] = []
    tools._look_back = lambda key: looked.append(key) or key in earlier
    assert call_tool(tools, "hand_off", {"reason": "insurance"}).code == "ALREADY_HANDED_OFF"
    assert call_tool(tools, "report_outcome", {"status": "case_change"}).code == "ALREADY_REPORTED"
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).code == "ALREADY_HANDED_OFF"
    assert call_tool(tools, "flag_emergency", {}).code == "ALREADY_SENT"
    assert opened == [] and reported == []
    # Another reason is a new card, with its text.
    assert call_tool(tools, "hand_off", {"reason": "wants_person"}).fixed_texts == [
        "TEXT:wants_person"]
    assert opened == ["wants_person"]
    # Each look-up once per reply.
    assert len(looked) == len(set(looked))


def test_live_look_back_reads_lead_reviews_without_this_jobs_own_cards(monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta

    seen = []

    class Conn:
        def execute(self, sql, params):
            seen.append((sql, params))

            class Rows:
                def fetchone(self):
                    return {"found": True}
            return Rows()

    @contextmanager
    def conn():
        yield Conn()

    tools = LiveTools(WorkflowTrace("test", "test"), lead_id="lead-1", job_id=7, lead=lead(),
                      history=ANSWERED)
    monkeypatch.setattr(tools, "_conn", conn)
    assert tools._earlier("hand_off:insurance") and tools._earlier("emergency")
    assert tools._earlier("case_change")
    (sql, hand), (_, emergency), (_, case) = seen
    assert "lead_reviews" in sql and "details->>'conversation'" in sql
    assert hand == ("lead-1", "sms_front_desk", "insurance", "insurance", "sms:lead-1:7",
                    timedelta(hours=24), True)
    # An emergency counts for an hour only, open or not.
    assert emergency[2] == "emergency" and emergency[5:] == (timedelta(hours=1), False)
    assert case[1] == "case_change_asked" and case[2] is None


# --- service and therapist names (gpt-5.5 eval, 2026-11-02) ---------------------------------------

class FakeContext:
    def __init__(self, service="Physical Therapy", names=("Alan Brooks", "Thao Nguyen")):
        self.case_type = {"name": service}
        self.location = {"name": "Laguna Niguel"}
        self.clinicians = [{"display_name": name} for name in names]
        self.lead = {"practice_id": 1}


def test_resolve_therapist_fixes_a_wrong_surname_only_when_unambiguous():
    from rpt_agent.services.sms_agent_tools import closest_therapist, resolve_therapist

    context = FakeContext()
    assert resolve_therapist(context, "Alan Rome") == "Alan Brooks"
    assert resolve_therapist(context, "Thao") == "Thao"  # matches as given
    assert resolve_therapist(context, None) is None
    with pytest.raises(ValueError, match="NO_SUCH_THERAPIST.*Alan Brooks, Thao Nguyen"):
        resolve_therapist(context, "Maria Lopez")
    two = FakeContext(names=("Alan Brooks", "Alan Rome", "Thao Nguyen"))
    assert resolve_therapist(two, "Alan Rome") == "Alan Rome"
    with pytest.raises(ValueError, match="WHICH_THERAPIST"):
        resolve_therapist(two, "Alan Smith")
    assert closest_therapist(two, "Alan Smith") == "Alan Smith"  # the search lists them itself


def test_guard_accepts_the_cards_therapist_by_a_wrong_surname():
    turns = history(("elliot", CARD), ("patient", "yes"))
    assert card_guard(turns, day="2026-11-03", time="9:00 AM", therapist="Alan Brooks",
                      clinic=None)[0]["therapist"] == "Alan Rome"
    assert card_guard(turns, day="2026-11-03", time="9:00 AM", therapist="Thao", clinic=None)[0] \
        is None


def test_another_service_for_a_referral_is_a_case_change_but_a_new_person_may_name_one(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def no_conn():
        yield None

    for who, code in ((lead(), "CASE_CHANGE"), (lead(is_new_person=True, service=""), None)):
        tools = SandboxTools(WorkflowTrace("test", "test"), lead=who, history=[], practice_id=1,
                             now_local=None)
        asked: list = []

        def context(conn, clinic, service=None, asked=asked):
            asked.append(service)
            return FakeContext(service="Pelvic Health")
        monkeypatch.setattr(tools, "_conn", no_conn)
        monkeypatch.setattr(tools, "_context", context)
        monkeypatch.setattr(slot_booking_module(), "find_slots_readonly",
                            lambda trace, conn, context, **kw: f"OPENINGS for {context.case_type['name']}")
        result = call_tool(tools, "find_times", {"when": "earliest", "service": "pelvic"})
        if code:
            assert result.code == code and "Physical Therapy" in result.output
        else:
            assert result.output == "OPENINGS for Pelvic Health" and asked == ["pelvic"]


def test_same_service_for_a_referral_is_fine(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def no_conn():
        yield None

    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=[], practice_id=1,
                         now_local=None)
    monkeypatch.setattr(tools, "_conn", no_conn)
    monkeypatch.setattr(tools, "_context", lambda conn, clinic, service=None: FakeContext())
    monkeypatch.setattr(slot_booking_module(), "find_slots_readonly",
                        lambda trace, conn, context, **kw: "OPENINGS")
    assert call_tool(tools, "find_times", {"when": "earliest", "service": "PT"}).code == "OPENINGS"


def slot_booking_module():
    from rpt_agent.services import slot_booking

    return slot_booking


# --- round 2 (gpt-5.5 eval, 2026-11-02) -------------------------------------------------------------

@pytest.mark.parametrize("latest,when,expected", [
    ("yo can i get in tmrw aftrnoon at lagna nigel", "today", "tomorrow"),
    ("TOMORROW?", "today", "tomorrow"),
    ("tmr pls", "today", "tomorrow"),
    ("tmw morning", "today", "tomorrow"),
    ("tomoro after 3", "today", "tomorrow"),
    ("2morrow?", "today", "tomorrow"),
    ("today or tomorrow", "today", "today"),
    ("anything today?", "today", "today"),
    ("tmrw", "this_week", "this_week"),
    ("tmrwx", "today", "today"),
])
def test_search_when_reads_tomorrow_from_the_patients_latest_text(latest, when, expected):
    from rpt_agent.services.sms_agent_tools import search_when

    turns = history(("patient", "today?"), ("elliot", "Sure"), ("patient", latest))
    assert search_when(when, turns) == expected


def test_find_times_today_after_tmrw_searches_tomorrow_live_and_sandbox(monkeypatch):
    # book-12: "tmrw aftrnoon" was searched as when="today".
    from contextlib import contextmanager

    @contextmanager
    def no_conn():
        yield None

    turns = history(("patient", "yo can i get in tmrw aftrnoon"))
    asked: list = []
    module = slot_booking_module()
    monkeypatch.setattr(module, "find_slots_readonly",
                        lambda trace, conn, context, **kw: asked.append(kw["when"]) or "OPENINGS")
    monkeypatch.setattr(module, "find_slots", lambda trace, **kw: asked.append(kw["when"]) or "OPENINGS")
    sandbox_tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=turns,
                                 practice_id=1, now_local=None)
    monkeypatch.setattr(sandbox_tools, "_conn", no_conn)
    monkeypatch.setattr(sandbox_tools, "_context", lambda conn, clinic, service=None: FakeContext())
    live = LiveTools(WorkflowTrace("test", "test"), lead_id="00000000-0000-0000-0000-000000000001",
                     job_id=1, lead=lead(), history=turns)
    for tools in (sandbox_tools, live):
        result = call_tool(tools, "find_times", {"when": "today", "time_of_day": "afternoon"})
        assert result.code == "OPENINGS" and result.summary.startswith("Looked for times (tomorrow)")
    assert asked == ["tomorrow", "tomorrow"]


NO_FIRST = {"first_name": ""}
BOOK_ARGS = {"date": "2026-11-03", "time": "9:00 AM", "therapist": "Alan"}


def test_booking_names_use_the_names_on_file_or_ones_the_patient_wrote():
    from rpt_agent.services.sms_agent_tools import booking_names, greeting_name

    # On file wins over whatever the model passes.
    assert booking_names(lead(), ANSWERED, "Bob", "Smith") == ("Sam", "Test")
    no_first = lead(first_name="", last_name="Quill")
    # x-04: the model passed the last name as the first name.
    with pytest.raises(ValueError, match="^MISSING_NAME: .*first name"):
        booking_names(no_first, ANSWERED, "Quill", "Quill")
    # A first name the patient never wrote.
    with pytest.raises(ValueError, match="MISSING_NAME"):
        booking_names(no_first, ANSWERED, "Jordan", None)
    with pytest.raises(ValueError, match="MISSING_NAME"):
        booking_names(no_first, ANSWERED, None, None)
    said = history(("patient", "it's jordan btw"), ("elliot", CARD), ("patient", "yes"))
    assert booking_names(no_first, said, "Jordan", "Other") == ("Jordan", "Quill")
    # Last name written by the patient but on file: still the last name.
    said_last = history(("patient", "quill here"), ("elliot", CARD), ("patient", "yes"))
    with pytest.raises(ValueError, match="MISSING_NAME"):
        booking_names(no_first, said_last, "Quill", None)
    # A new person gives both in the chat.
    assert booking_names(lead(is_new_person=True, first_name="", last_name=""), ANSWERED,
                         "Brooke", "Tanner") == ("Brooke", "Tanner")
    # The booked text: on file, else written by the patient, else none.
    assert greeting_name(lead(), ANSWERED, "Sam") == "Sam"
    assert greeting_name(no_first, said, "Jordan") == "Jordan"
    assert greeting_name(lead(is_new_person=True, first_name=""), ANSWERED, "Brooke") == ""


def test_book_visit_without_a_first_name_is_refused_before_any_booking(monkeypatch):
    module = slot_booking_module()
    monkeypatch.setattr(module, "book_slot", lambda *a, **k: pytest.fail("never booked"))
    trace = WorkflowTrace("test", "test")
    no_first = lead(first_name="", last_name="Quill")
    for tools in (
        LiveTools(trace, lead_id="00000000-0000-0000-0000-000000000001", job_id=1, lead=no_first,
                  history=ANSWERED),
        SandboxTools(trace, lead=no_first, history=ANSWERED, practice_id=1, now_local=None),
    ):
        monkeypatch.setattr(tools, "_conn", lambda: pytest.fail("no DB without a first name"))
        # Only the count of earlier failed bookings reads (live: sms_agent_actions).
        monkeypatch.setattr(tools, "_earlier_failures", lambda code: 0)
        result = call_tool(tools, "book_visit", {**BOOK_ARGS, "first_name": "Quill",
                                                 "last_name": "Quill"})
        assert result.code == "MISSING_NAME" and not result.fixed_texts
        assert "ask for their first name" in result.output.lower()


def test_live_booking_uses_the_names_on_file_and_never_an_ungiven_name(monkeypatch):
    from contextlib import contextmanager

    booked: list = []
    module = slot_booking_module()
    monkeypatch.setattr(module, "book_slot",
                        lambda *a, **k: booked.append(k["arguments"]) or "BOOKED: done")

    @contextmanager
    def draft_only():  # the booked text from the draft in code (no Studio row)
        raise RuntimeError("no db in this test")
        yield  # pragma: no cover

    def book(view, turns, **arguments):
        tools = LiveTools(WorkflowTrace("test", "test"),
                          lead_id="00000000-0000-0000-0000-000000000001", job_id=1, lead=view,
                          history=turns)
        monkeypatch.setattr(tools, "_conn", draft_only)
        return call_tool(tools, "book_visit", {**BOOK_ARGS, **arguments})

    result = book(lead(), ANSWERED, first_name="Robert", last_name="Smith")
    assert result.code == "BOOKED" and result.fixed_texts[0].startswith("You're all set, Sam!")
    assert (booked[-1]["first_name"], booked[-1]["last_name"]) == ("Sam", "Test")
    said = history(("patient", "Jordan here"), ("elliot", CARD), ("patient", "yes"))
    result = book(lead(first_name="", last_name="Quill"), said, first_name="Jordan")
    assert result.fixed_texts[0].startswith("You're all set, Jordan!")
    assert (booked[-1]["first_name"], booked[-1]["last_name"]) == ("Jordan", "Quill")


def test_sandbox_booked_text_greets_only_a_name_the_patient_gave(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def no_conn():
        yield None

    module = slot_booking_module()
    monkeypatch.setattr(module, "open_slot", lambda *a, **k: {"ok": True})
    new = lead(is_new_person=True, first_name="", last_name="")
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=new, history=ANSWERED,
                         practice_id=1, now_local=None)
    monkeypatch.setattr(tools, "_conn", no_conn)
    monkeypatch.setattr(tools, "_context", lambda conn, clinic, service=None: type(
        "C", (), {"now_local": __import__("datetime").datetime(2026, 11, 2, 9, 0)})())
    monkeypatch.setattr(tools, "_render", lambda conn, key, **values: texts.render(
        None, key, **values))
    result = call_tool(tools, "book_visit", {**BOOK_ARGS, "first_name": "Brooke",
                                             "last_name": "Tanner", "date_of_birth": "1993-02-09"})
    assert result.code == "BOOKED" and result.fixed_texts[0].startswith("You're all set! ")


@pytest.mark.parametrize("clinics,expected", [
    (["Dana Point", "Laguna Niguel", "Mission Viejo"],
     ("NO_CLINICIANS: no Physical Therapy - Pelvic therapist at Dana Point. Offered at: Laguna "
      "Niguel, Mission Viejo. Ask if one of those works.")),
    (["Dana Point"], ("NO_CLINICIANS: no Physical Therapy - Pelvic therapist at Dana Point, and no "
                      "other clinic offers it. Offer to text the booking link or hand off to the "
                      "front desk.")),
])
def test_offered_elsewhere_names_the_clinics_that_offer_the_service(clinics, expected):
    from rpt_agent.services.sms_agent_tools import offered_elsewhere

    message = ("NO_CLINICIANS: no therapist is set up for Physical Therapy - Pelvic at Dana Point. "
               "Offer to text the booking link or transfer to the team.")
    assert offered_elsewhere(message, clinics) == expected
    assert offered_elsewhere("NO_CLINICIANS: something else", clinics) == \
        "NO_CLINICIANS: something else"


def test_find_times_no_clinicians_lists_the_other_clinics_live_and_sandbox(monkeypatch):
    # np-03: pelvic at Dana Point had no therapist; the reply now says where it is offered.
    from contextlib import contextmanager

    message = ("NO_CLINICIANS: no therapist is set up for Physical Therapy - Pelvic at Dana Point. "
               "Offer to text the booking link or transfer to the team.")
    queries: list = []

    class Conn:
        def execute(self, sql, params=None):
            queries.append(params)

            class Rows:
                def fetchall(self):
                    return [{"name": "Laguna Niguel"}, {"name": "Mission Viejo"}]
            return Rows()

    @contextmanager
    def conn():
        yield Conn()

    def no_therapist(*args, **kwargs):
        raise ValueError(message)

    module = slot_booking_module()
    monkeypatch.setattr(module, "find_slots", no_therapist)
    new = lead(is_new_person=True, service="", clinic="Dana Point")
    sandbox_tools = SandboxTools(WorkflowTrace("test", "test"), lead=new, history=[],
                                 practice_id=7, now_local=None)
    monkeypatch.setattr(sandbox_tools, "_context", no_therapist)
    live = LiveTools(WorkflowTrace("test", "test"), lead_id="00000000-0000-0000-0000-000000000001",
                     job_id=1, lead=lead(clinic="Dana Point"), history=[], practice_id=7)
    for tools in (sandbox_tools, live):
        monkeypatch.setattr(tools, "_conn", conn)
        result = call_tool(tools, "find_times", {"when": "earliest", "clinic": "Dana Point"})
        assert result.code == "NO_CLINICIANS" and result.kind == "find_times"
        assert result.output == ("NO_CLINICIANS: no Physical Therapy - Pelvic therapist at Dana "
                                 "Point. Offered at: Laguna Niguel, Mission Viejo. Ask if one of "
                                 "those works.")
    assert queries == [{"practice": 7, "service": "Physical Therapy - Pelvic"}] * 2
    # Other ValueErrors are untouched.
    monkeypatch.setattr(sandbox_tools, "_context",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("UNKNOWN_LOCATION: x")))
    assert call_tool(sandbox_tools, "find_times", {"when": "earliest"}).code == "UNKNOWN_LOCATION"


# --- round 3 (gpt-5.5 eval, 2026-11-02) --------------------------------------------------------------

CALENDAR = ("Calendar today: Wednesday, November 4, 2026. Last bookable day: Monday, February 1, "
            "2027; searches without a named date or month look up to Friday, December 4.")
NOTHING = ("NOTHING_IN_RANGE: no Physical Therapy at Laguna Niguel openings for that time. Next "
           "openings: (1) Friday, November 6 (2026-11-06) at 1:00 PM with Alan Brooks. Offer these "
           f"times. {CALENDAR}")
OPENINGS = ("OPENINGS for Physical Therapy at Laguna Niguel: (1) Thursday, November 5 (2026-11-05) "
            f"at 1:00 PM with Alan Brooks. Offer these times. {CALENDAR}")


def test_a_search_moved_to_tomorrow_names_the_day_first():
    # book-12: "tmrw aftrnoon" searched as today became tomorrow, but the reply did not say so and
    # the model texted "nothing this afternoon".
    from rpt_agent.services.sms_agent_tools import name_search_day

    named = name_search_day(NOTHING, asked="today", searched="tomorrow")
    assert named.startswith("NOTHING_IN_RANGE: Searched tomorrow, Thu Nov 5 (the patient said "
                            "tomorrow). Name that day in your text (\"Thu Nov 5\"); never \"today\"")
    assert named.endswith(NOTHING[len("NOTHING_IN_RANGE: "):])
    assert ToolResult(named, "find_times", "x").code == "NOTHING_IN_RANGE"
    openings = name_search_day(OPENINGS, asked="today", searched="tomorrow")
    assert openings.startswith("OPENINGS: Searched tomorrow, Thu Nov 5 (the patient said tomorrow).")
    assert "Openings for Physical Therapy at Laguna Niguel: (1) Thursday, November 5" in openings
    assert ToolResult(openings, "find_times", "x").code == "OPENINGS"


@pytest.mark.parametrize("asked,searched,start", [
    # Every today / tomorrow search names its day, moved or not.
    ("today", "today", ("NOTHING_IN_RANGE: Searched today, Wed Nov 4. Name that day in your text "
                        "(\"Wed Nov 4\"). no Physical")),
    ("tomorrow", "tomorrow", "NOTHING_IN_RANGE: Searched tomorrow, Thu Nov 5. Name that day"),
])
def test_today_and_tomorrow_searches_always_name_the_day(asked, searched, start):
    from rpt_agent.services.sms_agent_tools import name_search_day

    assert name_search_day(NOTHING, asked=asked, searched=searched).startswith(start)


@pytest.mark.parametrize("output,searched", [
    (NOTHING, "earliest"),                         # not a one-day search
    (NOTHING, "specific_date"),
    ("NO_OPENINGS: nothing open at all.", "tomorrow"),   # other codes are left alone
    ("NEED_LOCATION: which clinic?", "today"),
])
def test_other_searches_and_codes_are_unchanged(output, searched):
    from rpt_agent.services.sms_agent_tools import name_search_day

    assert name_search_day(output, asked=searched, searched=searched) == output


def test_a_pinned_tomorrow_search_names_the_day_it_searched():
    # Emma 2026-11-12: on the demo's pinned calendar tomorrow keeps its real weekday (Fri Jul 17),
    # not the day after the pinned Wed Jul 15.
    from rpt_agent.services.sms_agent_tools import name_search_day

    output = ("NOTHING_IN_RANGE: no Physical Therapy at Dana Point openings (on the booking calendar: "
              "Friday, July 17, 2026) for that time. Calendar today: Wednesday, July 15, 2026.")
    assert name_search_day(output, asked="tomorrow", searched="tomorrow").startswith(
        "NOTHING_IN_RANGE: Searched tomorrow, Fri Jul 17. Name that day")


def test_a_reply_without_the_calendar_note_still_says_what_was_searched():
    from rpt_agent.services.sms_agent_tools import calendar_today, name_search_day

    assert calendar_today(NOTHING) == date(2026, 11, 4)
    assert calendar_today("OPENINGS for x") is None
    assert name_search_day("OPENINGS for x: (1) ...", asked="today", searched="tomorrow") == (
        "OPENINGS: Searched tomorrow (the patient said tomorrow). Name that day in your text "
        "(\"tomorrow\"); never \"today\" or \"this afternoon\" for it. Openings for x: (1) ...")


def test_find_times_names_the_moved_day_live_and_sandbox(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def no_conn():
        yield None

    turns = history(("patient", "yo can i get in tmrw aftrnoon at lagna nigel"))
    module = slot_booking_module()
    monkeypatch.setattr(module, "find_slots_readonly", lambda trace, conn, context, **kw: NOTHING)
    monkeypatch.setattr(module, "find_slots", lambda trace, **kw: NOTHING)
    sandbox_tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=turns,
                                 practice_id=1, now_local=None)
    monkeypatch.setattr(sandbox_tools, "_conn", no_conn)
    monkeypatch.setattr(sandbox_tools, "_context", lambda conn, clinic, service=None: FakeContext())
    live = LiveTools(WorkflowTrace("test", "test"), lead_id="00000000-0000-0000-0000-000000000001",
                     job_id=1, lead=lead(), history=turns)
    for tools in (sandbox_tools, live):
        result = call_tool(tools, "find_times", {"when": "today", "time_of_day": "afternoon"})
        assert result.code == "NOTHING_IN_RANGE"
        assert result.output.startswith("NOTHING_IN_RANGE: Searched tomorrow, Thu Nov 5 (the "
                                        "patient said tomorrow).")


def test_missing_name_asks_only_for_the_first_name_and_sends_the_card_again():
    # x-04 (round 3): the model also asked for the last name, then promised the front desk.
    from rpt_agent.services.sms_agent_tools import MISSING_FIRST_NAME

    assert "never ask for it" in MISSING_FIRST_NAME
    assert "call confirm_visit again for the same time in that turn" in MISSING_FIRST_NAME


# --- eval 2026-11-03 (results-gpt-5.5-dob.json) -------------------------------------------------------

TODAY_NOV4 = date(2026, 11, 4)
NOW_NOV4 = datetime(2026, 11, 4, 10, 30, tzinfo=CLINIC_TZ)


@pytest.mark.parametrize("latest,when,specific,expected", [
    # book-12: "tmrw aftrnoon" searched as when=specific_date with today's date.
    ("yo can i get in tmrw aftrnoon at lagna nigel", "specific_date", "2026-11-04", "tomorrow"),
    ("tmrw?", "today", None, "tomorrow"),
    # Another day, or a text that also says today, is left as asked.
    ("tmrw?", "specific_date", "2026-11-05", "specific_date"),
    ("today or tmrw", "specific_date", "2026-11-04", "specific_date"),
    ("next friday", "specific_date", "2026-11-04", "specific_date"),
    ("tmrw?", "specific_date", "not a date", "specific_date"),
])
def test_search_when_a_specific_date_today_after_tmrw_is_tomorrow(latest, when, specific, expected):
    from rpt_agent.services.sms_agent_tools import search_when

    turns = history(("patient", latest))
    assert search_when(when, turns, specific, {TODAY_NOV4}) == expected


def test_find_times_specific_date_today_after_tmrw_searches_tomorrow_and_names_it(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def no_conn():
        yield None

    turns = history(("patient", "yo can i get in tmrw aftrnoon at lagna nigel"))
    asked: list = []
    module = slot_booking_module()

    def readonly(trace, conn, context, **kw):
        asked.append((kw["when"], kw["specific_date"]))
        return OPENINGS

    def live_find(trace, **kw):
        asked.append((kw["when"], kw["specific_date"]))
        return OPENINGS

    monkeypatch.setattr(module, "find_slots_readonly", readonly)
    monkeypatch.setattr(module, "find_slots", live_find)
    # The test chat's clock is the day searched; live uses the clinic's real date.
    sandbox_tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=turns,
                                 practice_id=1, now_local=NOW_NOV4)
    monkeypatch.setattr(sandbox_tools, "_conn", no_conn)
    monkeypatch.setattr(sandbox_tools, "_context", lambda conn, clinic, service=None: FakeContext())
    live = LiveTools(WorkflowTrace("test", "test"), lead_id="00000000-0000-0000-0000-000000000001",
                     job_id=1, lead=lead(), history=turns)
    monkeypatch.setattr(live, "search_todays", lambda: {TODAY_NOV4})
    for tools in (sandbox_tools, live):
        result = call_tool(tools, "find_times", {"when": "specific_date",
                                                 "specific_date": "2026-11-04",
                                                 "time_of_day": "afternoon"})
        assert result.summary.startswith("Looked for times (tomorrow)")
        assert result.output.startswith("OPENINGS: Searched tomorrow, Thu Nov 5 (the patient said "
                                        "tomorrow). Name that day in your text (\"Thu Nov 5\")")
    assert asked == [("tomorrow", None), ("tomorrow", None)]
    # Without "tmrw" the date is searched as asked.
    asked.clear()
    plain = SandboxTools(WorkflowTrace("test", "test"), lead=lead(),
                         history=history(("patient", "anything wed afternoon?")), practice_id=1,
                         now_local=NOW_NOV4)
    monkeypatch.setattr(plain, "_conn", no_conn)
    monkeypatch.setattr(plain, "_context", lambda conn, clinic, service=None: FakeContext())
    call_tool(plain, "find_times", {"when": "specific_date", "specific_date": "2026-11-04"})
    assert asked == [("specific_date", date(2026, 11, 4))]


def test_search_todays_include_the_pinned_calendar_day(monkeypatch):
    from rpt_agent.config import get_settings

    settings = get_settings().model_copy(update={"booking_today_override": date(2026, 7, 15)})
    live = LiveTools(WorkflowTrace("test", "test"), lead_id="00000000-0000-0000-0000-000000000001",
                     job_id=1, lead=lead(), history=[], settings=settings)
    assert date(2026, 7, 15) in live.search_todays() and len(live.search_todays()) == 2
    sandbox_tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=[],
                                 practice_id=1, now_local=NOW_NOV4)
    assert sandbox_tools.search_todays() == {TODAY_NOV4}


ASK_FIRST = "Thanks! What's your first name?"


@pytest.mark.parametrize("turns,given,expected", [
    (history(("elliot", ASK_FIRST), ("patient", "Dana")), None, "Dana"),
    (history(("elliot", ASK_FIRST), ("patient", "it's dana")), None, "Dana"),
    (history(("elliot", ASK_FIRST), ("patient", "Dana Quill")), None, "Dana"),
    (history(("elliot", ASK_FIRST), ("patient", "Dana")), "Dana", "Dana"),
    # x-04: the last name passed as first_name, with the answer in the chat: the answer.
    (history(("elliot", ASK_FIRST), ("patient", "Dana")), "Quill", "Dana"),
    # Not an answer to the question, the last name, a yes, a sentence: none.
    (history(("elliot", "What's your last name?"), ("patient", "Quill")), "Quill", ""),
    (history(("elliot", ASK_FIRST), ("patient", "Quill")), None, ""),
    (history(("elliot", ASK_FIRST), ("patient", "yes")), None, ""),
    (history(("elliot", ASK_FIRST), ("patient", "why do you need that")), None, ""),
    (history(("patient", "Dana")), None, ""),
])
def test_first_name_in_chat(turns, given, expected):
    from rpt_agent.services.sms_agent_tools import first_name_in_chat

    assert first_name_in_chat(lead(first_name="", last_name="Quill"), turns, given) == expected


def card_tools(monkeypatch, view, turns):
    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def conn():
        yield SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(
            fetchone=lambda: {"name": "Physical Therapy"}))

    module = slot_booking_module()
    monkeypatch.setattr(module, "open_slot", lambda *a, **k: {
        "local_date": date(2026, 11, 3), "local_time": time(9, 0), "display_name": "Alan Rome"})
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=view, history=turns, practice_id=1,
                         now_local=None, today=date(2026, 11, 3))
    monkeypatch.setattr(tools, "_conn", conn)
    monkeypatch.setattr(tools, "_context", lambda *a, **k: FakeContext())
    return tools


def test_no_first_name_on_file_asks_for_it_before_the_card(monkeypatch):
    # x-04 (2026-11-03): MISSING_NAME came only after the yes to the card; Elliot then asked for
    # the last name (on file). Now the card waits for the first name.
    no_first = lead(first_name="", last_name="Quill")
    picked = history(("elliot", "Fri Nov 6, 9:00 AM with Alan"), ("patient", "9am"),
                     ("elliot", "To confirm it's you, what's your date of birth?"),
                     ("patient", "3/14/1990"))
    args = {"date": "2026-11-03", "time": "9:00 AM", "therapist": "Alan"}
    result = call_tool(card_tools(monkeypatch, no_first, picked), "confirm_visit", args)
    assert result.code == "MISSING_NAME" and not result.fixed_texts
    assert "first name only" in result.output and "never ask for it" in result.output
    # A first name the patient never wrote, or the last name, does not open the card.
    for name in ("Jordan", "Quill"):
        result = call_tool(card_tools(monkeypatch, no_first, picked), "confirm_visit",
                           {**args, "first_name": name})
        assert result.code == "MISSING_NAME"
    named = [*picked, *history(("elliot", ASK_FIRST), ("patient", "Dana"))]
    for extra in ({"first_name": "Dana"}, {}):
        result = call_tool(card_tools(monkeypatch, no_first, named), "confirm_visit",
                           {**args, **extra})
        assert result.code == "CARD_SENT" and result.fixed_texts == [CARD]
    # A first name on file, or someone new: no name step.
    for view in (lead(), lead(is_new_person=True, first_name="", last_name="")):
        result = call_tool(card_tools(monkeypatch, view, picked), "confirm_visit", args)
        assert result.code == "CARD_SENT"


def test_booking_names_use_the_first_name_answer_when_the_model_passes_the_last_name():
    from rpt_agent.services.sms_agent_tools import booking_names

    turns = history(("elliot", ASK_FIRST), ("patient", "Dana"), ("elliot", CARD), ("patient", "yes"))
    assert booking_names(lead(first_name="", last_name="Quill"), turns, "Quill", "Quill") == (
        "Dana", "Quill")


def test_cancel_visit_sends_its_own_text_and_keeps_booking_open():
    """Yajat's test, 2026-11-05: a cancel got the general front desk text, which never said the
    visit is still booked."""
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=ANSWERED,
                         practice_id=1, now_local=None)
    tools._conn = lambda: nullcontext(None)
    tools._render = lambda conn, key, **values: f"TEXT:{key}"
    handed = call_tool(tools, "hand_off", {"reason": "cancel_visit", "notes": "Cancel Sat visit"})
    assert handed.code == "HANDED_OFF" and handed.fixed_texts == ["TEXT:cancel_visit"]
    assert handed.summary == "Would open a Needs Review card: wants to cancel or change their visit"
    assert not tools.paused_by_hand_off
    assert call_tool(tools, "hand_off", {"reason": "other"}).fixed_texts == ["TEXT:front_desk"]
    text = texts.render(None, "cancel_visit", settings=get_settings().model_copy(
        update={"clinic_front_desk_phone": "(949) 276-5401"}))
    assert "stays booked" in text and "(949) 276-5401" in text


def test_insurance_sends_its_own_text():
    """Emma's retest, 2026-11-13: the general front desk text never named insurance."""
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=ANSWERED,
                         practice_id=1, now_local=None)
    tools._conn = lambda: nullcontext(None)
    tools._render = lambda conn, key, **values: f"TEXT:{key}"
    handed = call_tool(tools, "hand_off", {"reason": "insurance"})
    assert handed.code == "HANDED_OFF" and handed.fixed_texts == ["TEXT:insurance"]
    text = texts.render(None, "insurance", settings=get_settings().model_copy(
        update={"clinic_front_desk_phone": "(949) 276-5401"}))
    assert "insurance question" in text and "(949) 276-5401" in text


def test_already_handed_off_gives_the_number_when_asked():
    """Yajat's test, 2026-11-05: "I want to talk to the front desk" after a hand-off got "Our front
    desk already has your request" without the number."""
    settings = get_settings().model_copy(update={"clinic_front_desk_phone": "(949) 276-5401"})
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=ANSWERED,
                         practice_id=1, now_local=None, settings=settings,
                         earlier={"hand_off:wants_person"})
    again = call_tool(tools, "hand_off", {"reason": "wants_person"})
    assert again.code == "ALREADY_HANDED_OFF" and again.fixed_texts == []
    assert "(949) 276-5401" in again.output


@pytest.mark.parametrize(("kind", "text"), [("medical", texts.EMERGENCY_TEXT),
                                            ("self_harm", texts.SELF_HARM_TEXT)])
def test_emergency_text_by_kind(kind, text):
    """Yajat's test, 2026-11-05: chest pain got the 988 line too."""
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=lead(), history=ANSWERED,
                         practice_id=1, now_local=None)
    assert call_tool(tools, "flag_emergency", {"kind": kind}).fixed_texts == [text]
