"""The SMS agent's birthday step (owner, 2026-11-03), without a database.

Test chat that started it: "is your birthday November 1?" -> "yep" -> card -> "yes" -> "Could you send
your date of birth with the year?" -> "nov 1, 2026" -> the same card -> "yes" -> the same question
again, never booked (the booking refused a birthday after the demo's pinned calendar day). Now the
patient types their date of birth, check_birthday reads it in code, the Context says whether the chat
settled it, the card needs it, the same card never goes twice in a row and a second failed booking for
the same reason hands off.
"""

from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import slot_booking, sms_agent
from rpt_agent.services import sms_agent_birthday as birthday
from rpt_agent.services import sms_agent_texts as texts
from rpt_agent.services.sms_agent_context import LeadView, Turn
from rpt_agent.services.sms_agent_tools import (
    CARD_ALREADY_SENT,
    LiveTools,
    SandboxTools,
    ToolResult,
    call_tool,
)

TODAY = date(2026, 11, 3)
ON_FILE = date(1990, 3, 14)
CARD = texts.confirm_card(date(2026, 7, 18), time(10, 0), "Thao Nguyen", "Dana Point")


def lead(**update):
    values = {"is_new_person": False, "first_name": "Sam", "last_name": "Test",
              "date_of_birth": ON_FILE, "clinic": "Dana Point", "service": "Physical Therapy",
              "referral_word": "knee", "status": "in_progress", "cadence_state": "active",
              "needs_review": False, "review_reason": "", "booked_visit": "", "recent_calls": []}
    values.update(update)
    return LeadView(**values)


def chat(*turns):
    return [Turn(role, text, None) for role, text in turns]


# --- reading what the patient typed -----------------------------------------------------------------

@pytest.mark.parametrize("words,expected", [
    ("3/14/1990", ON_FILE), ("03-14-1990", ON_FILE), ("3.14.90", ON_FILE), ("1990-03-14", ON_FILE),
    ("March 14 1990", ON_FILE), ("Mar 14, 1990", ON_FILE), ("march 14th, 1990", ON_FILE),
    ("14 March 1990", ON_FILE), ("14/3/1990", ON_FILE), ("mar 14 '90", ON_FILE),
    ("my birthday is 3/14/90 thx", ON_FILE),
    # 2-digit years: 20xx only when that is at least a year back.
    ("3/14/05", date(2005, 3, 14)), ("3/14/30", date(1930, 3, 14)), ("Jul 18, 88", date(1988, 7, 18)),
    ("nov 1, 2026", date(2026, 11, 1)),
])
def test_common_us_formats_are_read(words, expected):
    assert birthday.parse_birth_date(words, TODAY) == birthday.Parsed(expected)


@pytest.mark.parametrize("words,need_year", [
    ("nov 1", True), ("11/1", True), ("March 14th", True),
    # Times and visit days are not birth dates (a "10:30" is never a year).
    ("sat jul 18 at 10:00", True), ("Nov 20 10:30", True), ("Nov 20, 10am", True),
    ("yes", False), ("mark 3 1990", False), ("call me at 949 555 0100", False),
])
def test_no_full_date(words, need_year):
    assert birthday.parse_birth_date(words, TODAY) == birthday.Parsed(None, need_year=need_year)


@pytest.mark.parametrize("words", ["can I come 11/20/26?", "3/14/26", "1/5/27", "Nov 20, '26",
                                   "11/20/25"])
def test_a_two_digit_year_near_now_is_a_visit_day_not_a_birth_year(words):
    # Review 2026-11-03: "11/20/26" read as 1926-11-20 and settled the date of birth of someone new.
    parsed = birthday.parse_birth_date(words, TODAY)
    assert parsed.value is None and birthday.birth_date_in(words, TODAY) is None
    state = birthday.birthday_state(lead(is_new_person=True, date_of_birth=None),
                                    chat(("patient", words)), TODAY)
    assert state.confirmed is None


