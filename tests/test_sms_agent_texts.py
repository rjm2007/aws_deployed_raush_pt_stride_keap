"""SMS agent editable texts (services/sms_agent_texts.py) and the read-back card. Fake connection."""

from __future__ import annotations

import re
from datetime import date, time
from pathlib import Path

import pytest

from rpt_agent.config import get_settings
from rpt_agent.services import sms_agent_texts as texts

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
# The Studio texts' seeds: 054, and 055 (sms_agent.cancel_visit, 2026-11-07).
MIGRATION_FILES = ("054_sms_agent.sql", "055_sms_agent_cancel_text.sql",
                   "057_sms_agent_booking_problem_reply_limit.sql")


class Conn:
    """Answers the template and booking-link queries from dicts."""

    def __init__(self, bodies=None, link="https://book.example/rpt"):
        self.bodies = bodies or {}
        self.link = link
        self.params = []

    def execute(self, sql, params=None):
        self.params.append((sql, params))
        if "from message_templates" in sql:
            body = self.bodies.get(params[0])
            row = {"body": body} if body is not None else None
        elif "from practice_settings" in sql:
            row = {"booking_link_url": self.link} if self.link else None
        else:
            raise AssertionError(sql)

        class Rows:
            def fetchone(self):
                return row
        return Rows()


def settings():
    return get_settings().model_copy(update={"clinic_front_desk_phone": "(949) 276-5401"})


def test_drafts_match_the_migrations():
    """The code fallback is the same text staff start from in the Studio."""
    sql = "\n".join((MIGRATIONS / name).read_text() for name in MIGRATION_FILES)
    for key, draft in texts.DRAFTS.items():
        match = re.search(r"\('sms_agent\." + key + r"',\s*\n(.*?)\)(?:,|\n\) as t)", sql, re.DOTALL)
        assert match, key
        pieces = re.findall(r"'((?:[^']|'')*)'|(chr\(10\))", match.group(1))
        body = "".join("\n" if newline else literal.replace("''", "'")
                       for literal, newline in pieces)
        assert body == draft, key


def test_render_uses_the_studio_text_and_fills_placeholders():
    conn = Conn({"sms_agent.front_desk": "Call {front_desk_phone} or book: {booking_link}"})
    text = texts.render(conn, "front_desk", practice_id=7, settings=settings())
    assert text == "Call (949) 276-5401 or book: https://book.example/rpt"
    sql, params = next(item for item in conn.params if "message_templates" in item[0])
    assert "cadence_step_id is null" in sql and "is_active" in sql
    assert params == ("sms_agent.front_desk", 7, 7)


def test_render_falls_back_to_the_draft_when_the_row_is_gone_or_blank():
    for conn in (Conn({}), Conn({"sms_agent.text_only": "   "}), None):
        assert texts.render(conn, "text_only", settings=settings()) == texts.DRAFTS["text_only"]


def test_unknown_placeholder_is_left_as_is():
    conn = Conn({"sms_agent.intro": "Hi {first_name}! {typo_here} {booking_link}"})
    text = texts.render(conn, "intro", first_name="Sam", settings=settings())
    assert text == "Hi Sam! {typo_here} https://book.example/rpt"


def test_missing_link_keeps_the_placeholder_rather_than_inventing_one():
    text = texts.render(Conn({}, link=None), "unknown_number", settings=settings())
    assert "{booking_link}" in text and "(949) 276-5401" in text


def test_booked_text_with_address_and_empty_name():
    text = texts.render(Conn({}), "booked", first_name="", day="Tuesday, November 3",
                        time="9:00 AM", therapist="Alan Rome", clinic="Laguna Niguel",
                        settings=settings())
    assert text.startswith("You're all set! Tuesday, November 3 at 9:00 AM with Alan Rome at our "
                           "Laguna Niguel office (30100 Town Center Drive, Suite YZ, Laguna Niguel).")
    intro = texts.render(Conn({}), "intro", first_name="", settings=settings())
    assert intro.startswith("Hi, this is Elliot")


def test_unknown_key_is_a_bug():
    with pytest.raises(KeyError):
        texts.render(None, "goodbye", settings=settings())


