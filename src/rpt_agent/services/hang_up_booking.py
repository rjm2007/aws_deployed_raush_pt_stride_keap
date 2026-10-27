"""Book a time the patient confirmed on the call but hung up before Elliot could book it.

Owner's rule (2026-10-27): the patient's "yes" to Elliot's booking confirmation ("So that's
Tuesday the sixteenth at nine with Thao at our Laguna Niguel office. Shall I book it?") books the
visit, even if they hang up a second later, before the book_appointment tool ran. Without a "yes"
to that question nothing is booked and the cadence simply continues.

`book_confirmed_time_after_hang_up` is called from the end-of-call report
(delivery.process_vapi_end_report) for an answered call whose outreach event is still unsettled.
It books through slot_booking.book_slot, so every normal guarantee applies (hold, live re-check,
Stride create, mark_booked, confirmation text, Keap, Needs Review on failure). When the patient
said yes but the time cannot be booked safely (which offered time is unclear, no last name, the
birthday was not confirmed, the time was taken, ...), the lead goes to Needs Review for the front
desk. Booking switched off (production) -> nothing happens here.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..db import transaction
from ..observability import WorkflowTrace
from .review import flag_lead_for_review

HUNG_UP_CODE = "booking_said_yes"
HUNG_UP_REASON = "Patient said yes to {when} and hung up before the booking finished; book by hand"

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_UNITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
          "nineteen")
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
_ORDINALS = dict(enumerate(
    ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
     "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
     "eighteenth", "nineteenth", "twentieth"), start=1))
_ORDINALS[30] = "thirtieth"

# The patient's first reply after the confirmation decides, then every later line is checked too
# (Vapi splits speech at pauses: "Yes." + "Wait, actually make it eleven." is not a yes). Farewells
# and filler ("bye", "thank you", "I have to run", a leading "hello") are dropped first: "Yes. Bye."
# is a yes (tester, 2026-10-27).
# - only yes words (+ filler) -> "yes": book;
# - a yes word with something unclear (a "but", a question, a trailing "so" / "um", extra words)
#   -> "maybe": Needs Review;
# - a no / change / "let me check" word, or no yes word at all -> None: nothing, cadence continues.
_FAREWELL = re.compile(
    r"\b(?:bye bye|good ?bye|bye|thank you|thanks|elliot|maam|sir|"
    r"i (?:have|got) to (?:go|run)|i gotta (?:go|run)|gotta (?:go|run)|got to (?:go|run)|"
    r"talk (?:to you )?later|see you then|see you|have a (?:good|nice|great) (?:day|one))\b")
_GREETING = re.compile(r"^\s*(?:(?:hello|hi|hey)\b[\s?.!,]*)+", re.IGNORECASE)
# Rewritten before the words are read: "no problem" is not a no, "why not" is a yes.
_REWRITES = (
    (re.compile(r"\bno (?:thats|its) (?:fine|okay|ok)\b"), " fine unclear "),  # agreed, unclear
    (re.compile(r"\bno problem\b"), " fine "),
    (re.compile(r"\b(?:uh huh|mm hmm|mmhmm|mhm|lets do it|lets do that|book it|book that|"
                r"that works|sure thing|go for it|of course|go ahead|sounds (?:good|great)|please do|do it|"
                r"why not|ill take it)\b"), " yes "),
)
_YES_CORE = frozenset({
    "yes", "yeah", "yep", "yup", "ya", "yah", "sure", "ok", "okay", "alright", "absolutely",
    "definitely", "perfect", "please", "correct", "great", "fine",
})
_YES_FILLER = frozenset({
    "that", "thats", "it", "good", "uh", "um", "mm", "oh", "so", "right", "well", "for", "me",
    "works", "lets", "and",
})
# A no or a change of plan: never a yes and no card ("Okay, let me check with my wife").
_NOT_YES = frozenset({
    "no", "not", "nope", "nah", "instead", "actually", "wait", "different", "another", "change",
    "else", "rather", "hold", "dont", "cant", "later", "maybe", "hmm", "think", "earlier", "other",
    "unless", "check", "call", "back", "let", "ask",
})
# Agreed, but something is unclear: Needs Review ("Sure, but can my wife come too?").
_UNCLEAR = frozenset({"but", "what", "which", "how", "when", "where", "why", "who"})
_TRAILING = frozenset({"so", "um", "uh", "and", "but"})


# Elliot's confirmation (config/vapi_booking_assistant_prompt.md, step 3f2): "So that's
# [day] the [date] at [time] with [therapist] at our [clinic] office. Shall I book it?" Tolerated
# (tester, 2026-10-27): "that is", no "so", and the question as "shall / should / can I (go ahead
# and) book it / that (for you)", "(do you) want me to book it", "would you like me to book it".
_BOOK_QUESTION = re.compile(
    r"\b(?:(?:shall|should|can|may) i|(?:do you )?want me to|would you like me to) "
    r"(?:go ahead and )?(?:book (?:it|that|this|you in)|lock (?:it|that|this) in)\b")
_SUMMARY = re.compile(r"\b(?:so )?that(?:s| is) ")
# The tool's own waiting line ("One moment, please, while I confirm your appointment") is a separate
# bot item after the tool call in Vapi's report; it is not Elliot's last word.
_WAITING = re.compile(r"\bone moment please while i\b")
# Every waiting line Vapi can play for the booking tools (request-start and request-response-delayed
# `content` in config/vapi_booking_tools.json and config/vapi_booking_tools_human.json). Kept here
# because the Docker image does not ship config/; tests/test_hang_up_waiting_lines.py keeps it in
# sync with both files. Why (2026-10-27): the human copy of Elliot plays "Perfect, booking that now."
# after the patient's yes; read as Elliot's last line, it hid the confirmation and the yes was lost.
TOOL_WAITING_LINES = (
    # config/vapi_booking_tools.json
    "One moment, please, while I check our availability.",
    "Thank you. One moment, please, while I confirm your appointment.",
    "Thanks for your patience, I'm still confirming that for you.",
    "One moment, please.",
    # config/vapi_booking_tools_human.json
    "Sure, let me look.",
    "Okay, one sec.",
    "Let me see what's open.",
    "Let me check.",
    "Perfect, booking that now. It'll just take a few seconds.",
    "Great, I'll lock that in. Just a few seconds.",
    "Okay, booking that for you now. Just a few seconds.",
    "Thanks for waiting, nearly there.",
    "Sure, one sec.",
    "Let me pull that up.",
)
# The book_appointment lines of both files (subset of TOOL_WAITING_LINES): they play right after
# the patient's yes, so one cut to a single word ("Perfect,") is still a waiting line there.
BOOKING_WAITING_LINES = (
    "Thank you. One moment, please, while I confirm your appointment.",
    "Thanks for your patience, I'm still confirming that for you.",
    "Perfect, booking that now. It'll just take a few seconds.",
    "Great, I'll lock that in. Just a few seconds.",
    "Okay, booking that for you now. Just a few seconds.",
    "Thanks for waiting, nearly there.",
)
# Vapi's own stock fillers while a tool is slow.
_STOCK_FILLERS = ("Sorry, a few more seconds.", "Just a sec.", "Just a second.", "One moment.",
                  "Give me a moment.", "Give me a second.", "Hold on a moment.", "Hold on a sec.",
                  "This'll just take a sec.")


def _is_waiting_line(words: str) -> bool:
    """A tool waiting line or stock filler, whole or cut short (Vapi stores a line cut off mid-word
    when the call ends: "One moment, please, while I check our available"). A cut line must keep at
    least two words, so a lone "Sure." or "Okay." from Elliot is never skipped."""
    plain = _plain(words)
    if not plain:
        return False
    if _WAITING.search(plain):
        return True
    cut = len(plain.split()) >= 2
    return any(plain == line or (cut and line.startswith(plain)) for line in _WAITING_PLAIN)


# What the card says for each book_slot reply that was not BOOKED.
_PLAIN_WHY = {
    "TAKEN": "that time was no longer free",
    "NOT_AVAILABLE": "that time was no longer free",
    "FOLLOW_UP": "the booking system could not finish it",
    "UNCONFIRMED": "the booking system did not answer clearly",
    "PATIENT_EXISTS": "the patient is already in Stride",
}


def _plain(text: str) -> str:
    """Lower case, apostrophes dropped ("that's" -> "thats"), punctuation to spaces."""
    text = text.lower().replace("’", "'").replace("'", "")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_WAITING_PLAIN = tuple(_plain(line) for line in TOOL_WAITING_LINES + _STOCK_FILLERS)
_BOOKING_WAITING_PLAIN = tuple(_plain(line) for line in BOOKING_WAITING_LINES)


def _cut_booking_waiting_line(words: str) -> bool:
    """Any start of a book_appointment waiting line, even one word ("Perfect," / "Okay,")."""
    plain = _plain(words)
    return bool(plain) and any(line.startswith(plain) for line in _BOOKING_WAITING_PLAIN)


def _number_words(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    tens, unit = divmod(n, 10)
    return _TENS[tens] + (f" {_UNITS[unit]}" if unit else "")


def _ordinal_words(n: int) -> str:
    if n in _ORDINALS:
        return _ORDINALS[n]
    tens, unit = divmod(n, 10)
    return f"{_TENS[tens]} {_ORDINALS[unit]}"


def _reply_words(text: str) -> tuple[list[str], bool]:
    """The reply's words after farewells / filler and rewrites, and whether it asks something."""
    text = _GREETING.sub("", text)
    plain = _FAREWELL.sub(" ", _plain(text))
    for pattern, replacement in _REWRITES:
        plain = pattern.sub(replacement, plain)
    return plain.split(), "?" in text