@pytest.mark.parametrize("value,ok", [
    (date(2025, 11, 3), True), (date(2025, 11, 4), False),   # at least one year old
    (date(2026, 11, 1), False), (date(2027, 1, 1), False),   # this year, the future
    (date(1906, 11, 3), True), (date(1906, 11, 2), False),   # at most 120 years
])
def test_possible_is_one_to_120_years_old(value, ok):
    assert birthday.possible(value, TODAY) is ok


# --- check_birthday --------------------------------------------------------------------------------

@pytest.mark.parametrize("history,words,code", [
    (chat(("patient", "3/14/1990")), "3/14/1990", "MATCH"),
    (chat(("patient", "march 14 1990")), "1990-03-14", "MATCH"),
    (chat(("patient", "nov 1")), "nov 1", "NEED_YEAR"),
    (chat(("patient", "nov 1, 2026")), "nov 1, 2026", "NOT_POSSIBLE"),
    (chat(("patient", "1/1/2099")), "1/1/2099", "NOT_POSSIBLE"),
    (chat(("patient", "1/1/1880")), "1/1/1880", "NOT_POSSIBLE"),
    (chat(("patient", "hmm")), "hmm", "UNREADABLE"),
    # A date the model worked out from words the code cannot read: asked again in a readable form.
    (chat(("patient", "the fourteenth of march, ninety")), "1990-03-14", "UNREADABLE"),
    (chat(("patient", "7/2/1979")), "7/2/1979", "DIFFERENT"),
    (chat(("patient", "7/2/1979"), ("elliot", "Could you double-check that date of birth for me?"),
          ("patient", "July 2 1979")), "7/2/1979", "CONFIRMED_DIFFERENT"),
])
def test_check_birthday_codes(history, words, code):
    result = birthday.check(lead(), history, words, TODAY)
    assert result.code == code and result.output.startswith(code)
    # The date on file is never in what the model reads.
    assert "1990" not in result.output and "March 14" not in result.output


def test_a_new_person_or_no_date_on_file_is_new():
    history = chat(("patient", "8/23/1985"))
    assert birthday.check(lead(is_new_person=True), history, "8/23/1985", TODAY).code == "NEW"
    assert birthday.check(lead(date_of_birth=None), history, "8/23/1985", TODAY).code == "NEW"


# --- what the chat settled (Context line, booking) -------------------------------------------------

DOUBLE = "Could you double-check that date of birth for me?"


@pytest.mark.parametrize("history,confirmed,pending,differs", [
    (chat(("patient", "hi")), None, None, False),
    (chat(("patient", "3/14/1990")), ON_FILE, None, False),
    (chat(("patient", "nov 1, 2026")), None, None, False),   # not possible: never confirmed
    (chat(("patient", "7/2/1979")), None, date(1979, 7, 2), False),
    # The double-check settles it: the date again, a yes, or a corrected date.
    (chat(("patient", "7/2/1979"), ("elliot", DOUBLE), ("patient", "7/2/1979")),
     date(1979, 7, 2), None, True),
    (chat(("patient", "7/2/1979"), ("elliot", DOUBLE), ("patient", "yes that's right")),
     date(1979, 7, 2), None, True),
    (chat(("patient", "7/2/1979"), ("elliot", DOUBLE), ("patient", "oops 3/14/1990")),
     ON_FILE, None, False),
    (chat(("patient", "7/2/1979"), ("elliot", DOUBLE), ("patient", "7/3/1979")),
     date(1979, 7, 3), None, True),
    # A yes to something else is not a yes to the double-check.
    (chat(("patient", "7/2/1979"), ("elliot", "Want a morning?"), ("patient", "yes")),
     None, date(1979, 7, 2), False),
    # An explicit correction after it was settled is used.
    (chat(("patient", "3/14/1990"), ("elliot", CARD), ("patient", "wait my dob is 3/15/1990")),
     date(1990, 3, 15), None, True),
])
def test_birthday_state(history, confirmed, pending, differs):
    state = birthday.birthday_state(lead(), history, TODAY)
    assert (state.confirmed, state.pending, state.differs) == (confirmed, pending, differs)