def test_every_contract_key_has_a_draft():
    assert set(texts.KEYS) == set(texts.DRAFTS)


@pytest.mark.parametrize("clinic,expected", [
    ("Dana Point", "24582 Del Prado Avenue, Suite C, Dana Point"),
    ("mission viejo office", "24000 Alicia Parkway, Suite 27, Mission Viejo"),
    ("Irvine", ""),
])
def test_address_for(clinic, expected):
    assert texts.address_for(clinic) == expected


def test_card_round_trip_and_shape():
    card = texts.confirm_card(date(2026, 11, 3), time(9, 0), "Alan Rome", "Laguna Niguel")
    assert card == ("Here's your visit:\nTue Nov 3 at 9:00 AM with Alan Rome\n"
                    "Laguna Niguel office\nReply YES and I'll book it.")
    assert texts.parse_card(card) == {"day": "Tue Nov 3", "time": "9:00 AM",
                                      "therapist": "Alan Rome", "service": None,
                                      "clinic": "Laguna Niguel"}
    assert texts.parse_card("  " + card + "\n") is not None
    # Cards sent before 2026-11-04 opened with "Great choice! " and still count as cards.
    assert texts.parse_card("Great choice! " + card) == texts.parse_card(card)


def test_card_names_a_service_other_than_the_default():
    # np-03 (gpt-5.5 eval): a new person asked for pelvic and got a Physical Therapy card. The
    # card names the service when it is not the default, and the guard reads it back.
    card = texts.confirm_card(date(2026, 11, 16), time(13, 0), "Thao Nguyen", "Dana Point",
                              service="Pelvic Health")
    assert card == ("Here's your visit:\nMon Nov 16 at 1:00 PM with Thao Nguyen\n"
                    "Pelvic Health\nDana Point office\nReply YES and I'll book it.")
    assert texts.parse_card(card) == {"day": "Mon Nov 16", "time": "1:00 PM",
                                      "therapist": "Thao Nguyen", "service": "Pelvic Health",
                                      "clinic": "Dana Point"}
    assert texts.confirm_card(date(2026, 11, 16), time(13, 0), "Thao", "Dana Point",
                              service=" ") == texts.confirm_card(date(2026, 11, 16), time(13, 0),
                                                                 "Thao", "Dana Point")


@pytest.mark.parametrize("text", [
    "", None, "Here's your visit:\nTue Nov 3 at 9:00 AM with Alan Rome",
    "Here's your visit: Tue Nov 3 at 9:00 AM with Alan Rome. Reply YES and I'll book it.",
    texts.confirm_card(date(2026, 11, 3), time(9, 0), "Alan", "Dana Point") + " Also, hi!",
])
def test_not_a_card(text):
    assert texts.parse_card(text) is None


def test_emergency_line_is_in_code():
    assert "emergency" not in texts.DRAFTS
    for text in (texts.EMERGENCY_TEXT, texts.SELF_HARM_TEXT):
        assert "911" in text and len(text) <= 200
    # Yajat's test, 2026-11-05: chest pain got the 988 line. 988 (owner, 2026-11-02) is only for
    # thoughts of suicide or self-harm.
    assert "988" not in texts.EMERGENCY_TEXT and "call or text 988" in texts.SELF_HARM_TEXT


def test_emergency_text_by_kind():
    assert texts.emergency_text("medical") == texts.EMERGENCY_TEXT
    assert texts.emergency_text("self_harm") == texts.SELF_HARM_TEXT
    # Unknown: the text with both 911 and 988.
    assert texts.emergency_text(None) == texts.SELF_HARM_TEXT


def test_the_old_emergency_text_is_still_an_emergency_text():
    old = ("If this is a medical emergency, please call 911 right away. If you're thinking about "
           "harming yourself, call or text 988 any time. Our front desk team will also reach out to "
           "you.")
    for text in (old, texts.EMERGENCY_TEXT, " " + texts.SELF_HARM_TEXT + "\n"):
        assert texts.is_emergency_text(text)
    assert not texts.is_emergency_text("Please call 911.")