def reply_kind(text: str) -> str | None:
    """"yes", "maybe" (agreed, but unclear), "empty" (only farewell / filler), or None."""
    words, question = _reply_words(text)
    found = set(words)
    if not found or found <= {"uh", "um", "oh", "mm", "and", "so", "well"}:
        return "empty"
    if found & _NOT_YES or not found & _YES_CORE:
        return None
    if question or found & _UNCLEAR or words[-1] in _TRAILING:
        return "maybe"
    return "yes" if found <= _YES_CORE | _YES_FILLER else "maybe"


def _first_reply(replies: list[str]) -> str | None:
    """The first real reply decides; after a yes, any later line with a no / change word or a
    question makes it "maybe" (Needs Review, never a booking). A leading "Hello?" is dropped."""
    for at, reply in enumerate(replies):
        kind = reply_kind(reply)
        if kind == "empty":
            continue
        if kind == "yes":
            for later in replies[at + 1:]:
                words, question = _reply_words(later)
                if question or set(words) & (_NOT_YES | _UNCLEAR):
                    return "maybe"
        return kind
    return None


def plain_yes(replies: list[str]) -> bool:
    return _first_reply(replies) == "yes"


def booking_tool_called(message: dict) -> bool:
    """Whether the call's report shows a book_appointment tool call (a log hint only).

    Vapi's end-of-call report lists the call in `artifact.messages` (role "tool_calls" with
    `toolCalls[].function.name`, role "tool_call_result" with `name`) and in
    `artifact.messagesOpenAIFormatted` (role "assistant" with `tool_calls[].function.name`).
    Vapi may log a tool call that never reached us (the patient hung up), so whether the booking
    ran is decided from the database, never from this.
    """
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    lists = (artifact.get("messages"), artifact.get("messagesOpenAIFormatted"), message.get("messages"))
    for items in lists:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("role") == "tool_call_result" and item.get("name") == "book_appointment":
                return True
            for call in (item.get("toolCalls") or item.get("tool_calls") or []):
                function = call.get("function") if isinstance(call, dict) else None
                name = (function or {}).get("name") or (call.get("name") if isinstance(call, dict) else None)
                if name == "book_appointment":
                    return True
    return False


