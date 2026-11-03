"""The SMS agent's birthday step: the patient types their date of birth, code reads and checks it.

Owner, 2026-11-03 (test chat: "is your birthday November 1?", "yep", the card, "yes", then "Could you
send your date of birth with the year?" twice and the same card again, never booked): Elliot asks
once for the complete date of birth ("To confirm it's you, what's your date of birth?"), never reads
the date on file to the patient, and once a valid date of birth was given in the chat he never asks
again. The model only passes the patient's words; this module parses them, compares them with the
date on file and says what the chat has settled (`birthday_state`), so the Context line "Date of
birth confirmed in this chat" and the booking use the same answer, live and in the test chat.

Rules (deterministic, no model):
- A date of birth is read only from the patient's own texts, in common US forms: 3/14/1988,
  03-14-1988, 3.14.88, March 14 1988, Mar 14, 1988, March 14th, 1988, 14 March 1988, 1988-03-14.
  Numbers are month first; a first number over 12 is the day (14/3/1988). A 2-digit year is 20xx
  when that is at least a year back, else 19xx ("88" = 1988, "05" = 2005), except a year from last
  year to 2 years ahead ("26", "27" in 2026), which is a visit day ("can I come 11/20/26?"), not a
  birth year: no date (review 2026-11-03: it read as 1926 and settled the date of birth).
- A date in a patient text counts as their date of birth only when it is one: the text names it
  (birthday, born, DOB, date of birth), Elliot's text before it asked about it, or the text is little
  more than the date ("3/14/1988", "John Smith 3/14/1988"). "I hurt my knee on 3/2/2024" is not one
  (review 2026-11-03: someone new had it settled as their date of birth and was never asked). Eval
  np-03 (2026-11-03: "I'd like to book. Brooke Tanner, 02/09/1993" was asked again): a full date that
  can only be a date of birth counts too, when the text is not about something that happened (hurt,
  surgery, since, ...): one at least 10 years back, one right after a name, or one sent while an
  earlier date of birth question is unanswered.
- Possible = at least one year old and at most 120, against the REAL clinic today (never the demo's
  pinned booking calendar: a birthday after the pinned "today" made every booking fail).
- MATCH = the date on file. DIFFERENT = a possible date that is not the one on file: Elliot asks once
  to double-check; the date typed again, a yes to the double-check, or any second date settles it
  (like voice: the patient's own date is used, staff see it). NEW = no date on file (someone new).
- A month and day without a year ("June 3"), then a text that is only the year ("1989") after we
  asked for it, is that full date (eval x-07, 2026-11-03: the year alone was UNREADABLE and the
  whole date of birth question was asked again).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from .front_desk import CLINIC_TZ
from .sms_agent_context import LeadView, Turn

MAX_AGE_YEARS = 120

_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
# After a month name: 4 digits, '88, or ", 88" (a bare "Nov 20 10:30" or "Nov 20, 10am" is a time).
_YEAR = r"(?:,?\s*(?P<year>\d{4}|'\d{2})|,\s*(?P<yy>\d{2})(?!\s*[:ap]))\b"
_ISO = re.compile(r"(?<!\d)(?P<year>\d{4})[-/.](?P<a>\d{1,2})[-/.](?P<b>\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<![\d/.-])(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<year>\d{4}|\d{2})"
                      r"(?![\d/-])")
_MONTH_FIRST = re.compile(rf"\b{_MONTH}\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:{_YEAR})?")
_DAY_FIRST = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:{_YEAR})?")
# Month and day with no year ("11/1", "nov 1") = NEED_YEAR.
_NUMERIC_NO_YEAR = re.compile(r"(?<![\d/.-])(?P<a>\d{1,2})[/-](?P<b>\d{1,2})(?![\d/.-])")

# Elliot's double-check question after DIFFERENT (the tool reply gives it to the model).
DOUBLE_CHECK = re.compile(r"double[- ]?check", re.IGNORECASE)
_YES = re.compile(r"^(?:y|ya|ye|yes+|yeah|yea|yep|yup|correct|right|that'?s right|that'?s correct|"
                  r"it'?s right|it'?s correct|confirmed?|sure)\b")


def real_today() -> date:
    """The clinic's real date (never BOOKING_TODAY_OVERRIDE)."""
    return datetime.now(CLINIC_TZ).date()