@pytest.mark.parametrize("history,confirmed", [
    # Review 2026-11-03: any old date in a patient's text settled their date of birth (someone new
    # was never asked and would be booked as born on the day they got hurt).
    (chat(("patient", "I hurt my knee on 3/2/2024 playing soccer")), None),
    (chat(("patient", "had surgery jan 5 2021 and still sore")), None),
    # A date of birth said as one, asked for, or little more than the date counts.
    (chat(("patient", "Hi I'm Ana Ruiz, new patient, my birthday is 8/23/1985")), date(1985, 8, 23)),
    (chat(("patient", "Ana Ruiz 8/23/1985")), date(1985, 8, 23)),
    (chat(("elliot", "To confirm it's you, what's your date of birth?"),
          ("patient", "sure thing, it would be august 23 1985 I think")), date(1985, 8, 23)),
    # The question split from the answer by a second patient text still counts.
    (chat(("elliot", "What's your date of birth?"), ("patient", "one sec"),
          ("patient", "ok so it is 8/23/1985 for the record")), date(1985, 8, 23)),
])
def test_only_a_date_given_as_a_date_of_birth_counts(history, confirmed):
    state = birthday.birthday_state(lead(is_new_person=True, date_of_birth=None), history, TODAY)
    assert state.confirmed == confirmed


NEW_PERSON = {"is_new_person": True, "first_name": "", "last_name": "", "date_of_birth": None}


@pytest.mark.parametrize("history,confirmed", [
    # Eval np-03 (2026-11-03): a full date that can only be a date of birth counts, so Elliot does
    # not ask for it again.
    (chat(("patient", "I'd like to book. Brooke Tanner, 02/09/1993")), date(1993, 2, 9)),
    # At least 10 years back, many other words, no birth word, not asked.
    (chat(("patient", "hello I would like to book a first visit please 2/9/1993 thanks")),
     date(1993, 2, 9)),
    # Right after a name: a recent date of birth (a child) next to their name.
    (chat(("patient", "Hi can you book a first visit for my son please, Leo Tanner 4/2/2020")),
     date(2020, 4, 2)),
    # Sent while an earlier date of birth question is unanswered (another text in between).
    (chat(("elliot", "To confirm it's you, what's your date of birth?"),
          ("patient", "which therapist is it?"), ("elliot", "It's Alan."),
          ("patient", "ok cool it is 4/2/2020 and Alan sounds great to me")), date(2020, 4, 2)),
    # The reviewer's protections stay: a visit day, a recent injury, an event long ago, a holiday.
    (chat(("patient", "can I come in on 11/20/26 in the morning please")), None),
    (chat(("patient", "I hurt my knee on 3/2/2024 playing soccer")), None),
    (chat(("patient", "I hurt my knee on 3/2/2010 playing soccer and it still aches")), None),
    (chat(("patient", "had knee surgery back on 1/5/2012 and never got it checked again")), None),
    (chat(("patient", "can you book me something after Labor Day 9/1/2025 please")), None),
    (chat(("patient", "I can come any morning, the doctor saw me 4/2/2025 for it")), None),
])
def test_a_date_that_can_only_be_a_date_of_birth_counts(history, confirmed):
    state = birthday.birthday_state(lead(**NEW_PERSON), history, TODAY)
    assert state.confirmed == confirmed


def test_np03_check_birthday_is_new_not_asked_again():
    history = chat(("elliot", "Yes, we do pelvic floor therapy. Want me to find you a time?"),
                   ("patient", "I'd like to book. Brooke Tanner, 02/09/1993"))
    result = birthday.check(lead(**NEW_PERSON), history, "02/09/1993", TODAY)
    assert result.code == "NEW"


def test_a_date_next_to_the_name_on_file_counts():
    history = chat(("patient", "please book me in for a visit next week, Test 4/2/2020"))
    assert birthday.birthday_state(lead(date_of_birth=None), history, TODAY).confirmed == date(
        2020, 4, 2)
    assert birthday.next_to_name("Brooke Tanner, 02/09/1993")
    assert not birthday.next_to_name("on Labor Day 9/1/2025")
    assert not birthday.next_to_name("see you Nov 20 2026")