def _names_a_time(what: str) -> bool:
    return bool(re.search(rf"\b(?:{'|'.join(_WEEKDAYS)})\b", what)
                or re.search(r"\bat (?:\d|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
                             r"twelve|noon)\b", what))


def find_confirmation(turns: list[tuple[str, str]]) -> tuple[int, str, bool] | None:
    """(index, spoken "[day] ... [clinic]" part, complete) when Elliot's last line is a confirmation.

    complete=False: it reads like the confirmation but the question was cut short (the patient
    spoke over it, so Vapi stored the line cut): never booked from that, only reviewed.
    """
    spoken = [i for i, (role, words) in enumerate(turns)
              if role == "bot" and words.strip() and not _is_waiting_line(words)]
    if not spoken:
        return None
    index = spoken[-1]
    # Tester (2026-10-27): the booking waiting line cut to its first word ("Perfect,") right after
    # the patient's yes to the confirmation is still the waiting line. Only there: a lone "Okay."
    # anywhere else (or after a reply that is not a plain yes) stays Elliot's last line.
    if len(spoken) >= 2 and _cut_booking_waiting_line(turns[index][1]):
        before = spoken[-2]
        replies = [w for role, w in turns[before + 1:index] if role == "user" and w.strip()]
        if replies and plain_yes(replies):
            found = find_confirmation(turns[:index])
            if found and found[0] == before:
                return found
    plain = _plain(turns[index][1])
    question, summary = _BOOK_QUESTION.search(plain), _SUMMARY.search(plain)
    if question:
        if summary and summary.end() <= question.start():
            return index, plain[summary.end():question.start()].strip(), True
        return index, plain, True  # "Can I book you in for Tuesday the sixteenth at nine?"
    # Cut short: the slot details and nothing after them. A line with any other question (an offer
    # "... Would that work?", the birthday check) is not a confirmation (tester, 2026-10-27).
    if (summary and _names_a_time(plain[summary.end():]) and "?" not in turns[index][1]
            and not re.search(r"\b(?:birthday|last name|date of birth|work for you)\b", plain)):
        return index, plain[summary.end():].strip(), False
    return None