def years_before(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # Feb 29
        return today.replace(year=today.year - years, day=28)


def possible(dob: date, today: date) -> bool:
    """At least one year old (so never this year or the future) and at most MAX_AGE_YEARS."""
    return years_before(today, MAX_AGE_YEARS) <= dob <= years_before(today, 1)


def _day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(raw: str, month: int, day: int, today: date) -> int | None:
    digits = raw.lstrip("'")
    if len(digits) == 4:
        return int(digits)
    if len(digits) != 2:
        return None
    recent = 2000 + int(digits)
    candidate = _day(recent, month, day)
    if candidate is not None and candidate <= years_before(today, 1):
        return recent
    if today.year - 1 <= recent <= today.year + 2:
        return None  # "11/20/26": a visit day this year or next, never a birth in 1926
    return 1900 + int(digits)


def _month_day(a: int, b: int) -> tuple[int, int]:
    # Month first (US); "14/3/1988" can only be day first.
    return (b, a) if a > 12 >= b else (a, b)


@dataclass(frozen=True)
class Parsed:
    value: date | None          # a full date (year included), possible or not
    need_year: bool = False     # a month and day with no year


def parse_birth_date(text: str | None, today: date | None = None) -> Parsed:
    """The first full date in the text, or need_year for a month and day without one."""
    today = today or real_today()
    flat = " ".join(str(text or "").lower().replace("’", "'").split())
    found: list[tuple[int, date | None]] = []
    for match in _ISO.finditer(flat):
        found.append((match.start(), _day(int(match["year"]), int(match["a"]), int(match["b"]))))
    for match in _NUMERIC.finditer(flat):
        month, day = _month_day(int(match["a"]), int(match["b"]))
        year = _year(match["year"], month, day, today)
        found.append((match.start(), _day(year, month, day) if year else None))
    no_year = False
    for pattern in (_MONTH_FIRST, _DAY_FIRST):
        for match in pattern.finditer(flat):
            month, day = _MONTHS[match["mon"][:3]], int(match["day"])
            raw_year = match["year"] or match["yy"]
            if not raw_year:
                no_year = no_year or _day(2000, month, day) is not None
                continue
            year = _year(raw_year, month, day, today)
            found.append((match.start(), _day(year, month, day) if year else None))
    dates = [value for _, value in sorted(found, key=lambda item: item[0]) if value is not None]
    if dates:
        return Parsed(dates[0])
    for match in _NUMERIC_NO_YEAR.finditer(flat):
        month, day = _month_day(int(match["a"]), int(match["b"]))
        no_year = no_year or (month <= 12 and _day(2000, month, day) is not None)
    return Parsed(None, need_year=no_year)


def month_day_only(text: str | None, today: date | None = None) -> tuple[int, int] | None:
    """(month, day) of a text that gives them with no year ("june 3", "6/3"), else None."""
    today = today or real_today()
    parsed = parse_birth_date(text, today)
    if parsed.value is not None or not parsed.need_year:
        return None
    flat = " ".join(str(text or "").lower().replace("’", "'").split())
    found: list[tuple[int, int, int]] = []
    for pattern in (_MONTH_FIRST, _DAY_FIRST):
        found += [(m.start(), _MONTHS[m["mon"][:3]], int(m["day"])) for m in pattern.finditer(flat)
                  if not (m["year"] or m["yy"]) and _day(2000, _MONTHS[m["mon"][:3]], int(m["day"]))]
    for match in _NUMERIC_NO_YEAR.finditer(flat):
        month, day = _month_day(int(match["a"]), int(match["b"]))
        if month <= 12 and _day(2000, month, day) is not None:
            found.append((match.start(), month, day))
    return min(found)[1:] if found else None


# A text that is only a year ("1989", "'89", "it's 1989", "born in 89"): the answer to "and the year?"
# after a month and day (x-07: "June 3", then "1989" was UNREADABLE and the whole question came again).
_YEAR_ONLY = re.compile(r"^(?:(?:it'?s|it is|in|born in|year)\s+)?(?P<year>\d{4}|'?\d{2})[.!]?$")
_ASKED_YEAR = re.compile(r"\byear\b", re.IGNORECASE)


def year_for(text: str | None, month_day: tuple[int, int], asked: str,
             today: date | None = None) -> date | None:
    """The possible date of birth made of `month_day` and a text that is only a year. Only when our
    text before it asked about the year or the date of birth ("10" after "nov 20" may be a time)."""
    today = today or real_today()
    if not (_ASKED_YEAR.search(asked) or _BIRTH_WORDS.search(asked)):
        return None
    flat = " ".join(str(text or "").lower().replace("’", "'").split())
    match = _YEAR_ONLY.match(flat)
    if not match:
        return None
    month, day = month_day
    year = _year(match["year"], month, day, today)
    value = _day(year, month, day) if year else None
    return value if value is not None and possible(value, today) else None


def birth_date_in(text: str | None, today: date | None = None) -> date | None:
    """A possible date of birth the text holds (None for none, no year, or impossible)."""
    today = today or real_today()
    value = parse_birth_date(text, today).value
    return value if value is not None and possible(value, today) else None


# Words that make a date a date of birth (in the patient's text, or Elliot's question before it).
_BIRTH_WORDS = re.compile(r"\b(?:birth\w*|born|b-?day|dob|d\.o\.b)\b", re.IGNORECASE)
# At most this many other words next to the date ("it's 3/14/1988 thanks", "John Smith 3/14/1988").
_FEW_WORDS = 3


def _other_words(text: str) -> int:
    flat = " ".join(str(text or "").lower().replace("’", "'").split())
    for pattern in (_ISO, _NUMERIC, _MONTH_FIRST, _DAY_FIRST):
        flat = pattern.sub(" ", flat)
    return len(re.findall(r"[a-z]+(?:'[a-z]+)?", flat))


# Eval np-03 (2026-11-03): "I'd like to book. Brooke Tanner, 02/09/1993" was not counted (more than 3
# other words, no birth word, not asked yet) and Elliot asked for the date of birth again. A full
# date at least this many years back can only be a date of birth (never a visit day or a recent
# injury), unless the text is about something that happened ("hurt", "surgery", "since", ...).
ONLY_BIRTH_YEARS = 10
_EVENT_WORDS = re.compile(r"\b(?:hurt\w*|injur\w*|surger(?:y|ies)|accident|fell|fall(?:en)?|"
                          r"broke|broken|sprain\w*|tore|torn|since|happened|operation|"
                          r"diagnos\w*)\b", re.IGNORECASE)
# A full name right before the date ("Brooke Tanner, 02/09/1993"): two capitalised words, not a
# month, weekday or holiday ("Labor Day 9/1/2025").
_NAME_BEFORE = re.compile(r"(?:^|[^\w'])(?P<first>[A-Z][a-z'-]+)\s+(?P<last>[A-Z][a-z'-]+)"
                          r"\s*[,:-]?\s*$")
_NOT_NAMES = re.compile(r"^(?:jan\w*|feb\w*|mar\w*|apr\w*|may|june?|july?|aug\w*|sep\w*|oct\w*|"
                        r"nov\w*|dec\w*|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*|day|"
                        r"eve|i|im|i'm|my|the|on|in|at|and|dr|doctor)$", re.IGNORECASE)


def _date_starts(text: str) -> list[int]:
    flat = " ".join(str(text or "").replace("’", "'").split())
    lower = flat.lower()
    return sorted(match.start() for pattern in (_ISO, _NUMERIC, _MONTH_FIRST, _DAY_FIRST)
                  for match in pattern.finditer(lower))


def next_to_name(text: str, name: tuple[str, ...] = ()) -> bool:
    """A date in the text comes right after a name: the name on file ("Smith 3/14/1988") or a
    capitalised first and last name ("Brooke Tanner, 02/09/1993")."""
    flat = " ".join(str(text or "").replace("’", "'").split())
    known = [word for part in name for word in str(part or "").split() if len(word) > 1]
    on_file = (re.compile(r"(?:^|\W)(?:" + "|".join(re.escape(word) for word in known)
                          + r")\s*[,:-]?\s*$", re.IGNORECASE) if known else None)
    for start in _date_starts(flat):
        before = flat[:start]
        if on_file is not None and on_file.search(before):
            return True
        match = _NAME_BEFORE.search(before)
        if match and not (_NOT_NAMES.match(match["first"]) or _NOT_NAMES.match(match["last"])):
            return True
    return False


def given_as_birth_date(text: str, asked: str = "", *, today: date | None = None,
                        name: tuple[str, ...] = (), asked_before: bool = False) -> bool:
    """The patient's text gives their date of birth: it says so, Elliot's text before it (`asked`)
    asked about it, or it is little more than the date. Also (np-03) a full date that can only be a
    date of birth, unless the text is about something that happened: one at least
    ONLY_BIRTH_YEARS back, one right after a name, or one sent while a date of birth question
    earlier in the chat is still unanswered (`asked_before`)."""
    if (_BIRTH_WORDS.search(text) or _BIRTH_WORDS.search(asked)
            or _other_words(text) <= _FEW_WORDS):
        return True
    today = today or real_today()
    value = birth_date_in(text, today)
    if value is None or _EVENT_WORDS.search(text):
        return False
    return bool(asked_before or value <= years_before(today, ONLY_BIRTH_YEARS)
                or next_to_name(text, name))


@dataclass(frozen=True)
class BirthdayState:
    confirmed: date | None      # the date of birth this chat settled (the booking uses it)
    pending: date | None        # a different date given once, waiting for the double-check
    differs: bool               # `confirmed` is not the date on file (staff should see it)
    given: tuple[date, ...]     # every possible date of birth the patient typed, oldest first


def _plain_yes(text: str) -> bool:
    words = " ".join(re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'")).split())
    return bool(_YES.match(words))


def birthday_state(lead: LeadView, history: list[Turn], today: date | None = None) -> BirthdayState:
    """What the chat has settled about the date of birth (from the patient's own texts only)."""
    today = today or real_today()
    on_file = None if lead.is_new_person else lead.date_of_birth
    confirmed: date | None = None
    pending: date | None = None
    given: list[date] = []
    asked_double_check = False
    asked = ""  # our latest text before the patient's (a question split over two texts counts)
    asked_open = False  # Elliot asked for the date of birth and none was given since (np-03)
    name = (lead.first_name, lead.last_name)
    month_day: tuple[int, int] | None = None  # their last text gave a month and day, no year
    for turn in history:
        if turn.role != "patient":
            asked = turn.text
            if turn.role == "elliot":
                asked_double_check = pending is not None and bool(DOUBLE_CHECK.search(turn.text))
                asked_open = asked_open or bool(_BIRTH_WORDS.search(turn.text))
            continue
        as_birth = given_as_birth_date(turn.text, asked, today=today, name=name,
                                       asked_before=asked_open)
        value = birth_date_in(turn.text, today)
        if value is not None and not as_birth:
            value = None  # a date that is not their date of birth ("hurt it on 3/2/2024")
        if value is None and month_day is not None:
            value = year_for(turn.text, month_day, asked, today)  # "June 3", then "1989"
        month_day = month_day_only(turn.text, today) if value is None and as_birth else None
        if value is None:
            if asked_double_check and _plain_yes(turn.text):
                confirmed, pending = pending, None
            asked_double_check = False
            continue
        given.append(value)
        asked_double_check = False
        asked_open = False
        if on_file is None or value == on_file:
            confirmed, pending = value, None
        elif pending is not None or confirmed is not None:
            # Typed again, a second different date, or a correction after one was settled: the
            # patient's own date (voice rule), shown to staff.
            confirmed, pending = value, None
        else:
            pending = value
    differs = confirmed is not None and on_file is not None and confirmed != on_file
    return BirthdayState(confirmed, pending, differs, tuple(given))


def context_line(state: BirthdayState) -> str:
    """The Context value for "Date of birth confirmed in this chat"."""
    if state.confirmed is not None:
        return "yes (never ask for it again)"
    if state.pending is not None:
        return ("not yet: the date they typed is not the one in our records; ask once to "
                "double-check it")
    return "no"


@dataclass(frozen=True)
class Check:
    code: str
    output: str
    summary: str


def check(lead: LeadView, history: list[Turn], words: str | None,
          today: date | None = None) -> Check:
    """check_birthday: the patient's words (or YYYY-MM-DD) against the date on file and the chat."""
    today = today or real_today()
    parsed = parse_birth_date(words, today)
    year_only = _YEAR_ONLY.match(" ".join(str(words or "").lower().replace("’", "'").split()))
    if parsed.value is None and year_only:
        # The model passed only the year the patient sent after "June 3": the chat holds the date.
        given = birthday_state(lead, history, today).given
        if given and str(given[-1].year).endswith(year_only["year"].lstrip("'")):
            parsed = Parsed(given[-1])
    if parsed.value is None:
        if parsed.need_year:
            return Check("NEED_YEAR", (
                "NEED_YEAR: they gave a month and day but no year. Ask once for their full date of "
                "birth with the year (\"Thanks! And the year you were born?\"). Never guess one."),
                "Date of birth: no year")
        return Check("UNREADABLE", (
            "UNREADABLE: no date of birth could be read. Ask them to type it with the year, like "
            "3/14/1988."), "Date of birth: could not read it")
    if not possible(parsed.value, today):
        return Check("NOT_POSSIBLE", (
            "NOT_POSSIBLE: that date of birth can't be right (it is in the future, less than a year "
            "ago, or over 120 years ago). Ask them kindly to check it and type it again with the "
            "year. Never say a date or year back to them."), "Date of birth: not possible")
    state = birthday_state(lead, history, today)
    if parsed.value not in state.given:
        # Only the patient's own texts count (the Context line and the booking read the same), so
        # a date the model worked out from words the code cannot read, or a date that was not given
        # as their date of birth ("I hurt it on 3/2/2024"), is asked for once.
        return Check("UNREADABLE", (
            "UNREADABLE: the patient has not typed that as their date of birth in a form we can "
            "read. Ask: \"To confirm it's you, what's your date of birth?\" (with the year, like "
            "3/14/1988)."),
            "Date of birth: not typed in a readable form")
    on_file = None if lead.is_new_person else lead.date_of_birth
    if on_file is None:
        return Check("NEW", (
            "NEW: date of birth noted; identity done. Call confirm_visit now for the time they "
            "picked (once you have everything else it needs). Never ask for the date of birth "
            "again."), "Date of birth given (none on file)")
    if parsed.value == on_file:
        return Check("MATCH", (
            "MATCH: the date of birth matches our records; identity done. Call confirm_visit now "
            "for the time they picked. Never ask for the date of birth again."),
            "Date of birth matches our records")
    if state.confirmed == parsed.value:
        return Check("CONFIRMED_DIFFERENT", (
            "CONFIRMED_DIFFERENT: they confirmed their date of birth; it differs from our records, "
            "so we book with theirs and our staff will see it. Identity done: call confirm_visit "
            "now. Say nothing about our records and never ask for it again."),
            "Date of birth differs from our records: patient confirmed theirs (staff to check)")
    return Check("DIFFERENT", (
        "DIFFERENT: that is a real date, but not the one in our records. Ask them once to "
        "double-check it, in these words: \"Could you double-check that date of birth for me?\" "
        "Never say or hint at the date on file, its month, day or year. Their answer settles it "
        "(the date again, a yes, or a corrected date): then call check_birthday with it."),
        "Date of birth differs from our records: asked to double-check")