YEAR_Q = "Could you send the year too? For example, 6/3/1988."


@pytest.mark.parametrize("history,confirmed", [
    # Eval x-07 (2026-11-03): "June 3", then "1989" was UNREADABLE and the whole date of birth
    # question came again. The year alone after a month and day is that date.
    (chat(("elliot", "What's your date of birth?"), ("patient", "birthday is June 3"),
          ("elliot", YEAR_Q), ("patient", "1989")), date(1989, 6, 3)),
    (chat(("elliot", "What's your date of birth?"), ("patient", "6/3"),
          ("elliot", "And the year you were born?"), ("patient", "it's '89")), date(1989, 6, 3)),
    (chat(("elliot", "What's your date of birth?"), ("patient", "june 3"),
          ("elliot", YEAR_Q), ("patient", "89")), date(1989, 6, 3)),
    # Not when we did not ask about the year or birth ("10" after "nov 20" may be a time).
    (chat(("elliot", "What day works?"), ("patient", "nov 20"),
          ("elliot", "What time that day?"), ("patient", "10")), None),
    # Not a year that makes an impossible date, and not a year after some other text.
    (chat(("elliot", "What's your date of birth?"), ("patient", "june 3"),
          ("elliot", YEAR_Q), ("patient", "2026")), None),
    (chat(("elliot", "What's your date of birth?"), ("patient", "june 3"),
          ("elliot", YEAR_Q), ("patient", "hmm"), ("elliot", YEAR_Q), ("patient", "1989")), None),
    (chat(("elliot", "What's your date of birth?"), ("patient", "1989")), None),
])
def test_a_year_alone_after_a_month_and_day(history, confirmed):
    state = birthday.birthday_state(lead(is_new_person=True, date_of_birth=None), history, TODAY)
    assert state.confirmed == confirmed


@pytest.mark.parametrize("words", ["June 3 1989", "1989", "'89"])
def test_check_birthday_after_a_year_alone_is_new_not_asked_again(words):
    history = chat(("elliot", "What's your date of birth?"), ("patient", "birthday is June 3"),
                   ("elliot", YEAR_Q), ("patient", "1989"))
    result = birthday.check(lead(is_new_person=True, date_of_birth=None), history, words, TODAY)
    assert result.code == "NEW"


def test_check_birthday_a_year_alone_matches_the_date_on_file():
    history = chat(("elliot", "To confirm it's you, what's your date of birth?"),
                   ("patient", "march 14"), ("elliot", "Thanks! And the year you were born?"),
                   ("patient", "1990"))
    assert birthday.check(lead(), history, "1990", TODAY).code == "MATCH"


def test_check_birthday_on_a_date_not_given_as_a_birthday_asks_for_it():
    history = chat(("patient", "I hurt my knee on 3/2/2024 playing soccer"))
    result = birthday.check(lead(is_new_person=True, date_of_birth=None), history, "3/2/2024", TODAY)
    assert result.code == "UNREADABLE" and "what's your date of birth?" in result.output


def test_context_line():
    assert birthday.context_line(birthday.BirthdayState(ON_FILE, None, False, ())).startswith("yes")
    assert birthday.context_line(birthday.BirthdayState(None, None, False, ())) == "no"
    pending = birthday.context_line(birthday.BirthdayState(None, date(1979, 7, 2), False, ()))
    assert pending.startswith("not yet") and "1979" not in pending


# --- tools ------------------------------------------------------------------------------------------

def sandbox(history, **kwargs):
    tools = SandboxTools(WorkflowTrace("test", "test"), lead=kwargs.pop("view", lead()),
                         history=history, practice_id=1, now_local=None, today=TODAY, **kwargs)
    return tools


def no_db(monkeypatch, tools):
    monkeypatch.setattr(tools, "_conn", lambda: pytest.fail("no database"))