def _confirmed_yes(turns: list[tuple[str, str]]) -> tuple[int, str] | None:
    """(index, spoken time) when Elliot's last line is the full confirmation and the patient said yes."""
    found = find_confirmation(turns)
    if not found or not found[2]:
        return None
    replies = [words for role, words in turns[found[0] + 1:] if role == "user" and words.strip()]
    return (found[0], found[1]) if plain_yes(replies) else None


def _matches(slot: dict[str, Any], what: str, names: set[str]) -> bool:
    """Whether the spoken "[day] the [date] at [time] with [therapist]" is this offered slot."""
    day: date = slot["local_date"]
    weekdays = {w for w in _WEEKDAYS if re.search(rf"\b{w}\b", what)}
    if weekdays and _WEEKDAYS[day.weekday()] not in weekdays:
        return False
    months = {m for m in _MONTHS if re.search(rf"\b{m}\b", what)}
    if months and _MONTHS[day.month - 1] not in months:
        return False
    rest, days = what, set()
    for n in range(31, 0, -1):  # "twenty first" before "first"
        pattern = rf"\b(?:{_ordinal_words(n)}|{n}(?:st|nd|rd|th))\b"
        if re.search(pattern, rest):
            days.add(n)
            rest = re.sub(pattern, " ", rest)
    if days and day.day not in days:
        return False
    if not weekdays and not days:
        return False
    hour, minute = slot["local_time"].hour, slot["local_time"].minute
    h12 = hour % 12 or 12
    if minute:
        minute_words = _number_words(minute) if minute >= 10 else f"oh {_number_words(minute)}"
        forms = [f"{_number_words(h12)} {minute_words}", f"{h12} {minute:02d}"]
    else:
        forms = [_number_words(h12), f"{h12} 00", str(h12)] + (["noon"] if hour == 12 else [])
    timed = False
    for form in forms:
        for found in re.finditer(rf"\bat {form}\b", what):
            after = what[found.end():]
            if not minute and form != "noon" and re.match(
                    r" (?:oh|ten|fifteen|twenty|thirty|forty|fifty|\d)\b", after):
                continue  # "at ten" inside "at ten thirty"
            if re.match(r" (?:in the morning|am|a m)\b", after) and hour >= 12:
                continue
            if re.match(r" (?:in the afternoon|in the evening|pm|p m|at night)\b", after) and hour < 12:
                continue
            timed = True
    if not timed:
        return False
    said = {name for name in names if re.search(rf"\b{name}\b", what)}
    mine = set(_plain(slot["display_name"]).split())
    return not said or bool(said & mine)


_NAME_FILLER = frozenset({"its", "it", "is", "my", "last", "name", "uh", "um", "sure", "yeah", "yes",
                          "oh", "so", "okay", "ok"})


def _last_name_from_call(turns: list[tuple[str, str]], before: int) -> str | None:
    """The last name the patient gave after "And what's your last name?", when it is clear.

    Kept as spoken ("O'Brien", "Smith-Jones"); only a lower-case first letter is raised.
    """
    asked = [i for i in range(before) if turns[i][0] == "bot"
             and "whats your last name" in _plain(turns[i][1])]
    if not asked:
        return None
    start = asked[-1]
    following = [i for i in range(start + 1, before) if turns[i][0] == "bot" and turns[i][1].strip()]
    if following and re.search(r"\b(spell|sorry|again|repeat|catch)\b", _plain(turns[following[0]][1])):
        return None
    end = following[0] if following else before
    said = " ".join(w for role, w in turns[start + 1:end] if role == "user").replace("\u2019", "'")
    tokens = [t.strip("'-") for t in re.findall(r"[A-Za-z][A-Za-z'-]*", said)]
    tokens = [t for t in tokens if t and _plain(t) not in _NAME_FILLER]
    if len(tokens) > 1 and all(len(t) == 1 for t in tokens):
        tokens = ["".join(tokens).lower()]  # spelled out letter by letter
    if len(tokens) != 1 or len(tokens[0]) < 2:
        return None
    name = tokens[0]
    return name[0].upper() + name[1:]


