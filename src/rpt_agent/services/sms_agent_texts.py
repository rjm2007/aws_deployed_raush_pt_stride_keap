"""Texts the SMS agent sends word for word (SMS agent plan section 5, owner 2026-11-02).

The texts we can predict (first reply, booked, front desk hand-off, English only, "I can only read
text", unknown number) are editable by staff in the SMS Template Studio: `message_templates` rows
with no cadence step and key `sms_agent.<name>` (seeded by migration 054). The agent never words
them itself. A deleted or inactive row falls back to the same draft here, so a Studio mistake can
never leave a patient without an answer.

Two texts stay in code: the emergency lines (never editable: they must always say 911) and the
read-back card, whose exact shape the booking guard recognises in the history (`confirm_card` /
`parse_card`).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from datetime import time as clock_time
from typing import Any

from ..config import Settings, get_settings

PREFIX = "sms_agent."
KEYS = ("intro", "new_person_intro", "booked", "front_desk", "english_only", "text_only",
        "unknown_number", "cancel_visit", "booking_problem", "reply_limit")

# The same drafts as migration 054 (kept equal by tests/test_sms_agent_texts.py).
DRAFTS = {
    "intro": (
        "Hi {first_name}, this is Elliot from Rausch Physical Therapy! We have your referral and "
        "would love to get your first visit on the calendar.\n\n"
        "I can book it right here by text. Just tell me which days or times work for you.\n\n"
        "Prefer to book yourself? {booking_link}\n"
        "Or call our front desk: {front_desk_phone}"
    ),
    "new_person_intro": (
        "Hi, this is Elliot from Rausch Physical Therapy! I can help you book your first visit "
        "right here by text.\n\n"
        "Prefer to book yourself? {booking_link}\n"
        "Or call our front desk: {front_desk_phone}"
    ),
    "booked": (
        "You're all set, {first_name}! {day} at {time} with {therapist} at our {clinic} office "
        "({address}).\n\n"
        "Your new patient paperwork will be sent over via email, please complete this prior to your "
        "visit. Just bring a photo ID.\n\n"
        "See you then!"
    ),
    "front_desk": (
        "That's one for our front desk team. You can call them at {front_desk_phone}, or they'll "
        "reach out to you as soon as they're available."
    ),
    "english_only": (
        "Sorry, I can only help in English by text. Our front desk can help you at "
        "{front_desk_phone}, or they'll reach out to you."
    ),
    "text_only": "Sorry, I can only read text messages. Could you type that out for me?",
    # Migration 055 (Yajat's test, 2026-11-05: "no cancel it" got the general front desk text, which
    # never said the visit is still booked).
    "cancel_visit": (
        "Got it, I've passed that to our front desk. Your visit stays booked until they confirm the "
        "change with you. You can also call them at {front_desk_phone}."
    ),
    # Migration 057 (Emma's test, 2026-11-11: a failed booking sent the front desk text and Elliot's
    # own "I couldn't finish booking that by text..." at once): one text for every failed booking.
    "booking_problem": (
        "I couldn't finish booking that by text, so our front desk will finish setting up your "
        "visit. You can also call them at {front_desk_phone}."
    ),
    # Migration 057 (Emma's test, 2026-11-11: at the daily reply limit she got no answer at all).
    "reply_limit": (
        "I've passed your messages to our front desk. They'll reach out to you, or you can call "
        "them at {front_desk_phone}."
    ),
    "unknown_number": (
        "Hi, this is Rausch Physical Therapy! You can book your first visit online here: "
        "{booking_link}\n"
        "Or call our front desk: {front_desk_phone}"
    ),
}

# In code on purpose: an emergency answer must never be edited away (plan section 6). Two texts
# (Yajat's test, 2026-11-05: chest pain got the 988 line too): a medical emergency gets only 911;
# thoughts of suicide or self-harm also get 988, the Suicide and Crisis Lifeline (owner, 2026-11-02).
EMERGENCY_TEXT = "Please call 911 right now if this is an emergency. Our team has been alerted too."
SELF_HARM_TEXT = (
    "If you're in danger, please call 911 right now. You can also call or text 988 (Suicide and "
    "Crisis Lifeline) any time. Our team has been alerted too."
)
EMERGENCY_KINDS = {"medical": EMERGENCY_TEXT, "self_harm": SELF_HARM_TEXT}
# The one text sent before 2026-11-07: still an emergency text in a chat's history.
_OLD_EMERGENCY_TEXT = (
    "If this is a medical emergency, please call 911 right away. If you're thinking about harming "
    "yourself, call or text 988 any time. Our front desk team will also reach out to you."
)


def emergency_text(kind: str | None) -> str:
    """The text for flag_emergency's kind; anything unknown gets the self-harm text (it says 911
    first, and 988 does no harm)."""
    return EMERGENCY_KINDS.get(str(kind or "").strip().lower(), SELF_HARM_TEXT)


def is_emergency_text(text: str | None) -> bool:
    return (text or "").strip() in (EMERGENCY_TEXT, SELF_HARM_TEXT, _OLD_EMERGENCY_TEXT)

# The three approved office addresses (voice prompt, "Clinic facts"), written for text.
CLINIC_ADDRESSES = {
    "dana point": "24582 Del Prado Avenue, Suite C, Dana Point",
    "laguna niguel": "30100 Town Center Drive, Suite YZ, Laguna Niguel",
    "mission viejo": "24000 Alicia Parkway, Suite 27, Mission Viejo",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def address_for(clinic: str | None) -> str:
    words = " ".join(re.sub(r"[^a-z]", " ", str(clinic or "").lower()).split())
    return next((value for key, value in CLINIC_ADDRESSES.items() if key in words), "")


def fill(body: str, values: dict[str, Any]) -> str:
    """Put the values into {placeholders}. An unknown placeholder (a Studio typo) stays as typed:
    never an exception, so a patient still gets the text."""
    def one(match: re.Match) -> str:
        value = values.get(match.group(1))
        return match.group(0) if value is None else str(value)
    return _PLACEHOLDER.sub(one, body)


def template_body(conn, key: str, practice_id: int | None = None) -> str:
    """The active Studio text for `key` (without the prefix), else the draft in code."""
    if key not in DRAFTS:
        raise KeyError(f"unknown SMS agent text: {key}")
    if conn is not None:
        row = conn.execute(
            "select body from message_templates where key=%s and cadence_step_id is null "
            "and is_active and channel='sms' and (%s::bigint is null or practice_id=%s) "
            "order by updated_at desc,id desc limit 1",
            (PREFIX + key, practice_id, practice_id),
        ).fetchone()
        if row and str(row["body"] or "").strip():
            return str(row["body"])
    return DRAFTS[key]


def booking_link(conn, practice_id: int | None = None) -> str | None:
    if conn is None:
        return None
    row = conn.execute(
        "select booking_link_url from practice_settings where (%s::bigint is null or practice_id=%s) "
        "and coalesce(booking_link_url,'')<>'' order by practice_id limit 1",
        (practice_id, practice_id),
    ).fetchone()
    return row["booking_link_url"] if row else None


def render(conn_or_none, key: str, *, practice_id: int | None = None,
           settings: Settings | None = None, **values: Any) -> str:
    """One editable text, ready to send. The booking link and front desk phone are filled in
    unless given; a value that is None leaves its placeholder as is."""
    settings = settings or get_settings()
    defaults: dict[str, Any] = {"front_desk_phone": settings.clinic_front_desk_phone}
    if "booking_link" not in values:
        defaults["booking_link"] = booking_link(conn_or_none, practice_id)
    if "address" not in values and values.get("clinic"):
        defaults["address"] = address_for(values["clinic"]) or None
    body = template_body(conn_or_none, key, practice_id)
    text = fill(body, {**defaults, **values})
    # An empty first name ("Hi , this is ...") reads badly: drop the gap.
    return text.replace("Hi , ", "Hi, ").replace(" , ", ", ").replace("set, !", "set!")


# --- the read-back card ---------------------------------------------------------------------------

# Owner, 2026-11-04: no "Great choice!" (Yajat's test). Cards sent before still read as cards.
CARD_FIRST_LINE = "Here's your visit:"
CARD_LAST_LINE = "Reply YES and I'll book it."
_OLD_CARD_OPENER = "Great choice! "


def card_day(value: date) -> str:
    """"Tue Nov 3": short, and the same in the card and in the guard."""
    return f"{value:%a} {value:%b} {value.day}"


def card_date(day: str, today: date) -> date | None:
    """The date a card's "Mon Jul 20" means: weekday, month and day fit, the year nearest `today`
    (the demo calendar is pinned to July while the real date is later)."""
    fits = []
    for year in (today.year - 1, today.year, today.year + 1):
        for month in range(1, 13):
            for number in range(1, 32):
                try:
                    value = date(year, month, number)
                except ValueError:
                    break
                if card_day(value) == day:
                    fits.append(value)
    return min(fits, key=lambda value: abs((value - today).days), default=None)


def card_time(value: clock_time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def confirm_card(day: date, time: clock_time, therapist: str, clinic: str,
                 service: str | None = None) -> str:
    """The read-back card (plan section 5). Built in code so the booking guard can recognise it.
    `service`: its own line when it is not the practice's default (a new person's Pelvic Health),
    so the patient says yes to the service too."""
    service_line = f"{service.strip()}\n" if service and service.strip() else ""
    return (f"{CARD_FIRST_LINE}\n{card_day(day)} at {card_time(time)} with {therapist}\n"
            f"{service_line}{clinic} office\n{CARD_LAST_LINE}")


_CARD = re.compile(
    r"^(?:" + re.escape(_OLD_CARD_OPENER) + r")?" + re.escape(CARD_FIRST_LINE) + r"\n(?P<day>\w{3} \w{3} \d{1,2}) at "
    r"(?P<time>\d{1,2}:\d{2} [AP]M) with (?P<therapist>[^\n]+)\n(?:(?P<service>[^\n]+)\n)?"
    r"(?P<clinic>[^\n]+) office\n" + re.escape(CARD_LAST_LINE) + r"$"
)


def parse_card(text: str | None) -> dict[str, str | None] | None:
    """day / time / therapist / service / clinic of a card text (service None = the default), or
    None if it is not exactly a card."""
    match = _CARD.match((text or "").strip())
    return match.groupdict() if match else None


# The date of birth question, sent by code with the picked time (owner, 2026-11-04: the booking steps
# are code, not the model's choice), so the next turn can send the card for that time itself.
BIRTHDAY_QUESTION_END = "to confirm it's you, what's your date of birth?"


def birthday_question(day: date, time: clock_time, therapist: str, clinic: str,
                      service: str | None = None) -> str:
    """"For Mon Jul 20 at 5:45 PM with Alan Rome at our Dana Point office, to confirm it's you,
    what's your date of birth?" (`service`: named when it is not the default, like the card)."""
    service_part = f" ({service.strip()})" if service and service.strip() else ""
    return (f"For {card_day(day)} at {card_time(time)} with {therapist}{service_part} at our "
            f"{clinic} office, {BIRTHDAY_QUESTION_END}")


_BIRTHDAY_QUESTION = re.compile(
    r"^For (?P<day>\w{3} \w{3} \d{1,2}) at (?P<time>\d{1,2}:\d{2} [AP]M) with "
    r"(?P<therapist>[^\n(]+?)(?: \((?P<service>[^)\n]+)\))? at our (?P<clinic>[^\n]+?) office, "
    + re.escape(BIRTHDAY_QUESTION_END) + r"$"
)


def parse_birthday_question(text: str | None) -> dict[str, str | None] | None:
    """day / time / therapist / service / clinic of our date of birth question, or None."""
    match = _BIRTHDAY_QUESTION.match((text or "").strip())
    return match.groupdict() if match else None


def long_day(value: date | datetime) -> str:
    """"Tuesday, November 3" for the booked text."""
    return f"{value:%A}, {value:%B} {value.day}"