def test_check_birthday_tool_needs_no_database(monkeypatch):
    tools = sandbox(chat(("patient", "3/14/1990")))
    no_db(monkeypatch, tools)
    result = call_tool(tools, "check_birthday", {"date_of_birth": "3/14/1990"})
    assert result.code == "MATCH" and result.kind == "check_birthday" and not result.fixed_texts


@pytest.mark.parametrize("history,expected", [
    (chat(("patient", "10am works")), "Ask once"),
    (chat(("patient", "nov 1")), "Ask once"),
    (chat(("patient", "nov 1, 2026")), "Ask once"),
    (chat(("patient", "7/2/1979")), "double-check"),
])
def test_no_card_before_a_date_of_birth_is_confirmed(monkeypatch, history, expected):
    tools = sandbox(history)
    no_db(monkeypatch, tools)
    result = call_tool(tools, "confirm_visit", {"date": "2026-07-18", "time": "10:00 AM"})
    assert result.code == "NEED_BIRTH_DATE" and expected in result.output
    assert not result.fixed_texts


def test_the_same_card_is_never_sent_twice_in_a_row(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def conn():
        # default_service: Physical Therapy (no service line on the card).
        yield SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(
            fetchone=lambda: {"name": "Physical Therapy"}))

    context = SimpleNamespace(case_type={"name": "Physical Therapy"},
                              location={"name": "Dana Point"}, clinicians=[],
                              lead={"practice_id": 1})
    slot = {"local_date": date(2026, 7, 18), "local_time": time(10, 0),
            "display_name": "Thao Nguyen"}
    monkeypatch.setattr(slot_booking, "open_slot", lambda *a, **k: slot)
    turns = chat(("patient", "3/14/1990"), ("elliot", CARD), ("patient", "Liked “Great”"))
    # A new step in between (Elliot answered "maybe"): the card may go again. A reaction alone is
    # no new step.
    answered = chat(("patient", "3/14/1990"), ("elliot", CARD), ("patient", "maybe"),
                    ("elliot", "No rush!"), ("patient", "ok book it"))
    for history, code in ((answered, "CARD_SENT"), (turns, "CARD_ALREADY_SENT"),
                          (turns[:2], "CARD_ALREADY_SENT")):
        tools = sandbox(history)
        monkeypatch.setattr(tools, "_conn", conn)
        monkeypatch.setattr(tools, "_context", lambda *a, **k: context)
        result = call_tool(tools, "confirm_visit", {"date": "2026-07-18", "time": "10:00 AM"})
        if code == "CARD_SENT":
            assert result.code == code and result.fixed_texts == [CARD]
        else:
            assert result.code == code and result.output == CARD_ALREADY_SENT
            assert not result.fixed_texts


def test_a_second_failed_booking_for_the_same_reason_hands_off(monkeypatch):
    # No card answered: NOT_CONFIRMED. Once is a nudge; the second time (in this reply or earlier in
    # the chat) the front desk text goes and booking stops.
    for earlier, calls in (({}, 2), ({"NOT_CONFIRMED": 1}, 1)):
        tools = sandbox(chat(("patient", "book it")), earlier_failures=earlier)
        monkeypatch.setattr(tools, "_hand_off_text", lambda reason: "FRONT DESK TEXT")
        results = [call_tool(tools, "book_visit", {"date": "2026-07-18", "time": "10:00 AM"})
                   for _ in range(calls)]
        last = results[-1]
        assert [r.code for r in results[:-1]] == ["NOT_CONFIRMED"] * (calls - 1)
        assert last.code == "HANDED_OFF" and last.action_code == "HANDED_OFF_PAUSED"
        assert last.reason == "booking_problem" and last.fixed_texts == ["FRONT DESK TEXT"]
        assert tools.paused_by_hand_off
    # Different reasons do not add up.
    tools = sandbox(chat(("patient", "book it")), earlier_failures={"MISSING_NAME": 1})
    assert call_tool(tools, "book_visit", {"date": "2026-07-18", "time": "10:00 AM"}).code == \
        "NOT_CONFIRMED"