def _birthday_confirmed(turns: list[tuple[str, str]], before: int) -> bool:
    """Elliot checked the birthday on file ("...your birthday's ...?") and the patient said yes."""
    checks = [i for i in range(before) if turns[i][0] == "bot" and "your birthday" in _plain(turns[i][1])]
    if not checks:
        return False
    start = checks[-1]
    later = [i for i in range(start + 1, before) if turns[i][0] == "bot" and turns[i][1].strip()]
    if any(re.search(r"\b(right one|date of birth|birthday)\b", _plain(turns[i][1])) for i in later):
        return False  # the patient gave another date, or Elliot asked again
    end = later[0] if later else before
    replies = [w for role, w in turns[start + 1:end] if role == "user" and w.strip()]
    return bool(replies) and plain_yes(replies)


def _settle_for_review(lead_id: str, event_id: int, call_id: str, when: str, why: str) -> None:
    with transaction() as conn:
        # Lead before event, as apply_call_outcome and the voice booking lock them.
        lead = conn.execute("select needs_review from leads where id=%s for update",
                            (lead_id,)).fetchone()
        conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by='webhook',"
            "outcome='manual',failure_reason=%s,updated_at=now() "
            "where id=%s and status in ('in_flight','attempted')",
            ("patient said yes to a time and hung up before it was booked", event_id),
        )
        # book_slot may already have flagged the lead with its own card (UNCONFIRMED: "confirm in
        # Stride", never "book by hand"); one card is enough.
        if not (lead and lead["needs_review"]):
            flag_lead_for_review(
                conn, lead_id, HUNG_UP_REASON.format(when=when), code=HUNG_UP_CODE,
                source="booking", outreach_event_id=event_id,
                details={"call_id": call_id, "confirmed": when, "why": why},
            )


