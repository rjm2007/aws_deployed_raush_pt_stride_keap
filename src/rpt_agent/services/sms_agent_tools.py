"""The SMS agent's tools: live (a real lead, real booking) and sandbox (the test chat, writes nothing).

Names and parameters are fixed by docs/SMS_AGENT.md and config/sms_agent_tools.json. Every reply
starts with a code word (OPENINGS, CARD_SENT, BOOKED, TAKEN, ...) like the voice tools, so the prompt
can branch on it. The lead always comes from the job or the sandbox setup, never from the model.

Texts the patient must get word for word (the read-back card, "booked", front desk, intro, the 911
line) are returned as `fixed_texts`: the agent loop sends exactly those (the model may add one short text
of its own), so the model can never reword a booking confirmation or a hand-off.

Booking guard (owner, 2026-11-02): book_visit is refused unless Elliot's last text was the read-back
card for that same date, time and therapist and the patient has replied since, in words (a photo
alone is no yes), and that reply, when it is their only text since the card, is not a clear no
("no", "nope", "nah", "not", "wait", "later", "cancel", "change"). A model that skips the card
cannot book; the yes itself stays the model's judgment, the no-word check is only a cheap backstop.

Any exception in a tool goes back to the model as an ERROR reply (category logged only), so a
database hiccup in a hand-off after a booking in the same reply never loses the booked text.

Repeats (gpt-5.5 eval, 2026-11-02): a hand-off for the same reason whose card is still open (flagged
within 24 h), a case change still open and an emergency flagged in the last hour open no second card and send no
fixed text again; the tool replies ALREADY_HANDED_OFF / ALREADY_REPORTED / ALREADY_SENT and the model
answers the new text briefly (or NO_REPLY for a thank-you). Live reads `lead_reviews`; the test chat
reads what Elliot did earlier in the chat.

Service (2026-11-02): find_times, confirm_visit and book_visit take an optional `service` (a new
person's service). For a patient with a referral another service is a case change (CASE_CHANGE, never
booked by text). The card names the service when it is not the practice's default. A therapist name
that matches no clinician is matched by one of its words only when exactly one clinician fits, and
the card always prints the clinician's real name.

Round 2 of the gpt-5.5 eval (2026-11-02): "tmrw aftrnoon" searched as today is searched as tomorrow
(`search_when`); a lead with no first name on file is booked only under a first name the patient
wrote in this chat, never their last name (`booking_names`, MISSING_NAME), and the booked text never
greets them by a name they did not give; a service no therapist offers at the chosen clinic names the
clinics that do offer it (NO_CLINICIANS ... Offered at: ...).

Birthday step (owner, 2026-11-03; a test chat looped "send your date of birth with the year" and the
same card because the booking refused a birthday after the demo's pinned calendar day): the patient
types their date of birth, `check_birthday` reads and compares it in code (sms_agent_birthday), and
confirm_visit needs a date of birth confirmed in the chat (NEED_BIRTH_DATE otherwise); book_visit books
with that date. The same card is never sent twice in a row (CARD_ALREADY_SENT), and a second failed
booking for the same reason in a chat (BAD_DATE_OF_BIRTH, NOT_CONFIRMED, MISSING_NAME, ...) hands off
to the front desk (booking_problem) instead of asking again.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from ..config import Settings, get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from . import slot_booking
from . import sms_agent_birthday as birthday
from . import sms_agent_texts as texts
from .lead_review import SMS_FRONT_DESK_CODE, SMS_FRONT_DESK_REASONS
from .lead_status import (
    CASE_CHANGE_CODE,
    SMS_AGENT_SOURCE,
    explicit_opt_out,
    record_status,
    report_lead_status,
)
from .review import FRONT_DESK_BOOKABLE_REASONS, flag_lead_for_review, record_review
from .sms_agent_context import MEDIA_ONLY, LeadView, Turn

REPORT_STATUSES = ("callback_scheduled", "booking_link", "declined", "do_not_contact",
                   "wrong_person", "case_change")
HAND_OFF_REASONS = ("insurance", "different_case", "wants_person", "other_language",
                    "unknown_question", "booking_problem", "other", "cancel_visit")
# Tool name -> AgentAction kind (shown in the test chat's side panel).
KINDS = {
    "find_times": "find_times", "list_therapists": "list_therapists",
    "confirm_visit": "confirm_visit", "book_visit": "book", "report_outcome": "report",
    "hand_off": "hand_off", "flag_emergency": "emergency", "send_intro": "fixed_text",
    "check_birthday": "check_birthday",
}
# book_visit refusals that come back when the model asks again (2026-11-03 test chat: "send your date
# of birth with the year" and the same card, over and over). The second one for the same reason in a
# chat hands off to the front desk instead (booking_problem).
REPEATED_FAILURES = frozenset({"BAD_DATE_OF_BIRTH", "NOT_CONFIRMED", "MISSING_NAME", "BAD_NAME",
                               "BAD_EMAIL"})
FAILURE_LIMIT = 2
FAILURE_WINDOW = timedelta(hours=24)
# Booking replies after which the patient is told the front desk will take care of it (the booking
# engine already sent the lead to Needs Review).
FRONT_DESK_CODES = {"FOLLOW_UP", "UNCONFIRMED", "PATIENT_EXISTS"}
FINAL_STATUSES = {"booked", "declined", "do_not_contact", "closed_no_response"}
# report_outcome do_not_contact notes the tool description fixes for "no contact at all" (calls
# too). Anything else is "stop texts only": the patient's words decide, never a guess.
ALL_CONTACT_NOTE = "no contact at all"
# Repeats (gpt-5.5 eval, 2026-11-02: the same hand-off text went out 2-4 times in one chat, "thanks,
# I'll wait" got a second hand-off). Within these windows the tool does not act again.
HAND_OFF_REPEAT_WINDOW = timedelta(hours=24)
EMERGENCY_REPEAT_WINDOW = timedelta(hours=1)
ALREADY_HANDED_OFF = ("ALREADY_HANDED_OFF: the front desk already has this; no new card was opened and "
                      "no text was sent. Answer the new text briefly, or NO_REPLY for thanks.")
# Yajat's test, 2026-11-05: "can I talk to the front desk" after a hand-off got "Our front desk
# already has your request" without the number (1 in 3 replays). Asking how to reach them gets it.
ALREADY_HANDED_OFF_PHONE = (" If they ask to reach or talk to the front desk, say they will reach out "
                            "and give the number: {phone}.")
ALREADY_REPORTED = ("ALREADY_REPORTED: the front desk already has this case change; nothing was "
                    "changed and no text was sent. Answer the new text briefly, or NO_REPLY for "
                    "thanks.")
# flag_emergency's kind, as the card's note for staff.
EMERGENCY_NOTES = {"medical": "Medical emergency", "self_harm": "Thoughts of suicide or self-harm"}
ALREADY_SENT = ("ALREADY_SENT: the emergency text was sent within the last hour and staff were "
                "flagged; it was not sent again. Write one short, kind line (no 911 text again).")


def wants_no_contact_at_all(notes: str | None) -> bool:
    return ALL_CONTACT_NOTE in " ".join(str(notes or "").lower().split())


# A reply to the read-back card that is clearly not a yes (owner, 2026-11-02): "hmm not sure, maybe
# later" passed the guard and only the model stood between it and a real booking. Whole words only;
# "no problem" and the like are a yes, so they are taken out first. A false "no" costs one more
# card; a false yes books a visit the patient did not want. "nope" and "nah" are the same no in
# texting ("Nope, another day" got past the plain "no").
_CLEAR_NO = re.compile(r"\b(no|nope|nah|not|wait|later|cancel|change)\b")
_NOT_A_NO = re.compile(r"\b(no problem|not a problem|no worries|no prob)\b")


def clear_no(text: str) -> bool:
    words = " ".join(str(text or "").lower().replace("\u2019", "'").split())
    return bool(_CLEAR_NO.search(_NOT_A_NO.sub(" ", words)))


def last_elliot_text(history: list[Turn]) -> str:
    return next((turn.text for turn in reversed(history) if turn.role == "elliot"), "")


def latest_patient_text(history: list[Turn]) -> str:
    return next((turn.text for turn in reversed(history) if turn.role == "patient"), "")


# book-12 (round 2): "yo can i get in tmrw aftrnoon" was searched as when="today" although Context
# spells out Tomorrow. Whole words, any case; a text that also says "today" is left to the model.
_TOMORROW = re.compile(r"\b(?:tomorrow|tmrw|tmr|tmw|tomoro|2morrow)\b", re.IGNORECASE)
_TODAY = re.compile(r"\btoday\b", re.IGNORECASE)


def search_when(when: str, history: list[Turn], specific_date: str | None = None,
                todays: frozenset[date] | set[date] = frozenset()) -> str:
    """`when`, except a search for today becomes "tomorrow" when the patient's latest text says
    tomorrow (and not today). A search for today is when="today", or (eval 2026-11-03, book-12:
    "tmrw aftrnoon" searched as when="specific_date" with today's date) when="specific_date" with a
    date in `todays` (the calendar's today as the model sees it, and the demo's pinned one)."""
    text = latest_patient_text(history)
    wanted = str(when or "").strip().lower()
    for_today = wanted == "today" or (
        wanted == "specific_date" and _parse_iso(specific_date) in todays)
    if for_today and _TOMORROW.search(text) and not _TODAY.search(text):
        return "tomorrow"
    return when


def _parse_iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


# book-12 (round 3): search_when turned "today" into "tomorrow", but the reply did not say which day
# was searched, so the model told the patient "nothing this afternoon". A today / tomorrow search
# now names its day right after the code word. The day comes from the reply's own "Calendar today"
# note (slot_booking._today_note: always there for these searches, and on the demo's pinned
# calendar too), so no second database read.
DAY_SEARCHES = ("today", "tomorrow")
_CALENDAR_TODAY = re.compile(r"Calendar today: (\w+, \w+ \d{1,2}, \d{4})\.")


def calendar_today(output: str) -> date | None:
    match = _CALENDAR_TODAY.search(output or "")
    if not match:
        return None
    try:
        # A calendar day, no clock: the zone only satisfies the naive-datetime lint.
        return datetime.strptime(match.group(1), "%A, %B %d, %Y").replace(tzinfo=UTC).date()
    except ValueError:
        return None


_PINNED_DAY = re.compile(r"on the booking calendar: (\w+, \w+ \d{1,2}, \d{4})\)")


def pinned_search_day(output: str) -> date | None:
    match = _PINNED_DAY.search(output or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%A, %B %d, %Y").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def name_search_day(output: str, *, asked: str, searched: str) -> str:
    """An OPENINGS / NOTHING_IN_RANGE reply to a today or tomorrow search, with the day searched
    named first: "NOTHING_IN_RANGE: Searched tomorrow, Thu Nov 5 (the patient said tomorrow). ..."
    The code word stays first: the prompt and the side panel branch on it."""
    code = (re.match(r"[A-Z][A-Z_]*", output or "") or [""])[0]
    if searched not in DAY_SEARCHES or code not in ("OPENINGS", "NOTHING_IN_RANGE"):
        return output
    today = calendar_today(output)
    day = today + timedelta(days=1 if searched == "tomorrow" else 0) if today else None
    # The demo's pinned calendar moves today / tomorrow to the same weekday (slot_booking.
    # pinned_today_or_tomorrow): the reply names that day, which is the one searched.
    day = pinned_search_day(output) or day
    label = f"Searched {searched}" + (f", {texts.card_day(day)}" if day else "")
    if asked != searched:
        label += f" (the patient said {searched})"
    named = f"\"{texts.card_day(day)}\"" if day else f"\"{searched}\""
    note = (f"{label}. Name that day in your text ({named})"
            + ("; never \"today\" or \"this afternoon\" for it." if searched != "today" else ".")
            + " ")
    rest = output[len(code):].lstrip(": ")
    if code == "OPENINGS":
        rest = f"Openings {rest}"
    return f"{code}: {note}{rest}"


def said_in_chat(history: list[Turn], name: str | None) -> bool:
    """The patient wrote this name (whole words, any case) in one of their texts."""
    words = str(name or "").split()
    if not words:
        return False
    pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)",
                         re.IGNORECASE)
    return any(turn.role == "patient" and pattern.search(turn.text) for turn in history)


# x-04 (round 3): after "What's your first name?" the model also asked for the last name (on file)
# and then promised the front desk instead of sending the card again.
MISSING_FIRST_NAME = ("MISSING_NAME: not booked: we have no first name on file for this patient and "
                      "they have not written it in this chat. Ask for their first name only (the "
                      "last name is on file: never ask for it). Once they give it, call "
                      "confirm_visit again for the same time in that turn (the card goes out "
                      "again) and book_visit after their yes, with that first_name.")


# Eval 2026-11-03 (x-04): MISSING_NAME came from book_visit after the yes to the card, so the model
# asked "what's your last name?" (on file), sent the card again and passed the last name as
# first_name. Now the first name is asked BEFORE the card: confirm_visit replies MISSING_NAME until a
# first name was given in the chat.
MISSING_FIRST_NAME_CARD = ("MISSING_NAME: no card was sent: we have no first name on file and they "
                           "have not written it in this chat. Ask for their first name only (\"What's "
                           "your first name?\"; the last name is on file: never ask for it). With "
                           "their answer, call confirm_visit again for the same time with "
                           "first_name.")
# Elliot's question for the first name, and a short answer to it ("Dana", "it's Dana", "Dana Quill").
_ASKED_FIRST_NAME = re.compile(r"\bfirst name\b", re.IGNORECASE)
_NAME_ANSWER = re.compile(r"^(?:(?:it'?s|its|i'?m|im|this is|my (?:first )?name is|first name is|"
                          r"name'?s)\s+)?(?P<first>[a-z][a-z'-]*)(?:\s+(?P<second>[a-z][a-z'-]*))?"
                          r"[.!]?$", re.IGNORECASE)
_NOT_A_NAME = re.compile(r"^(?:y|ya|ye|yes|yeah|yep|yup|no|nope|nah|ok|okay|k|sure|thanks|thank|thx|"
                         r"hi|hey|hello|what|why|who|wait|sorry|idk|stop)$", re.IGNORECASE)


def first_name_in_chat(lead: LeadView, history: list[Turn], given: str | None = None) -> str:
    """A first name for a lead with none on file, from this chat: the model's `given` when the
    patient wrote it (whole words) and it is not the last name; else the patient's short answer
    right after Elliot asked for their first name. "" when there is none."""
    last = (lead.last_name or "").strip().lower()
    name = " ".join(str(given or "").split())
    if name and said_in_chat(history, name) and name.lower() != last:
        return name
    found = ""
    asked = False
    for turn in history:
        if turn.role != "patient":
            asked = turn.role == "elliot" and bool(_ASKED_FIRST_NAME.search(turn.text))
            continue
        match = _NAME_ANSWER.match(" ".join(turn.text.replace("\u2019", "'").split())) if asked else None
        asked = False
        if not match:
            continue
        first, second = match["first"], match["second"]
        if second and second.lower() != last:
            continue  # two words that are not first + the last name on file: not just a name
        if first.lower() == last or _NOT_A_NAME.match(first):
            continue
        found = first if any(ch.isupper() for ch in first) else first.capitalize()
    return found


def booking_names(lead: LeadView, history: list[Turn], first_name: str | None,
                  last_name: str | None) -> tuple[str, str]:
    """The names a booking uses (x-04, round 2: a lead with no first name on file was booked with
    its last name as the first name and greeted "You're all set, Quill!").

    An existing lead keeps the names on file. With no first name on file, the model's first_name is
    taken only when the patient wrote it in this chat and it is not the last name; otherwise
    MISSING_NAME. A new person gives both names in the chat (validate_patient checks them)."""
    if lead.is_new_person:
        return ((first_name or lead.first_name or "").strip(),
                (last_name or lead.last_name or "").strip())
    last = (lead.last_name or "").strip() or (last_name or "").strip()
    first = (lead.first_name or "").strip()
    if not first:
        first = first_name_in_chat(lead, history, first_name)
    if not first:
        raise ValueError(MISSING_FIRST_NAME)
    return first, last


def greeting_name(lead: LeadView, history: list[Turn], first_name: str) -> str:
    """The name in the booked text: the one on file, or one the patient wrote; else none
    ("You're all set!")."""
    on_file = (lead.first_name or "").strip()
    if on_file:
        return on_file
    return first_name if said_in_chat(history, first_name) else ""


# The booking engine's reply when no therapist sees that service at that clinic (np-03, round 2:
# a new person asked for pelvic at Dana Point and got only the front desk).
_NO_CLINICIANS = re.compile(r"^NO_CLINICIANS: no therapist is set up for (?P<service>.+?) at "
                            r"(?P<clinic>.+?)\. ")
OFFERED_AT_SQL = """
select bl.name from booking_locations bl
where bl.is_active and (%(practice)s::bigint is null or bl.practice_id=%(practice)s)
  and exists(select 1 from clinician_assignments ca
             join clinicians c on c.id=ca.clinician_id and c.is_active
             join case_types ct on ct.id=ca.case_type_id and ct.is_active
            where ca.is_active and ca.booking_location_id=bl.id
              and ct.practice_id=bl.practice_id and lower(ct.name)=lower(%(service)s))
order by bl.name
"""


def offered_elsewhere(message: str, clinics: list[str]) -> str:
    """NO_CLINICIANS that names the clinics that do offer the service."""
    match = _NO_CLINICIANS.match(message)
    if not match:
        return message
    service, clinic = match["service"], match["clinic"]
    others = [name for name in clinics if name.strip().lower() != clinic.strip().lower()]
    if not others:
        return (f"NO_CLINICIANS: no {service} therapist at {clinic}, and no other clinic offers it. "
                "Offer to text the booking link or hand off to the front desk.")
    return (f"NO_CLINICIANS: no {service} therapist at {clinic}. Offered at: {', '.join(others)}. "
            "Ask if one of those works.")


@dataclass
class ToolResult:
    output: str                       # what the model reads (starts with a code word)
    kind: str
    summary: str                      # plain words for the test chat / Activity; no patient data
    fixed_texts: list[str] = field(default_factory=list)  # sent word for word; ends the run
    send_last: bool = False           # the read-back card goes after the model's own text
    opted_out: bool = False           # this call opted the patient out (live do_not_contact)
    # The side panel's code when it must say more than the model's code word: which outcome was
    # reported (REPORTED_CASE_CHANGE), a hand-off that stops booking (HANDED_OFF_PAUSED). The test
    # chat sends these back as prior_actions, so a later turn knows what Elliot did (stateless).
    action_code: str | None = None
    # A hand-off's reason (insurance, different_case, ...): the test chat sends it back with the
    # code, so a later turn tells a repeat of the same hand-off from a new one (x-02).
    reason: str | None = None

    @property
    def code(self) -> str:
        match = re.match(r"[A-Z][A-Z_]*", self.output or "")
        return match.group(0) if match else "ERROR"


class AgentTools(Protocol):
    def find_times(self, *, when: str, specific_date: str | None = None, month: str | None = None,
                   time_of_day: str | None = None, therapist: str | None = None,
                   clinic: str | None = None, service: str | None = None) -> ToolResult: ...
    def list_therapists(self, *, clinic: str | None = None) -> ToolResult: ...
    def confirm_visit(self, *, date: str, time: str, therapist: str | None = None,
                      clinic: str | None = None, service: str | None = None,
                      first_name: str | None = None) -> ToolResult: ...
    def book_visit(self, *, date: str, time: str, therapist: str | None = None,
                   first_name: str | None = None, last_name: str | None = None,
                   date_of_birth: str | None = None, clinic: str | None = None,
                   service: str | None = None) -> ToolResult: ...
    def report_outcome(self, *, status: str, notes: str | None = None,
                       callback_type: str | None = None, delay_minutes: Any = None,
                       callback_datetime_iso: str | None = None) -> ToolResult: ...
    def hand_off(self, *, reason: str, notes: str | None = None) -> ToolResult: ...
    def flag_emergency(self, *, kind: str | None = None) -> ToolResult: ...
    def send_intro(self) -> ToolResult: ...
    def check_birthday(self, *, date_of_birth: str) -> ToolResult: ...


# --- dispatch --------------------------------------------------------------------------------------

def call_tool(tools: AgentTools, name: str, arguments: dict[str, Any] | None,
              trace: WorkflowTrace | None = None) -> ToolResult:
    """Run one tool call from the model. Unknown tools and arguments never reach our code; a
    ValueError (bad date, unknown clinic, ...) goes back to the model as the reply, like voice."""
    kind = KINDS.get(name)
    method: Callable[..., ToolResult] | None = getattr(tools, name, None) if kind else None
    if method is None:
        return ToolResult(f"UNKNOWN_TOOL: there is no tool called {name}.", "unknown", "Unknown tool")
    accepted = set(inspect.signature(method).parameters)
    args = {key: value for key, value in (arguments or {}).items()
            if key in accepted and value not in (None, "")}
    try:
        inspect.signature(method).bind(**args)
    except TypeError:
        return ToolResult(f"MISSING_ARGUMENT: {name} needs more details. Check the parameters.",
                          kind, f"{name}: missing details")
    try:
        result = method(**args)
    except (ValueError, slot_booking.BookingToolError) as exc:
        result = ToolResult(str(exc), kind, f"{name}: {str(exc).split(':', 1)[0]}")
    except Exception as exc:  # noqa: BLE001 - a booked text already made in this reply must go out
        # A database error in hand_off or report_outcome after book_visit in the same reply escaped
        # run_agent, and the patient never got the booked text for a visit that was made. Category
        # only in the log: the message may hold patient data.
        log = trace if trace is not None else getattr(tools, "trace", None)
        if log is not None:
            log.log("sms_agent_tool_error", tool=name, error_category=type(exc).__name__)
        result = ToolResult(f"ERROR: {name} did not work on our side; nothing was changed by it. "
                            "Do not say it worked; our team will follow up.", kind,
                            f"{name}: error on our side")
    if trace is not None:
        # Tool and result code only: never arguments (names, birthday) or text.
        trace.log("sms_agent_tool", tool=name, result_code=result.code)
    return result


# --- shared rules ----------------------------------------------------------------------------------

def _parse_day(value: str | None, label: str) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text if label == "date" else f"{text}-01")
    except ValueError:
        fmt = "YYYY-MM-DD" if label == "date" else "YYYY-MM"
        raise ValueError(f"BAD_DATE: send {label} as {fmt}.") from None


def card_guard(history: list[Turn], *, day: str, time: str, therapist: str | None,
               clinic: str | None) -> tuple[dict[str, Any] | None, str]:
    """(the card being answered, "") when booking is allowed, else (None, why not)."""
    last = max((index for index, turn in enumerate(history) if turn.role == "elliot"), default=None)
    if last is None:
        return None, "Elliot has not sent the read-back card yet"
    card = texts.parse_card(history[last].text)
    if card is None:
        return None, "Elliot's last text is not the read-back card"
    replies = [turn.text.strip() for turn in history[last + 1:]
               if turn.role == "patient" and turn.text.strip()]
    if not replies:
        return None, "the patient has not answered the read-back card yet"
    # A photo or file alone (sms_agent_context.MEDIA_ONLY) is no yes, whatever the model reads into
    # it.
    words = [text for text in replies if text != MEDIA_ONLY]
    if not words:
        return None, "the patient has not answered the read-back card in words"
    if len(words) == 1 and clear_no(words[0]):
        return None, "the patient's answer to the read-back card is not a clear yes"
    wanted_day = _parse_day(day, "date")
    wanted_time = slot_booking.parse_clock(time)
    if wanted_day is None or texts.card_day(wanted_day) != card["day"]:
        return None, "the date is not the one on the read-back card"
    if texts.card_time(wanted_time) != card["time"]:
        return None, "the time is not the one on the read-back card"
    if therapist and not same_therapist(card["therapist"], therapist):
        return None, "the therapist is not the one on the read-back card"
    if clinic and clinic.strip().lower() not in card["clinic"].lower():
        return None, "the clinic is not the one on the read-back card"
    return {**card, "date": wanted_day, "clock": wanted_time}, ""


def same_therapist(on_card: str, named: str) -> bool:
    """The model's therapist is the card's: every word matches, or (a wrong surname, "Alan Rome" for
    "Alan Brooks", gpt-5.5 eval) one of its words does. The booking always uses the card's therapist,
    the one the patient said yes to; this only catches a model that means someone else."""
    one = [{"display_name": on_card}]
    if slot_booking.match_clinicians(one, named):
        return True
    return any(slot_booking.match_clinicians(one, word) for word in str(named or "").split())


def resolve_therapist(context: slot_booking.BookingContext, named: str | None) -> str | None:
    """The clinician the model means, by their real name (None = any therapist).

    A name that matches as given is kept (slot_booking matches it). Otherwise one of its words must
    fit exactly one clinician at that clinic for that service ("Alan Rome" -> "Alan Brooks");
    none or several is a ValueError the model reads, never a guess."""
    if not named or not str(named).strip():
        return None
    clinicians = context.clinicians
    if slot_booking.match_clinicians(clinicians, named):
        return named
    fits = {clinician["display_name"]
            for word in str(named).split()
            for clinician in slot_booking.match_clinicians(clinicians, word)}
    if len(fits) == 1:
        return fits.pop()
    if fits:
        raise ValueError(f"WHICH_THERAPIST: {named} could be {', '.join(sorted(fits))}. Ask the "
                         "patient which one.")
    names = ", ".join(clinician["display_name"] for clinician in clinicians)
    raise ValueError(
        f"NO_SUCH_THERAPIST: no therapist named {named} sees {context.case_type['name']} patients "
        f"at {context.location['name']}. Therapists there: {names}.")


def closest_therapist(context: slot_booking.BookingContext, named: str) -> str:
    """resolve_therapist, or the name as given when it fits no one or several (the search's own
    NO_SUCH_THERAPIST reply then lists the therapists)."""
    try:
        return resolve_therapist(context, named) or named
    except ValueError:
        return named


def default_service(conn, context: slot_booking.BookingContext) -> str:
    """The practice's default service (the card names a service only when it is another one)."""
    row = conn.execute(
        "select name from case_types where practice_id=%s and is_active and is_default "
        "order by id limit 1", (context.lead.get("practice_id"),),
    ).fetchone()
    return row["name"] if row else ""


def reported_code(status: str) -> str:
    """The side panel's code for a recorded outcome (ToolResult.action_code)."""
    return f"REPORTED_{status.upper()}"


def hand_off_code(reason: str) -> str:
    """HANDED_OFF_PAUSED when the card stops booking by text (a different case, a booking problem),
    else HANDED_OFF (the front desk calls; booking goes on)."""
    return "HANDED_OFF" if reason in FRONT_DESK_BOOKABLE_REASONS else "HANDED_OFF_PAUSED"


# confirm_visit before a date of birth is confirmed: code texts the question naming the time
# (2026-11-04); the model adds nothing.
BIRTH_DATE_ASKED = ("NEED_BIRTH_DATE: no card yet: our date of birth question for this time was texted "
                    "to the patient word for word. Add nothing (NO_REPLY): never ask for the date of "
                    "birth in your own words.")
BIRTH_DATE_ASKED_AGAIN = ("NEED_BIRTH_DATE: no card yet: our date of birth question for this time is "
                          "already your latest text and their answer is not a full date of birth. "
                          "Ask once to type it again with the year, like 3/14/1988.")
PENDING_BIRTH_DATE = ("NEED_BIRTH_DATE: no card was sent: the date of birth they typed is not the one "
                      "in our records. Ask them once to double-check it (\"Could you double-check "
                      "that date of birth for me?\"), never saying the date on file; then "
                      "check_birthday with their answer.")
CARD_ALREADY_SENT = ("CARD_ALREADY_SENT: this same card is already your latest text, so it was not "
                     "sent again. A clear yes = book_visit now. For a reaction, 'maybe' or a "
                     "question, answer in one short line (no card, no 'reply YES'), or NO_REPLY for a "
                     "reaction alone.")
NOT_CONFIRMED = ("NOT_CONFIRMED: not booked: {why}. Call confirm_visit for the time the patient "
                 "chose (it texts them the read-back card), and call book_visit only after they reply "
                 "yes to that card.")


def _summary_when(day: date, clock) -> str:
    return f"{texts.card_day(day)} {texts.card_time(clock)}"


class _BaseTools:
    """What live and sandbox share: the guard, the fixed texts, argument checks."""

    pretend = False

    def __init__(self, trace: WorkflowTrace, *, lead: LeadView, history: list[Turn],
                 settings: Settings | None = None, practice_id: int | None = None,
                 today: date | None = None, birthday_history: list[Turn] | None = None):
        self.trace = trace
        # The birthday step reads further back than the model (a date of birth given more than 20
        # texts ago is still never asked again); None = `history`.
        self.birthday_history = history if birthday_history is None else birthday_history
        # The REAL clinic date for date-of-birth checks (tests pin it); never the demo's pinned
        # booking calendar (BOOKING_TODAY_OVERRIDE).
        self.today = today
        self.lead = lead
        self.history = history
        self.settings = settings or get_settings()
        self.practice_id = practice_id
        # A card this reply opened that stops booking: a hand_off for a different case or a booking
        # problem, an emergency, a case change. A front-desk card for insurance and the like does
        # not (owner, 2026-11-02). The LeadView is from before this reply.
        self.paused_by_hand_off = False
        # What this reply already did ("hand_off:<reason>", "case_change", "emergency"): a second
        # call for the same thing sends nothing again (repeat_key).
        self.done_here: set[str] = set()
        # Failed bookings in this reply by code (REPEATED_FAILURES).
        self.failures_here: dict[str, int] = {}

    # Subclasses: a connection (read-only for the sandbox) and the booking context for a clinic.
    @contextmanager
    def _conn(self) -> Iterator[Any]:
        with transaction() as conn:
            yield conn

    def _context(self, conn, clinic: str | None,
                 service: str | None = None) -> slot_booking.BookingContext:
        raise NotImplementedError

    def _offer_key(self) -> str | None:
        return None

    def find_times(self, *, when: str, specific_date: str | None = None, month: str | None = None,
                   time_of_day: str | None = None, therapist: str | None = None,
                   clinic: str | None = None, service: str | None = None) -> ToolResult:
        asked = str(when or "").strip().lower()
        when = search_when(when, self.history, specific_date, self.search_todays())
        if when == "tomorrow":
            specific_date = None
        try:
            result = self._find_times(when=when, specific_date=specific_date, month=month,
                                      time_of_day=time_of_day, therapist=therapist, clinic=clinic,
                                      service=service)
            result.output = name_search_day(result.output, asked=asked,
                                            searched=str(when or "").strip().lower())
            return result
        except ValueError as exc:
            if not str(exc).startswith("NO_CLINICIANS"):
                raise
            return ToolResult(self._no_clinicians(str(exc)), "find_times",
                              "No therapist for that service at that clinic")

    def search_todays(self) -> set[date]:
        """The days a find_times specific_date means "today": the clinic's real date (the Context's
        Now) and the demo's pinned booking calendar day."""
        days = {birthday.real_today()}
        if self.settings.booking_today_override:
            days.add(self.settings.booking_today_override)
        return days

    def _find_times(self, **search: Any) -> ToolResult:
        raise NotImplementedError

    def _no_clinicians(self, message: str) -> str:
        match = _NO_CLINICIANS.match(message)
        if not match:
            return message
        try:
            with self._conn() as conn:
                rows = conn.execute(OFFERED_AT_SQL, {"practice": self.practice_id,
                                                     "service": match["service"]}).fetchall()
        except Exception as exc:  # noqa: BLE001 - the plain reply still tells the model what failed
            self.trace.log("sms_agent_offered_at_failed", error_category=type(exc).__name__)
            return message
        return offered_elsewhere(message, [row["name"] for row in rows])

    def _service_context(self, conn, clinic: str | None,
                         service: str | None) -> slot_booking.BookingContext:
        """The booking context for the service the model named (None = the lead's own).

        A new person's service reaches the search and the booking (np-03 asked for pelvic and got a
        Physical Therapy card). For a patient with a referral another service is a case change: the
        front desk sets that up, it is never booked by text."""
        context = self._context(conn, clinic, service)
        wanted = context.case_type["name"]
        if (service and not self.lead.is_new_person and self.lead.service
                and wanted.strip().lower() != self.lead.service.strip().lower()):
            raise ValueError(
                f"CASE_CHANGE: this patient's referral is for {self.lead.service}, not {wanted}. "
                "Another service is a case change: do not book it by text (report_outcome "
                "case_change with what they asked for).")
        return context

    # --- repeats ---------------------------------------------------------------------------------

    def _earlier(self, key: str) -> bool:
        """Done before this reply (live: lead_reviews; test chat: earlier in the chat)."""
        return False

    def _repeat(self, key: str, *also_earlier: str) -> bool:
        return key in self.done_here or any(self._earlier(k) for k in (key, *also_earlier))

    def _hand_off_repeat(self, reason: str) -> bool:
        # A different case reported on an earlier reply is the same thing for the front desk; in
        # this reply the case change report comes first and the hand-off still sends its text.
        return self._repeat(f"hand_off:{reason}",
                            *(("case_change",) if reason == "different_case" else ()))

    def _already_handed_off(self, reason: str) -> ToolResult:
        self._note_hand_off(reason)
        self.done_here.add(f"hand_off:{reason}")
        phone = self.settings.clinic_front_desk_phone
        output = ALREADY_HANDED_OFF + (ALREADY_HANDED_OFF_PHONE.format(phone=phone) if phone else "")
        return ToolResult(output, "hand_off",
                          f"Front desk already has this ({reason}): nothing sent again",
                          action_code="ALREADY_HANDED_OFF", reason=reason)

    def _already_emergency(self) -> ToolResult:
        self.paused_by_hand_off = True
        self.done_here.add("emergency")
        return ToolResult(ALREADY_SENT, "emergency",
                          "Emergency text already sent within the hour: not sent again",
                          action_code="ALREADY_SENT")

    def _already_case_change(self) -> ToolResult:
        self.done_here.add("case_change")
        return ToolResult(ALREADY_REPORTED, "report",
                          "Case change already with the front desk: nothing changed",
                          action_code="ALREADY_REPORTED")

    def _render(self, conn, key: str, **values: Any) -> str:
        return texts.render(conn, key, practice_id=self.practice_id, settings=self.settings,
                            **values)

    def _paused_here(self) -> bool:
        return self.paused_by_hand_off

    def _note_hand_off(self, reason: str) -> None:
        if reason not in FRONT_DESK_BOOKABLE_REASONS:
            self.paused_by_hand_off = True

    def _review_open_reply(self) -> ToolResult | None:
        # Plan section 6: a lead waiting for staff review is not booked by text; staff decide first.
        # Except Elliot's own front-desk card (insurance, a person, ...; owner, 2026-11-02): it stays
        # open for the front desk, and booking goes on (LeadView.booking_paused).
        if self.lead.booking_paused or self._paused_here():
            return ToolResult(
                "REVIEW_OPEN: this patient is waiting for our staff to review their file, so we "
                "can't book by text right now. Tell them our front desk team will reach out to them "
                "about their visit.", "book", "Not booked: lead in Needs Review")
        return None

    # --- birthday ------------------------------------------------------------------------------

    def birthday_today(self) -> date:
        return self.today or birthday.real_today()

    def birthday_state(self) -> birthday.BirthdayState:
        """What this chat settled about the date of birth (the Context line reads the same)."""
        return birthday.birthday_state(self.lead, self.birthday_history, self.birthday_today())

    def check_birthday(self, *, date_of_birth: str) -> ToolResult:
        result = birthday.check(self.lead, self.birthday_history, date_of_birth,
                                self.birthday_today())
        return ToolResult(result.output, "check_birthday", result.summary)

    def _birth_date_for_booking(self, given: str | None) -> tuple[str, bool]:
        """(YYYY-MM-DD, differs from file): the date this chat confirmed; else the model's, else the
        one on file."""
        state = self.birthday_state()
        if state.confirmed is not None:
            return state.confirmed.isoformat(), state.differs
        on_file = self.lead.date_of_birth.isoformat() if self.lead.date_of_birth else ""
        return (given or on_file), bool(given and on_file and given != on_file)

    # --- repeated failures -----------------------------------------------------------------------

    def _earlier_failures(self, code: str) -> int:
        """Failed bookings with this code earlier in the chat (live: sms_agent_actions; test chat:
        the side panel's codes)."""
        return 0

    def _stop_repeated_failure(self, result: ToolResult) -> ToolResult:
        code = result.code
        if code not in REPEATED_FAILURES:
            return result
        self.failures_here[code] = self.failures_here.get(code, 0) + 1
        if self.failures_here[code] + self._earlier_failures(code) < FAILURE_LIMIT:
            return result
        self.trace.log("sms_agent_booking_gave_up", result_code=code)
        handed = self.hand_off(reason="booking_problem",
                               notes=f"Booking by text failed twice ({code}); please book by hand.")
        if handed.code == "ALREADY_HANDED_OFF":
            return ToolResult(f"ALREADY_HANDED_OFF: booking by text failed again ({code}) and the "
                              "front desk already has it. Ask nothing more about this booking; "
                              "NO_REPLY or one short line.", "book",
                              f"Booking refused again ({code}): front desk already has it",
                              action_code="ALREADY_HANDED_OFF", reason="booking_problem")
        return ToolResult(
            f"HANDED_OFF: booking by text failed twice for the same reason ({code}), so the front "
            "desk text was sent and our front desk will finish the booking. Ask nothing more about "
            "this booking (no date of birth, name or card again); NO_REPLY or one short line.",
            "book", f"Booking failed twice ({code}): handed to the front desk",
            fixed_texts=handed.fixed_texts, action_code="HANDED_OFF_PAUSED",
            reason="booking_problem")

    def book_visit(self, *, date: str, time: str, therapist: str | None = None,
                   first_name: str | None = None, last_name: str | None = None,
                   date_of_birth: str | None = None, clinic: str | None = None,
                   service: str | None = None) -> ToolResult:
        try:
            result = self._book_visit(date=date, time=time, therapist=therapist,
                                      first_name=first_name, last_name=last_name,
                                      date_of_birth=date_of_birth, clinic=clinic, service=service)
        except (ValueError, slot_booking.BookingToolError) as exc:
            result = ToolResult(str(exc), "book", f"book_visit: {str(exc).split(':', 1)[0]}")
        return self._stop_repeated_failure(result)

    def _book_visit(self, **arguments: Any) -> ToolResult:
        raise NotImplementedError

    # --- the card --------------------------------------------------------------------------------

    def confirm_visit(self, *, date: str, time: str, therapist: str | None = None,
                      clinic: str | None = None, service: str | None = None,
                      first_name: str | None = None) -> ToolResult:
        blocked = self._review_open_reply()
        if blocked:
            return ToolResult(blocked.output, "confirm_visit", blocked.summary)
        if self.lead.status == "booked":
            return ToolResult(f"ALREADY_BOOKED: this patient already has a visit "
                              f"({self.lead.booked_visit or 'on record'}). Do not book again.",
                              "confirm_visit", "Already booked")
        wanted_day = _parse_day(date, "date")
        wanted_time = slot_booking.parse_clock(time)
        with self._conn() as conn:
            context = self._service_context(conn, clinic, service)
            # "Alan Rome" for Alan Brooks (gpt-5.5 eval): the one clinician it can mean, or a
            # question back; the card prints the real name from the slot.
            therapist = resolve_therapist(context, therapist)
            slot = slot_booking.open_slot(conn, context, self._offer_key() or "sandbox",
                                          wanted_day, wanted_time, therapist)
            shown = context.case_type["name"]
            if shown.strip().lower() == default_service(conn, context).strip().lower():
                shown = None
        if slot is None:
            others = slot_booking.alternatives(self.trace, context, self._offer_key(), wanted_day)
            return ToolResult(
                f"NOT_AVAILABLE: {texts.card_time(wanted_time)} on {texts.long_day(wanted_day)} "
                f"is not an open time{f' with {therapist}' if therapist else ''}. {others}",
                "confirm_visit", f"{_summary_when(wanted_day, wanted_time)} is not open")
        state = self.birthday_state()
        if state.confirmed is None:
            # The identity step comes first (owner, 2026-11-03): the date of birth typed in this
            # chat; never a yes to a date read to them. Our question names the time (2026-11-04),
            # so their answer gets this card from code, not from the model's choice.
            if state.pending:
                return ToolResult(PENDING_BIRTH_DATE, "confirm_visit",
                                  "No card: date of birth not confirmed yet")
            question = texts.birthday_question(slot["local_date"], slot["local_time"],
                                               slot["display_name"], context.location["name"],
                                               service=shown)
            if last_elliot_text(self.history).strip() == question:
                return ToolResult(BIRTH_DATE_ASKED_AGAIN, "confirm_visit",
                                  "No card: date of birth not readable yet")
            return ToolResult(BIRTH_DATE_ASKED, "confirm_visit",
                              f"Asked for the date of birth for "
                              f"{_summary_when(slot['local_date'], slot['local_time'])}",
                              fixed_texts=[question], send_last=True)
        if self.no_first_name() and not first_name_in_chat(self.lead, self.history, first_name):
            # x-04 (2026-11-03): the first name comes before the card, never after the yes.
            return ToolResult(MISSING_FIRST_NAME_CARD, "confirm_visit",
                              "No card: no first name yet")
        card = texts.confirm_card(slot["local_date"], slot["local_time"], slot["display_name"],
                                  context.location["name"], service=shown)
        if last_elliot_text(self.history).strip() == card.strip():
            # Never the same card twice in a row (2026-11-03 loop): a new card needs a new step in
            # between (a name, a date of birth, another time).
            return ToolResult(CARD_ALREADY_SENT, "confirm_visit", "Card already sent: not sent again")
        no_first_name = ("" if not self.no_first_name()
                         else " No first name is on file: pass book_visit the first name the "
                              "patient wrote in this chat (never their last name).")
        return ToolResult(
            "CARD_SENT: the read-back card was texted to the patient word for word. Wait for their "
            "reply; book only after a clear yes." + no_first_name,
            "confirm_visit",
            f"Read back {_summary_when(slot['local_date'], slot['local_time'])} with "
            f"{slot['display_name']} at {context.location['name']}"
            + (f" ({shown})" if shown else ""),
            fixed_texts=[card], send_last=True)

    def no_first_name(self) -> bool:
        """A lead with a referral and no first name on file (someone new gives both names)."""
        return not self.lead.is_new_person and not (self.lead.first_name or "").strip()

    def _guard(self, date: str, time: str, therapist: str | None, clinic: str | None,
               service: str | None = None) -> tuple[dict | None, ToolResult | None]:
        blocked = self._review_open_reply()
        if blocked:
            return None, blocked
        card, why = card_guard(self.history, day=date, time=time, therapist=therapist,
                               clinic=clinic)
        if card is None:
            return None, ToolResult(NOT_CONFIRMED.format(why=why), "book",
                                    f"Booking refused: {why}")
        if service:
            # The service the model names must be the card's (a card without a service line is the
            # default service); for a patient with a referral another one is a case change.
            with self._conn() as conn:
                context = self._service_context(conn, card["clinic"], service)
                expected = card.get("service") or default_service(conn, context)
            if context.case_type["name"].strip().lower() != expected.strip().lower():
                why = "the service is not the one on the read-back card"
                return None, ToolResult(NOT_CONFIRMED.format(why=why), "book",
                                        f"Booking refused: {why}")
        return card, None

    def _booked_text(self, conn, card: dict, first_name: str) -> str:
        """`first_name`: the booking's; the text uses it only if on file or written by the
        patient (greeting_name)."""
        return self._render(
            conn, "booked", first_name=greeting_name(self.lead, self.history, first_name),
            day=texts.long_day(card["date"]), time=card["time"], therapist=card["therapist"],
            clinic=card["clinic"], address=texts.address_for(card["clinic"]))

    def send_intro(self) -> ToolResult:
        key = "new_person_intro" if self.lead.is_new_person else "intro"
        with self._conn() as conn:
            text = self._render(conn, key, first_name=self.lead.first_name)
        return ToolResult("INTRO_SENT: the first-reply text was sent word for word.", "fixed_text",
                          f"Sent the {key.replace('_', ' ')} text", fixed_texts=[text])

    def _hand_off_text(self, reason: str) -> str:
        with self._conn() as conn:
            key = {"other_language": "english_only", "cancel_visit": "cancel_visit"}
            return self._render(conn, key.get(reason, "front_desk"))

    @staticmethod
    def _check_reason(reason: str) -> str:
        reason = (reason or "").strip().lower()
        if reason not in HAND_OFF_REASONS:
            raise ValueError(f"BAD_REASON: reason must be one of {', '.join(HAND_OFF_REASONS)}.")
        return reason

    @staticmethod
    def _check_status(status: str) -> str:
        status = (status or "").strip().lower()
        if status not in REPORT_STATUSES:
            raise ValueError(f"BAD_STATUS: status must be one of {', '.join(REPORT_STATUSES)}.")
        return status


# --- live ------------------------------------------------------------------------------------------

class LiveTools(_BaseTools):
    """Tools for a real lead (the sms-agent-worker). Books in Stride, reports statuses, opens cards."""

    def __init__(self, trace: WorkflowTrace, *, lead_id: str, job_id: int, lead: LeadView,
                 history: list[Turn], settings: Settings | None = None,
                 practice_id: int | None = None, providers=None, today: date | None = None,
                 birthday_history: list[Turn] | None = None):
        super().__init__(trace, lead=lead, history=history, settings=settings,
                         practice_id=practice_id, today=today, birthday_history=birthday_history)
        self.lead_id = lead_id
        self.job_id = job_id
        self.providers = providers  # tests pass a fake Stride; None = the real client
        # Offers per conversation (kept 24 h, slot_booking.SMS_OFFER_MEMORY); reports per job.
        self.conversation = f"{slot_booking.SMS_CONVERSATION_PREFIX}{lead_id}"
        self.report_key = f"{self.conversation}:{job_id}"
        # A failed booking already opened its own card (Booking group); the prompt then hands off
        # with booking_problem, which must not open a second one.
        self.booking_flagged = False
        # A case change reports case_change (its own card, lead_status.flag_case_change) and then
        # hands off with different_case, which must not open a second card for the same thing.
        self.case_change_flagged = False
        self._looked_back: dict[str, bool] = {}

    def _paused_here(self) -> bool:
        return self.booking_flagged or self.case_change_flagged or self.paused_by_hand_off

    def _offer_key(self) -> str:
        return self.conversation

    def _earlier_failures(self, code: str) -> int:
        # The worker stores every reply's actions (tool kind + result code) after the reply, so
        # this reply's own are not counted twice.
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "select count(*) as n from sms_agent_actions where lead_id=%s and tool='book' "
                    "and result_code=%s and created_at>now()-%s::interval",
                    (self.lead_id, code, FAILURE_WINDOW),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001 - the refusal itself must still reach the model
            self.trace.log("sms_agent_failure_count_failed", error_category=type(exc).__name__)
            return 0
        return int(row["n"]) if row else 0

    def _context(self, conn, clinic: str | None,
                 service: str | None = None) -> slot_booking.BookingContext:
        return slot_booking.load_context(conn, self.lead_id, location_name=clinic,
                                         case_type_name=service, settings=self.settings)

    def _earlier(self, key: str) -> bool:
        if key not in self._looked_back:
            self._looked_back[key] = self._look_back(key)
        return self._looked_back[key]

    def _look_back(self, key: str) -> bool:
        """The same card from an earlier reply: still open and flagged within 24 h (hand-off, case
        change), flagged within the hour (emergency: after that the 911 text may go again).

        A card staff (or Elliot) already resolved is no repeat (review, 2026-11-02): the patient
        asking again after that is a new request, so it gets a new card and the front desk text."""
        if key == "case_change":
            code, reason, window, open_only = (CASE_CHANGE_CODE, None, HAND_OFF_REPEAT_WINDOW,
                                               True)
        elif key == "emergency":
            code, reason, window, open_only = (SMS_FRONT_DESK_CODE, "emergency",
                                               EMERGENCY_REPEAT_WINDOW, False)
        else:
            code, reason, window, open_only = (SMS_FRONT_DESK_CODE, key.split(":", 1)[1],
                                               HAND_OFF_REPEAT_WINDOW, True)
        # This job's own cards (a case change reported a moment ago in this reply) are not
        # "earlier": the hand-off after it still sends the front desk text once.
        with self._conn() as conn:
            row = conn.execute(
                "select exists(select 1 from lead_reviews where lead_id=%s and code=%s "
                "and (%s::text is null or details->>'reason'=%s) "
                "and coalesce(details->>'conversation',details->>'call_id','')<>%s "
                "and flagged_at>now()-%s::interval and (not %s or resolved_at is null)) as found",
                (self.lead_id, code, reason, reason, self.report_key, window, open_only),
            ).fetchone()
        return bool(row and row["found"])

    def _find_times(self, *, when: str, specific_date: str | None = None, month: str | None = None,
                    time_of_day: str | None = None, therapist: str | None = None,
                    clinic: str | None = None, service: str | None = None) -> ToolResult:
        if service:
            with self._conn() as conn:
                self._service_context(conn, clinic, service)  # CASE_CHANGE for another service

        def search(name: str | None) -> str:
            return slot_booking.find_slots(
                self.trace, lead_id=self.lead_id, call_id=self.conversation, when=when,
                specific_date=_parse_day(specific_date, "date"), month=_parse_day(month, "month"),
                time_of_day=time_of_day, clinician_name=name, location_name=clinic,
                case_type_name=service, settings=self.settings, providers=self.providers)

        output = search(therapist)
        if therapist and output.startswith("NO_SUCH_THERAPIST"):
            # A wrong surname: search again with the one clinician it can mean, if only one.
            with self._conn() as conn:
                better = closest_therapist(self._context(conn, clinic, service), therapist)
            if better != therapist:
                output = search(better)
        return ToolResult(output, "find_times", f"Looked for times ({when})")

    def list_therapists(self, *, clinic: str | None = None) -> ToolResult:
        output = slot_booking.list_providers(lead_id=self.lead_id, location_name=clinic,
                                             settings=self.settings)
        return ToolResult(output, "list_therapists", "Listed therapists")

    def _book_visit(self, *, date: str, time: str, therapist: str | None = None,
                    first_name: str | None = None, last_name: str | None = None,
                    date_of_birth: str | None = None, clinic: str | None = None,
                    service: str | None = None) -> ToolResult:
        card, refused = self._guard(date, time, therapist, clinic, service)
        if refused:
            return refused
        first_name, last_name = booking_names(self.lead, self.history, first_name, last_name)
        dob, differs = self._birth_date_for_booking(date_of_birth)
        arguments = {
            "date": card["date"].isoformat(), "time": card["time"],
            "clinician_name": card["therapist"], "location": card["clinic"],
            "first_name": first_name, "last_name": last_name,
            # The date of birth the patient typed and this chat confirmed (check_birthday), which
            # may differ from the file (voice rule: theirs is used; appointments.patient_dob keeps
            # it for staff); else the model's, else the one on file.
            "date_of_birth": dob,
        }
        if card.get("service"):
            arguments["case_type"] = card["service"]  # the service on the card the patient took
        when = f"{_summary_when(card['date'], card['clock'])} with {card['therapist']}"
        if differs:
            when += " (date of birth differs from our records: the patient's was used)"
        try:
            output = slot_booking.book_slot(self.trace, lead_id=self.lead_id,
                                            call_id=self.conversation, arguments=arguments,
                                            settings=self.settings, providers=self.providers,
                                            source="sms")
        except (ValueError, slot_booking.BookingToolError):
            raise
        except Exception as exc:  # noqa: BLE001 - unclear whether anything reached Stride
            self.trace.log("sms_agent_booking_error", error_category=type(exc).__name__)
            slot_booking.flag_booking_error(self.lead_id)
            self.booking_flagged = True
            return ToolResult("FOLLOW_UP: the booking could not be finished. The front desk text was "
                              "sent.", "book", f"Booking failed: {when}",
                              fixed_texts=[self._hand_off_text("booking_problem")])
        result = ToolResult(output, "book", f"Booking {when}: {output.split(':', 1)[0]}")
        if result.code == "BOOKED":
            try:
                with self._conn() as conn:
                    booked = self._booked_text(conn, card, first_name)
            except Exception as exc:  # noqa: BLE001 - the visit is made: the patient must hear it
                # A database error reading the Studio text here went to call_tool as "ERROR: nothing
                # was changed, do not say it worked" for a visit that is in Stride. The draft in
                # code is the same text without the clinic's edits.
                self.trace.log("sms_agent_booked_text_fallback", error_category=type(exc).__name__)
                booked = self._booked_text(None, card, first_name)
            result.fixed_texts = [booked]
            result.output = "BOOKED: the booked text was sent to the patient word for word."
            result.summary = f"Booked {when}"
        elif result.code in FRONT_DESK_CODES:
            # The booking engine already opened the Needs Review card (Booking group); the patient
            # is told the front desk will take care of it whatever the model does next.
            self.booking_flagged = True
            result.fixed_texts = [self._hand_off_text("booking_problem")]
        return result

    def report_outcome(self, *, status: str, notes: str | None = None,
                       callback_type: str | None = None, delay_minutes: Any = None,
                       callback_datetime_iso: str | None = None) -> ToolResult:
        status = self._check_status(status)
        if status == "do_not_contact":
            # By text "stop texting me" is an SMS opt-out (contract), the same as the STOP keyword.
            # "No contact at all" opts out of calls too, which makes the lead Do Not Contact
            # (explicit_opt_out: both channels off), so the reply's promise is true.
            everything = wants_no_contact_at_all(notes)
            with transaction() as conn:
                row = conn.execute("select phone_e164 from leads where id=%s",
                                   (self.lead_id,)).fetchone()
            if row and row["phone_e164"]:
                explicit_opt_out(self.trace, row["phone_e164"], "sms", SMS_AGENT_SOURCE)
                if everything:
                    explicit_opt_out(self.trace, row["phone_e164"], "call", SMS_AGENT_SOURCE)
            if everything:
                return ToolResult("REPORTED: texts and calls stopped; the patient is Do Not Contact.",
                                  "report", "Opted out of texts and calls", opted_out=True,
                                  action_code=reported_code(status))
            return ToolResult("REPORTED: texts stopped; the patient is opted out of our texts.",
                              "report", "Opted out of texts", opted_out=True,
                              action_code=reported_code(status))
        if status == "case_change" and self._repeat("case_change"):
            self.case_change_flagged = True
            return self._already_case_change()
        try:
            result = report_lead_status(
                self.trace, lead_id=self.lead_id, status=status, call_id=self.report_key,
                notes=notes, callback_type=callback_type, delay_minutes=delay_minutes,
                callback_datetime_iso=callback_datetime_iso, source=SMS_AGENT_SOURCE,
                queue_link_text=False)
        except ValueError as exc:
            return ToolResult(f"NOT_RECORDED: {exc}.", "report", f"Could not report {status}")
        if result == "already booked":
            return ToolResult("ALREADY_BOOKED: the patient is booked; nothing was changed.", "report",
                              f"Not reported ({status}): already booked")
        if status == "case_change":
            self.case_change_flagged = True
            self.done_here.add("case_change")
        if status == "booking_link":
            with self._conn() as conn:
                link = texts.booking_link(conn, self.practice_id)
            return ToolResult(f"REPORTED: booking link. Put this link in your text: {link}",
                              "report", "Reported: booking link",
                              action_code=reported_code(status))
        return ToolResult(f"REPORTED: {status}.", "report", f"Reported: {status}",
                          action_code=reported_code(status))

    def hand_off(self, *, reason: str, notes: str | None = None) -> ToolResult:
        reason = self._check_reason(reason)
        if self._hand_off_repeat(reason):
            return self._already_handed_off(reason)
        already = ((reason == "booking_problem" and self.booking_flagged)
                   or (reason == "different_case" and self.case_change_flagged))
        self._note_hand_off(reason)
        self.done_here.add(f"hand_off:{reason}")
        if not already:
            with transaction() as conn:
                open_front_desk_card(conn, self.lead_id, reason, notes,
                                     conversation=self.report_key)
        return ToolResult("HANDED_OFF: the front desk text was sent and a Needs Review card is open.",
                          "hand_off", f"Opened a Needs Review card: {reason}",
                          fixed_texts=[self._hand_off_text(reason)],
                          action_code=hand_off_code(reason), reason=reason)

    def flag_emergency(self, *, kind: str | None = None) -> ToolResult:
        if self._repeat("emergency"):
            return self._already_emergency()
        self.paused_by_hand_off = True
        self.done_here.add("emergency")
        with transaction() as conn:
            open_front_desk_card(conn, self.lead_id, "emergency", EMERGENCY_NOTES.get(kind or ""),
                                 conversation=self.report_key)
        return ToolResult("EMERGENCY_SENT: the 911 text was sent and staff were flagged.",
                          "emergency", "Flagged an emergency",
                          fixed_texts=[texts.emergency_text(kind)])


def open_front_desk_card(conn, lead_id: str, reason: str, notes: str | None, *,
                         conversation: str) -> None:
    """Needs Review card `sms_front_desk` (services/lead_review.py), in the caller's transaction.

    A lead in the cadence is paused like any review (status needs_attention, cadence paused), but
    for insurance, a person, another language, an unknown question or "other" the patient can still
    book by text (review.FRONT_DESK_BOOKABLE_REASONS); a booking keeps the card open
    (lead_status.mark_booked) and completes the cadence. A lead whose outcome is final (booked,
    declined, closed) keeps its status: the card only asks the front desk to call (`finished`).
    """
    lead = conn.execute(
        "select status,cadence_state from leads where id=%s for update", (lead_id,)
    ).fetchone()
    if not lead or lead["status"] == "do_not_contact":
        return
    words = SMS_FRONT_DESK_REASONS.get(reason, SMS_FRONT_DESK_REASONS["other"])
    message = f"Patient texted: {words}; front desk to reach out"[:500]
    details = {"reason": reason, "notes": (notes or "").strip()[:500] or None,
               "conversation": conversation}
    finished = (lead["status"] in FINAL_STATUSES
                or lead["cadence_state"] in {"completed", "terminated"})
    if not finished:
        if flag_lead_for_review(conn, lead_id, message, code=SMS_FRONT_DESK_CODE, details=details,
                                source="text"):
            record_status(conn, lead_id, lead["status"], "needs_attention", SMS_AGENT_SOURCE,
                          message)
        return
    conn.execute(
        "update leads set needs_review=true,review_reason=coalesce(case when needs_review "
        "then review_reason end,%s),review_flagged_at=coalesce(case when needs_review "
        "then review_flagged_at end,now()) where id=%s",
        (message, lead_id),
    )
    record_review(conn, lead_id, SMS_FRONT_DESK_CODE, message, details={**details, "finished": True},
                  source="text")


# --- sandbox ---------------------------------------------------------------------------------------

class SandboxTools(_BaseTools):
    """Tools for the test chat: the same search on the real cache, everything else pretend.

    Never writes to the database (every read runs in a read-only transaction), never calls Stride
    or Twilio; booking, reports and cards are described in the side panel ("Would ...").
    """

    pretend = True

    def __init__(self, trace: WorkflowTrace, *, lead: LeadView, history: list[Turn],
                 practice_id: int, now_local: datetime | None, settings: Settings | None = None,
                 earlier: set[str] | None = None, earlier_failures: dict[str, int] | None = None,
                 today: date | None = None):
        super().__init__(trace, lead=lead, history=history, settings=settings,
                         practice_id=practice_id, today=today)
        self.now_local = now_local
        # What Elliot did earlier in this chat (sms_agent.EarlierInChat.done): "case_change",
        # "emergency" (within the hour), "hand_off:<reason>" when the tab sent the hand-off's reason
        # back (compared like live), else "hand_off:front_desk" / "hand_off:paused": an older tab's
        # codes carry no reason, so there a reason counts by its kind (booking goes on, or stops).
        self.earlier = set(earlier or ())
        # Failed bookings earlier in this chat by code (sms_agent.EarlierInChat.failures).
        self.earlier_failures = dict(earlier_failures or {})

    def _earlier_failures(self, code: str) -> int:
        return self.earlier_failures.get(code, 0)

    def search_todays(self) -> set[date]:
        # The test chat's clock is the Context's Now (and the calendar searched).
        if self.now_local is not None:
            return {self.now_local.date()}
        return super().search_todays()

    def _earlier(self, key: str) -> bool:
        if key.startswith("hand_off:"):
            reason = key.split(":", 1)[1]
            kind = "front_desk" if reason in FRONT_DESK_BOOKABLE_REASONS else "paused"
            return key in self.earlier or f"hand_off:{kind}" in self.earlier
        return key in self.earlier

    @contextmanager
    def _conn(self) -> Iterator[Any]:
        with transaction() as conn:
            conn.execute("set transaction read only")
            yield conn

    def _context(self, conn, clinic: str | None,
                 service: str | None = None) -> slot_booking.BookingContext:
        return slot_booking.sandbox_context(
            conn, practice_id=self.practice_id, location_name=clinic or self.lead.clinic,
            case_type_name=service or self.lead.service, referral_word=self.lead.referral_word,
            settings=self.settings, now_local=self.now_local)

    def _find_times(self, *, when: str, specific_date: str | None = None, month: str | None = None,
                    time_of_day: str | None = None, therapist: str | None = None,
                    clinic: str | None = None, service: str | None = None) -> ToolResult:
        with self._conn() as conn:
            context = self._service_context(conn, clinic, service)
            if therapist:
                therapist = closest_therapist(context, therapist)
            output = slot_booking.find_slots_readonly(
                self.trace, conn, context, when=when,
                specific_date=_parse_day(specific_date, "date"), month=_parse_day(month, "month"),
                time_of_day=time_of_day, clinician_name=therapist)
        return ToolResult(output, "find_times", f"Looked for times ({when}) in the real cache")

    def list_therapists(self, *, clinic: str | None = None) -> ToolResult:
        with self._conn() as conn:
            output = slot_booking.providers_text(self._context(conn, clinic))
        return ToolResult(output, "list_therapists", "Listed therapists")

    def _book_visit(self, *, date: str, time: str, therapist: str | None = None,
                    first_name: str | None = None, last_name: str | None = None,
                    date_of_birth: str | None = None, clinic: str | None = None,
                    service: str | None = None) -> ToolResult:
        card, refused = self._guard(date, time, therapist, clinic, service)
        if refused:
            return refused
        if self.lead.status == "booked":
            return ToolResult("ALREADY_BOOKED: this patient already has a visit. Do not book again.",
                              "book", "Would not book: already booked")
        first_name, last_name = booking_names(self.lead, self.history, first_name, last_name)
        dob, differs = self._birth_date_for_booking(date_of_birth)
        with self._conn() as conn:
            context = self._context(conn, card["clinic"], card.get("service"))
            # The same checks the real booking makes on the name and birthday (the birthday against
            # the real date, like book_slot: the test clock may be pinned earlier).
            slot_booking.validate_patient(
                {"first_name": first_name, "last_name": last_name, "date_of_birth": dob},
                self.birthday_today())
            slot = slot_booking.open_slot(conn, context, "sandbox", card["date"], card["clock"],
                                          card["therapist"])
            when = f"{_summary_when(card['date'], card['clock'])} with {card['therapist']}"
            if differs:
                when += " (date of birth differs from our records: the patient's would be used)"
            if slot is not None:
                booked = self._booked_text(conn, card, first_name)
        if slot is None:
            others = slot_booking.alternatives(self.trace, context, None, card["date"])
            return ToolResult(f"TAKEN: that time was just taken. {others}", "book",
                              f"Would not book {when}: no longer open")
        return ToolResult("BOOKED (pretend): the booked text was sent word for word.", "book",
                          f"Would book {when}", fixed_texts=[booked])

    def report_outcome(self, *, status: str, notes: str | None = None,
                       callback_type: str | None = None, delay_minutes: Any = None,
                       callback_datetime_iso: str | None = None) -> ToolResult:
        status = self._check_status(status)
        if status == "do_not_contact":
            if wants_no_contact_at_all(notes):
                return ToolResult("REPORTED (pretend): texts and calls stopped; the patient is Do "
                                  "Not Contact.", "report", "Would opt the patient out of texts and calls",
                                  action_code=reported_code(status))
            return ToolResult("REPORTED (pretend): texts stopped; the patient is opted out of our "
                              "texts.", "report", "Would opt the patient out of texts",
                              action_code=reported_code(status))
        if status == "callback_scheduled" and not (callback_type or delay_minutes
                                                   or callback_datetime_iso):
            return ToolResult("NOT_RECORDED: callback_scheduled requires a callback time.",
                              "report", "Would not report a callback: no time")
        if self.lead.status == "booked" and status != "case_change":
            return ToolResult("ALREADY_BOOKED: the patient is booked; nothing was changed.", "report",
                              f"Would not report {status}: already booked")
        if status == "case_change":
            self.paused_by_hand_off = True
            if self._repeat("case_change"):
                return self._already_case_change()
            self.done_here.add("case_change")
        if status == "booking_link":
            with self._conn() as conn:
                link = texts.booking_link(conn, self.practice_id)
            return ToolResult(f"REPORTED (pretend): booking link. Put this link in your text: "
                              f"{link}", "report", "Would report: booking link",
                              action_code=reported_code(status))
        return ToolResult(f"REPORTED (pretend): {status}.", "report", f"Would report: {status}",
                          action_code=reported_code(status))

    def hand_off(self, *, reason: str, notes: str | None = None) -> ToolResult:
        reason = self._check_reason(reason)
        if self._hand_off_repeat(reason):
            return self._already_handed_off(reason)
        self._note_hand_off(reason)
        self.done_here.add(f"hand_off:{reason}")
        return ToolResult("HANDED_OFF (pretend): the front desk text was sent.", "hand_off",
                          f"Would open a Needs Review card: {SMS_FRONT_DESK_REASONS[reason]}",
                          fixed_texts=[self._hand_off_text(reason)],
                          action_code=hand_off_code(reason), reason=reason)

    def flag_emergency(self, *, kind: str | None = None) -> ToolResult:
        if self._repeat("emergency"):
            return self._already_emergency()
        self.paused_by_hand_off = True
        self.done_here.add("emergency")
        return ToolResult("EMERGENCY_SENT (pretend): the 911 text was sent.", "emergency",
                          "Would flag an emergency for staff",
                          fixed_texts=[texts.emergency_text(kind)])