def test_a_bad_date_of_birth_twice_hands_off(monkeypatch):
    # The loop in the test chat: BAD_DATE_OF_BIRTH, asked again, BAD_DATE_OF_BIRTH again.
    tools = sandbox(chat(("patient", "3/14/1990"), ("elliot", CARD), ("patient", "yes")),
                    earlier_failures={"BAD_DATE_OF_BIRTH": 1})

    def refuse(**_):
        raise ValueError("BAD_DATE_OF_BIRTH: that date of birth is not possible.")

    monkeypatch.setattr(tools, "_book_visit", refuse)
    monkeypatch.setattr(tools, "_hand_off_text", lambda reason: "FRONT DESK TEXT")
    result = call_tool(tools, "book_visit", {"date": "2026-07-18", "time": "10:00 AM"})
    assert result.code == "HANDED_OFF" and result.fixed_texts == ["FRONT DESK TEXT"]
    assert "BAD_DATE_OF_BIRTH" in result.output and "Ask nothing more" in result.output


def test_earlier_failures_come_from_the_side_panel_codes():
    prior = [{"kind": "book", "result_code": "NOT_CONFIRMED"},
             {"kind": "book", "result_code": "BAD_DATE_OF_BIRTH"},
             {"kind": "book", "result_code": "BAD_DATE_OF_BIRTH"},
             {"kind": "confirm_visit", "result_code": "NOT_CONFIRMED"},
             {"kind": "book", "result_code": "BOOKED"}]
    state = sms_agent.earlier_in_chat([], prior)
    assert state.failures == {"NOT_CONFIRMED": 1, "BAD_DATE_OF_BIRTH": 2}
    # The automatic hand-off's code stops booking in the test chat like live.
    handed = sms_agent.earlier_in_chat([], [{"kind": "book", "result_code": "HANDED_OFF_PAUSED",
                                             "reason": "booking_problem"}])
    assert handed.paused and "hand_off:booking_problem" in handed.done


def test_the_booking_uses_the_date_the_chat_confirmed(monkeypatch):
    # Their own date (double-checked) beats the model's argument and the file; staff see it differs.
    history = chat(("patient", "7/2/1979"), ("elliot", DOUBLE), ("patient", "yes"))
    tools = sandbox(history)
    assert tools._birth_date_for_booking("1990-03-14") == ("1979-07-02", True)
    assert sandbox(chat(("patient", "3/14/1990")))._birth_date_for_booking(None) == \
        ("1990-03-14", False)
    assert sandbox(chat(("patient", "hi")))._birth_date_for_booking(None) == ("1990-03-14", False)


# --- the loop: Context line, a reaction to the card --------------------------------------------------