def _already_handled(lead_id: str, event_id: int) -> bool:
    """Another path got there first (a second report, or the booking tool itself)."""
    with transaction() as conn:
        lead = conn.execute(
            "select status,exists(select 1 from appointments a where a.lead_id=leads.id "
            "and a.state in ('booking','scheduled','unknown')) as has_booking "
            "from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if lead and lead["status"] == "booked":
            conn.execute(
                "update outreach_events set status='delivered',settled_at=now(),settled_by='webhook',"
                "outcome='booked',updated_at=now() where id=%s and status in ('in_flight','attempted')",
                (event_id,),
            )
    return bool(lead and (lead["status"] == "booked" or lead["has_booking"]))


def book_confirmed_time_after_hang_up(
    trace: WorkflowTrace, message: dict, turns: list[tuple[str, str]], *, lead_id: str,
    event_id: int, call_id: str,
) -> str | None:
    """Book the time the patient said yes to when they hung up before the booking tool ran.

    Why (owner, 2026-10-27): a "yes" to "So that's ... Shall I book it?" is a booking, even if the
    patient hangs up a second later. Returns the call's outcome ("booked", or "manual" when the
    lead went to Needs Review or another path is booking it), or None when this does not apply (no
    yes to a confirmation, a booking already exists, booking switched off): the normal end-of-call
    path goes on and the cadence continues.
    """
    from . import slot_booking  # local: slot_booking is the demo booking engine

    found = find_confirmation(turns)
    if not found:
        return None
    index, what, complete = found
    kind = _first_reply([w for role, w in turns[index + 1:] if role == "user" and w.strip()])
    if kind is None:
        return None  # no yes (a no, a change, a question, or silence): the cadence continues
    with transaction() as conn:
        lead = conn.execute(
            "select l.first_name,l.last_name,l.full_name,l.date_of_birth,"
            "coalesce(ps.stride_booking_enabled,false) as booking_enabled,"
            "exists(select 1 from appointments a where a.lead_id=l.id "
            "and a.state in ('booking','scheduled','unknown')) as has_booking "
            "from leads l left join practice_settings ps on ps.practice_id=l.practice_id "
            "where l.id=%s",
            (lead_id,),
        ).fetchone()
        if not lead or not lead["booking_enabled"]:
            return None
        # Decided from the database, not from the report's tool calls (Vapi may list one that never
        # reached us): a booking running, made or unclear for this lead is never repeated here. A
        # failed attempt (e.g. TAKEN, then a new time confirmed) does not stop it.
        if lead["has_booking"] or slot_booking._booked_elsewhere(conn, lead_id):
            return None
        offers = conn.execute(
            "select distinct on (s.id) s.id,s.local_date,s.local_time,c.display_name,"
            "bl.name as location_name,ct.name as case_type_name "
            "from slot_offers o join availability_slots s on s.id=o.slot_id "
            "join clinicians c on c.id=s.clinician_id "
            "join booking_locations bl on bl.id=s.booking_location_id "
            "join case_types ct on ct.id=o.case_type_id "
            "where o.call_id=%s and o.lead_id=%s order by s.id,o.offered_at desc",
            (call_id, lead_id),
        ).fetchall()
    trace.log("hang_up_confirmation_found", event_id=event_id, offers=len(offers), reply=kind,
              complete=complete, tool_in_report=booking_tool_called(message))
    spoken = re.search(r"that(?:[\u2019']?s| is)\s+(.+?)[\s.,!?]*(?:shall|should|can|may|do you|"
                       r"want|would)\b", turns[index][1], re.IGNORECASE)
    when = (spoken[1] if spoken else what)[:200]

    def refuse(why: str) -> str:
        trace.log("hang_up_booking_refused", event_id=event_id, why=why)
        _settle_for_review(lead_id, event_id, call_id, when, why)
        return "manual"

    if not complete:
        return refuse("the confirmation question was cut short")
    if kind != "yes":
        return refuse("the patient's answer was not a plain yes")
    names = {token for offer in offers for token in _plain(offer["display_name"]).split()
             if len(token) > 2}
    matching = [offer for offer in offers if _matches(offer, what, names)]
    if len(matching) != 1:
        return refuse("could not tell which offered time was confirmed")
    slot = matching[0]
    when = (f"{slot['local_date'].strftime('%A, %B %d').replace(' 0', ' ')} at "
            f"{slot['local_time'].strftime('%I:%M %p').lstrip('0')} with {slot['display_name']}")
    full = (lead["full_name"] or "").split()
    first = (lead["first_name"] or "").strip() or (full[0] if full else "")
    last = (lead["last_name"] or "").strip() or " ".join(full[1:]) or _last_name_from_call(turns, index)
    if not first or not last:
        return refuse("no clear last name")
    if not lead["date_of_birth"] or not _birthday_confirmed(turns, index):
        return refuse("birthday on file not confirmed on the call")
    arguments = {
        "date": slot["local_date"].isoformat(),
        "time": slot["local_time"].strftime("%I:%M %p").lstrip("0"),
        "first_name": first, "last_name": last,
        "date_of_birth": str(lead["date_of_birth"])[:10],
        "clinician_name": slot["display_name"], "slot_id": str(slot["id"]),
        "location": slot["location_name"], "case_type": slot["case_type_name"],
        "outreach_event_id": str(event_id),
    }
    try:
        reply = slot_booking.book_slot(trace, lead_id=lead_id, call_id=call_id, arguments=arguments,
                                       source="voice_agent")
    except Exception as exc:  # noqa: BLE001 - the front desk follows up instead
        trace.log("hang_up_booking_error", event_id=event_id, error_category=type(exc).__name__)
        return refuse("the booking system had an error")
    code = reply.split(":", 1)[0]
    if code in {"ALREADY_BOOKED", "IN_PROGRESS"} and _already_handled(lead_id, event_id):
        # Two reports at once, or the tool's own request arrived late: that booking stands.
        trace.log("hang_up_booking_already_handled", event_id=event_id, reply=code)
        with transaction() as conn:
            row = conn.execute("select outcome from outreach_events where id=%s",
                               (event_id,)).fetchone()
        return (row or {}).get("outcome") or "manual"
    if code != "BOOKED":
        return refuse(_PLAIN_WHY.get(code, "the booking did not go through"))
    trace.log("hang_up_booking_booked", event_id=event_id)
    with transaction() as conn:
        row = conn.execute("select outcome from outreach_events where id=%s", (event_id,)).fetchone()
    return (row or {}).get("outcome") or "booked"