class Client:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.responses = self

    def create(self, **request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        return {"output": [{"type": "message", "content": [{"type": "output_text",
                                                            "text": answer}]}]}


@pytest.fixture
def small_config(monkeypatch):
    monkeypatch.setattr(sms_agent, "load_prompt", lambda: "PROMPT")
    monkeypatch.setattr(sms_agent, "load_tools", list)


def run(history, client):
    from rpt_agent.config import get_settings

    tools = sandbox(history)
    return sms_agent.run_agent(
        lead=lead(), history=history, tools=tools,
        now=datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
        front_desk_open=True, settings=get_settings(), client=client)


def test_context_says_the_date_of_birth_is_confirmed_and_never_shows_it(small_config):
    client = Client("Here's what I have open")
    run(chat(("patient", "3/14/1990")), client)
    context = client.requests[0]["input"][1]["content"]
    assert "Date of birth confirmed in this chat: yes (never ask for it again)" in context
    assert "Birthday on file: yes" in context and "1990" not in context
    client = Client("ok")
    run(chat(("patient", "hello")), client)
    assert "Date of birth confirmed in this chat: no" in client.requests[0]["input"][1]["content"]


def test_a_reaction_to_the_card_needs_no_reply(small_config):
    history = chat(("patient", "3/14/1990"), ("elliot", CARD),
                   ("patient", "Liked “Here's your visit:”"))
    reply = run(history, Client("NO_REPLY"))
    assert reply.no_reply and reply.error is None
    # "maybe" is not a reaction: it gets an answer.
    client = Client("NO_REPLY", "No rush!")
    reply = run(chat(("patient", "3/14/1990"), ("elliot", CARD), ("patient", "maybe")), client)
    assert reply.texts == ["No rush!"]


@pytest.mark.parametrize("text,asks", [
    ("To confirm it's you, what's your date of birth?", True),
    ("Thanks! And the year you were born?", True),
    ("Could you send your DOB again with the year? I may have that wrong.", True),
    ("Please confirm your birthday", True),
    ("Your date of birth is confirmed. Which time works best?", False),
    ("No need, we already have your date of birth.", False),
    ("Which time works for you?", False),
])
def test_asks_birth_date(text, asks):
    assert sms_agent.asks_birth_date(text) is asks


def test_a_confirmed_date_of_birth_is_never_asked_again(small_config):
    # Owner, 2026-11-03: once a valid date of birth is given in the chat, never asked again.
    history = chat(("patient", "3/14/1990"), ("elliot", CARD), ("patient", "yes"))
    client = Client("Could you send your date of birth again with the year?", "One moment!")
    reply = run(history, client)
    assert reply.texts == ["One moment!"] and reply.error is None
    note = client.requests[1]["input"][-1]["content"]
    assert note == sms_agent.ASKED_BIRTH_DATE_NOTE
    # A second miss sends nothing of the model's.
    reply = run(history, Client("What's your birthday?", "What's your DOB?"))
    assert reply.texts == [] and reply.error == "guard:asked_birth_date"
    # Before it is confirmed the question is the identity step and goes out.
    reply = run(chat(("patient", "10am works")),
                Client("To confirm it's you, what's your date of birth?"))
    assert reply.texts == ["To confirm it's you, what's your date of birth?"]


def test_live_reads_further_back_for_the_date_of_birth():
    # Live gives the model 20 texts; a date of birth given before those still counts.
    old = chat(("patient", "3/14/1990"))
    recent = chat(*[("patient", "question?"), ("elliot", "answer.")] * 10)
    tools = LiveTools(WorkflowTrace("test", "test"), lead_id="lead-1", job_id=1, lead=lead(),
                      history=recent, practice_id=1, today=TODAY, birthday_history=old + recent)
    assert tools.birthday_state().confirmed == ON_FILE
    assert call_tool(tools, "check_birthday", {"date_of_birth": "3/14/1990"}).code == "MATCH"


def test_card_already_sent_allows_no_reply(small_config):
    tools_result = ToolResult(CARD_ALREADY_SENT, "confirm_visit", "Card already sent")
    actions = [sms_agent.AgentAction("confirm_visit", "x", tools_result.code)]
    assert sms_agent._check_answer("", lead=lead(), history=chat(("patient", "hmm")),
                                   actions=actions, fixed=False) is None


# --- the booking engine: the real date, never the pinned calendar -----------------------------------

def test_the_booking_engine_checks_a_birthday_against_the_real_date():
    tz = ZoneInfo("America/Los_Angeles")
    pinned = SimpleNamespace(now_local=datetime(2026, 7, 15, 9, 0, tzinfo=tz))
    real = datetime.now(tz).date()
    assert slot_booking.birth_date_today(pinned) == real
    # A date after the pinned day but before the real today is a real date of birth.
    patient = slot_booking.validate_patient(
        {"first_name": "Sam", "last_name": "Test", "date_of_birth": "2026-07-20"}, real)
    assert patient.date_of_birth == date(2026, 7, 20)
    with pytest.raises(ValueError, match="BAD_DATE_OF_BIRTH"):
        slot_booking.validate_patient(
            {"first_name": "Sam", "last_name": "Test", "date_of_birth": "2999-01-01"}, real)
