"""The SMS agent ("Elliot by text"): one reply to a patient's texts (docs/SMS_AGENT.md).

A plain OpenAI function-calling loop, no framework (SMS agent plan section 2): the prompt, a Context
block built by code, the text history; the model calls our tools; we run them and send the results
back until it writes the reply. Memory is our database, not the model: every reply starts fresh.

- Responses API, store=False (nothing kept at OpenAI) with the reasoning items passed back between
  tool rounds (include reasoning.encrypted_content), per OpenAI's function-calling guide. Only for
  reasoning models (gpt-5*, gpt-6*, o-series): gpt-4.1 has no reasoning items and rejects the include.
- At most `sms_agent_max_rounds` tool rounds, the whole run under `sms_agent_timeout_seconds`: a
  model that is slow or loops gives an error (nothing sent, the worker flags staff), never a half
  answer.
- Texts the patient must get word for word (card, booked, front desk, 911, intro) come from the
  tools as fixed texts and are sent exactly as written: fixed texts first, then the model's own
  short text (if any), the read-back card last.
- Logs carry tool names, result codes and counts only, never message text, names or birthdays.
- Code guards on the model's own text (gpt-5.5 eval, 2026-11-02: "All set! You're booked" with no
  booking, its own card text, silence after a yes), because a prompt alone does not hold them:
  a booking claim with no BOOKED / ALREADY_BOOKED tool reply in this run, or a card / "reply YES" /
  "holding" text with no confirm_visit card, is never sent; the model runs once more (tool_choice
  forced to book_visit after a yes to the card, to confirm_visit when its text names a time, else a
  developer note). An empty answer is a no-reply only after a plain thank-you to something Elliot
  finished; otherwise one nudge. A second miss sends nothing invented (`_check_answer`).
- Round 2 (2026-11-02): a model text that repeats one of Elliot's last 3 texts, or writes the
  patient's full date of birth with the year, is dropped and re-run once with a note; a model call
  that times out with enough time left is tried once more (`CALL_TIMEOUT_SECONDS`).
- Round 3 (2026-11-02): a text whose sentences were all in Elliot's last 3 texts counts as a repeat
  (x-05); a text promising the front desk with no hand-off behind it is dropped and re-run once
  with a note (x-04).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from . import sms_agent_birthday, sms_agent_sandbox_setup
from . import sms_agent_texts as texts
from .front_desk import CLINIC_TZ
from .front_desk import front_desk_open as clinic_front_desk_open
from .lead_review import SMS_FRONT_DESK_REASONS
from .sms_agent_context import MEDIA_ONLY, LeadView, Turn, context_block, history_items, load_live
from .sms_agent_tools import (
    EMERGENCY_REPEAT_WINDOW,
    FINAL_STATUSES,
    FRONT_DESK_CODES,
    REPEATED_FAILURES,
    AgentTools,
    LiveTools,
    SandboxTools,
    ToolResult,
    call_tool,
    clear_no,
)

__all__ = [
    "AgentAction", "AgentReply", "AgentTools", "LeadView", "LiveTools", "SandboxSetup",
    "SandboxTools", "Turn", "reply_live", "reply_sandbox", "run_agent",
]

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
PROMPT_FILE = CONFIG_DIR / "sms_agent_prompt.md"
TOOLS_FILE = CONFIG_DIR / "sms_agent_tools.json"
# The model writes exactly this when no text should go out ("ok" / "thanks" after a booking).
NO_REPLY = "NO_REPLY"
# What a literal model might write instead of NO_REPLY (copied from an example): never sent.
_EMPTY_ANSWERS = {"", NO_REPLY, "(EMPTY ANSWER)", "(NO REPLY)", "(NO TEXT)", "[]", "()", "EMPTY"}
# history_items puts "[Mon Nov 2, 3:14 PM] " before each turn; a model that copies one must not
# send it to the patient.
_STAMP = re.compile(r"^\[[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M\]\s*")
# Models that return reasoning items (gpt-5*, gpt-6*, the o-series). Only these get
# include=["reasoning.encrypted_content"]: a non-reasoning model (gpt-4.1, picked in the test chat)
# may refuse the request with a 400, and the tab showed a 502.
_REASONING_MODEL = re.compile(r"^(gpt-[56]|o\d)", re.IGNORECASE)
# One model call waits at most this long, so a call that hangs (fact-09, round 2: one gpt-5.5 call
# used the whole 20 s) leaves time to try it once more within sms_agent_timeout_seconds (35 s).
CALL_TIMEOUT_SECONDS = 20.0
# A timed-out call is tried again only with at least this much of the overall time left.
RETRY_MIN_SECONDS = 8.0


def is_reasoning_model(model: str | None) -> bool:
    # "openai/gpt-5-mini" style names (a proxy base URL) count by the part after the slash.
    return bool(_REASONING_MODEL.match(str(model or "").rsplit("/", 1)[-1].strip()))


@dataclass
class AgentAction:
    kind: str          # find_times | list_therapists | confirm_visit | book | report | hand_off | emergency | fixed_text
    summary: str
    result_code: str
    reason: str | None = None   # a hand-off's reason; the test chat sends it back in prior_actions


@dataclass
class AgentReply:
    texts: list[str]
    actions: list[AgentAction]
    no_reply: bool = False
    error: str | None = None
    # The agent itself opted the patient out in this reply (report_outcome do_not_contact): the
    # worker still sends this one reply ("We won't text you again.") although the lead is opted
    # out by the time it sends.
    opted_out: bool = False


@dataclass
class SandboxSetup:
    """One test chat's made-up lead (the dashboard's SMS agent test tab)."""

    who: str = "existing"            # 'existing' | 'new'
    status: str = "in_progress"
    clinic: str = ""
    service: str = ""
    referral_word: str = ""
    first_name: str = ""
    last_name: str = ""
    date_of_birth: date | None = None
    front_desk: str = "auto"         # 'auto' | 'open' | 'closed'
    clock: datetime | None = None    # clinic local time to pretend it is; None = now
    model: str | None = None
    practice_slug: str = field(default="rausch-pt")
    # A Booked test lead's visit, the Context line live writes ("Tue Nov 3 at 9:00 AM with Alan at
    # Laguna Niguel"); empty = "no visit details set" (bk-02: Elliot could not say when it was).
    booked_visit: str = ""
    # 'auto' (status Needs attention = a full review) | 'none' | 'front_desk' (only Elliot's own
    # front-desk card: booking by text goes on; ls-01) | 'full'. sms_agent_sandbox_setup.
    open_review: str = "auto"


# --- prompt and tool files (read once per process) ---------------------------------------------------

@cache
def load_prompt(path: str | None = None) -> str:
    return Path(path or PROMPT_FILE).read_text(encoding="utf-8")


@cache
def _load_tools(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_tools(path: str | None = None) -> list[dict[str, Any]]:
    """Function tools in the Responses API shape. Accepts a list or {"tools": [...]}, and the Chat
    Completions shape ({"type": "function", "function": {...}}), whichever the file uses."""
    data = json.loads(_load_tools(str(path or TOOLS_FILE)))
    items = data.get("tools", []) if isinstance(data, dict) else data
    tools = []
    for item in items:
        inner = item.get("function") if isinstance(item.get("function"), dict) else item
        tool = {"type": "function", "name": inner["name"],
                "description": inner.get("description", ""),
                "parameters": inner.get("parameters") or {"type": "object", "properties": {}}}
        if "strict" in inner:
            tool["strict"] = inner["strict"]
        tools.append(tool)
    return tools


def default_client(settings: Settings):
    import openai  # local: the API starts even if the package is missing

    return openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.assistant_base_url,
                         max_retries=0, timeout=settings.sms_agent_timeout_seconds)


# --- the loop ----------------------------------------------------------------------------------------

def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _as_input(item: Any) -> dict[str, Any]:
    """An output item fed back as input (reasoning with its encrypted content, function calls)."""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    return dict(vars(item))


def _output_text(response: Any) -> str:
    text = _get(response, "output_text")
    if isinstance(text, str):
        return text
    parts = []
    for item in _get(response, "output", []) or []:
        if _get(item, "type") == "message":
            for content in _get(item, "content", []) or []:
                if _get(content, "type") == "output_text":
                    parts.append(_get(content, "text", ""))
    return "".join(parts)


def _clean(text: str) -> str:
    # Plan section 5: no markdown in a text message; no copied time stamp.
    return _STAMP.sub("", text.replace("**", "").replace("__", "").strip()).strip()


def _is_empty_answer(text: str) -> bool:
    return text.upper().strip(" .") in _EMPTY_ANSWERS


def _finish(sent_first: list[str], text: str, sent_last: list[str],
            actions: list[AgentAction], opted_out: bool = False) -> AgentReply:
    """The texts in sending order: fixed texts, then the model's own (if any), the card last
    (config/sms_agent_prompt.md, "Stop rules")."""
    ordered = list(sent_first)
    if text and not _is_empty_answer(text) and text not in ordered:
        ordered.append(text)
    ordered += [card for card in sent_last if card not in ordered]
    return AgentReply(ordered, actions, no_reply=not ordered, opted_out=opted_out)


# --- code guards on the model's own text (gpt-5.5 eval, 2026-11-02) -----------------------------------
#
# The prompt says all of this; the eval showed a model that ignores it (7 chats texted "All set!
# You're booked" with nothing booked, 5 wrote their own card, 9 went silent after a yes). These
# checks hold whatever the model does. They read the model's own text only, never a fixed text.

# A visit claimed as booked. Negations ("not booked yet", "haven't booked") do not match.
_BOOKED_CLAIM = re.compile(
    r"\byou(?:'re| are)\s+(?:all\s+|now\s+)?booked\b"
    r"|\b(?:i|we)(?:'ve| have)?\s+(?:just\s+|now\s+)?(?:booked|scheduled)\s+(?:you|it|that|this|your)\b"
    r"|\b(?:visit|appointment|evaluation|eval|session)\s+(?:is|has\s+been|was)\s+(?:all\s+|now\s+)?"
    r"(?:booked|scheduled|confirmed|set)\b"
    r"|\bsee\s+you\s+(?:on|then|next|this|tomorrow)\b"
    r"|\bconfirmed\s+for\b"
    r"|\byou(?:'re| are)\s+on\s+the\s+(?:calendar|schedule|books)\b"
    r"|^\W*(?:booked|all\s+booked)\b")
# Weaker words that also close a callback ("You're all set, we'll call you at 10"): a claim only
# when no other outcome was recorded in this run and the text is not about a call.
_SET_CLAIM = re.compile(r"\ball\s+set\b|\byou(?:'re| are)\s+(?:set|scheduled|confirmed)\b")
# The model's own card: a "reply YES" request, a held time, a promised card. Never fine.
_CARD_ASK = re.compile(
    r"\b(?:reply|text|respond|answer)(?:\s+with)?\s+[\"']?yes\b"
    r"|\b(?:visit|read-?back|confirmation)\s+card\b"
    r"|\bsend\s+(?:you\s+)?(?:the|a|your)\s+(?:\w+\s+)?card\b"
    r"|\b(?:i'?m|i\s+am|i'?ll\s+be|we'?re|we\s+are)\s+holding\b"
    r"|\bon\s+hold\s+for\s+you\b")
# A read-back of a visit: the model's own card, except for a lead with a visit on the books, where
# "Here's your visit: Tue Nov 3 at 9:00 AM" answers "when is my appointment?" (review, 2026-11-02:
# it was dropped, then the second try failed the reply and flagged staff).
_CARD_READBACK = re.compile(r"\bhere'?s\s+your\s+visit\b|\breview\s+this\s+visit\b")
_CLOCK_TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\b", re.IGNORECASE)
# A plain thank-you / okay (after punctuation and emoji are dropped).
_ACK_WORDS = (r"ok(?:ay)?|k+|kk|alright|all right|got it|sounds good|great|cool|perfect|awesome|nice|"
              r"will do|thanks|thank you(?: so much| very much)?|thx|ty|tysm|appreciate it|"
              r"(?:ok |okay )?i'?ll wait|i will wait|good|np|no problem|"
              r"see you (?:then|soon|there)|i'?ll call (?:them|you|back)|will call (?:them|you)")
_ACK = re.compile(rf"^(?:{_ACK_WORDS})(?: (?:{_ACK_WORDS}))*$")
# A plain yes to the read-back card (the code forces book_visit only then; the tool still checks
# the card and the no-words).
_YES = re.compile(r"^(?:y|ya|ye|yes+|yeah|yea|yep|yup|sure|ok(?:ay)?|k|correct|confirm(?:ed)?|"
                  r"book(?: it| me)?|please do|do it|go ahead|sounds good|perfect|that works|"
                  r"works for me|absolutely|definitely|bet)\b")
# A yes that asks for something else ("yes but can we do 10 instead?"): the code never forces a
# booking on it; the model reads it (review, 2026-11-02).
_YES_BUT = re.compile(r"\b(?:but|instead|actually|rather|different|another|other|earlier|later|"
                      r"change|switch|move)\b")
# Elliot's own opt-out line ("We won't text you again.").
_OPT_OUT_LINE = re.compile(r"\b(?:won'?t|will not) text you again\b|\btexts? (?:are )?stopped\b")
# A promise that the front desk (our team, staff) will act: "our front desk will finish setting up
# your visit", "I'll have our front desk call you". Not a pointer ("call our front desk at ...").
_FRONT_DESK_PROMISE = re.compile(
    r"\b(?:front\s+desk|our\s+team|our\s+staff|someone\s+from\s+our\s+(?:team|office|front\s+desk))"
    r"(?:\s+team)?\s+(?:will|'ll|is\s+going\s+to|are\s+going\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:reach\s+out|call|contact|follow\s+up|get\s+back|get\s+in\s+touch|be\s+in\s+touch|finish|"
    r"set\s+up|take\s+care|help|text|handle|sort)\b"
    r"|\b(?:i'?ll|i\s+will|i'?ve|i\s+have|we'?ll|we\s+will|we'?ve)\s+(?:\w+\s+){0,2}?"
    r"(?:pass(?:ed)?|let|have|ask(?:ed)?|forward(?:ed)?|flag(?:ged)?|notif(?:y|ied)|told|tell)"
    r"\b[^.?!\n]{0,25}\bfront\s+desk\b")
# Tool replies after which the front desk really has it: a hand-off or emergency, a report (case
# change, callback: "our team will call you at 10"), a failed booking's own card, a review that
# stops booking, a tool error ("our team will follow up").
_FRONT_DESK_CODE_PREFIXES = ("HANDED_OFF", "ALREADY_", "REPORTED", "EMERGENCY")
_FRONT_DESK_RESULT_CODES = {*FRONT_DESK_CODES, "REVIEW_OPEN", "ERROR", "CASE_CHANGE"}
# Finished outcomes: a thank-you after these needs no answer.
_DONE_STATUSES = {"booked", "callback_scheduled", "booking_link_sent", "declined"}

BOOK_NOTE = ("Code check: your last answer said the visit is booked, but nothing was booked in this "
             "reply (no book_visit returned BOOKED), so it was not sent. The patient said yes to the "
             "read-back card: call book_visit now for the date, time and therapist on that card.")
CLAIM_NOTE = ("Code check: your last answer said the visit is booked, but nothing was booked (no "
              "book_visit returned BOOKED), so it was not sent. Never say a visit is booked unless "
              "book_visit returned BOOKED. Continue the step you were on: the read-back card comes "
              "only from confirm_visit.")
CARD_FORCED_NOTE = ("Code check: your last answer read back a visit or asked the patient to reply "
                    "YES, but only confirm_visit sends the read-back card (nothing is held), so it "
                    "was not sent. Call confirm_visit now for the date, time and therapist the "
                    "patient chose.")
CARD_NOTE = ("Code check: your last answer read back a visit, asked the patient to reply YES or "
             "promised a card, but only confirm_visit sends the read-back card (nothing is held), so "
             "it was not sent. If the patient has chosen a time, call confirm_visit for it now; "
             "otherwise ask which time they want.")
EMPTY_NOTE = ("Code check: the patient expects a reply; continue the step you were on. Answer "
              "NO_REPLY only to a plain thank-you after something was finished.")
REPEAT_NOTE = ("Code check: you already said that, so it was not sent again. Reply differently in "
               "one short line, or NO_REPLY if nothing new is needed.")
FRONT_DESK_NOTE = ("Code check: your last answer said our front desk will reach out or take care of "
                   "something, but no hand_off was made, so it was not sent. If the patient needs "
                   "the front desk, call hand_off now with the right reason (it sends the front "
                   "desk text); otherwise do not promise it and continue the step you were on.")
BIRTH_DATE_NOTE = ("Code check: your last answer wrote the patient's full date of birth with the "
                   "year, so it was not sent. Never put a birth year in a text, and never read a "
                   "date of birth to the patient: they type theirs and check_birthday checks it. "
                   "Continue the step you were on.")
ASKED_BIRTH_DATE_NOTE = ("Code check: your last answer asked for the patient's date of birth, so it "
                         "was not sent. Context says it is already confirmed in this chat: never ask "
                         "for it again. Continue the step you were on (confirm_visit for their pick, "
                         "book_visit after a yes to the card), or NO_REPLY.")
ASKED_LAST_NAME_NOTE = ("Code check: your last answer asked for the patient's last name, so it was "
                        "not sent. Their last name is on file: never ask for it. If no first name "
                        "is on file, ask only \"What's your first name?\"; otherwise continue the "
                        "step you were on.")
# How many of Elliot's own texts a model text may not repeat (x-02, x-05: the same text twice).
REPEAT_LOOKBACK = 3


def _flat(text: str | None) -> str:
    return " ".join(str(text or "").lower().replace("’", "'").split())


def _words(text: str | None) -> str:
    """Letters, digits and apostrophes only (emoji and punctuation dropped)."""
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", _flat(text)).split())


def _same_words(text: str | None) -> str:
    """Letters and digits only: "Got it!" and "got it" are the same text."""
    return re.sub(r"[^a-z0-9]+", "", _flat(text))


# Sentence ends: . ! ? before a space, or a line break ("(949) 276-5401." stays one sentence).
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str | None) -> list[str]:
    """The text's sentences, each as letters and digits only (_same_words); empty ones dropped."""
    return [key for key in (_same_words(part) for part in _SENTENCE_BREAK.split(str(text or "")))
            if key]


def repeats_recent(text: str, history: list[Turn], lookback: int = REPEAT_LOOKBACK) -> bool:
    """The text is one of Elliot's last `lookback` texts again (case, spaces and punctuation
    ignored), or every sentence in it was already in those texts (x-05, round 3: "Please have Ruth
    text us directly or call our front desk" went out 3 times, each time inside another text)."""
    key = _same_words(text)
    mine = [turn.text for turn in history if turn.role == "elliot"][-lookback:]
    if not key:
        return False
    if any(_same_words(earlier) == key for earlier in mine):
        return True
    said = {sentence for earlier in mine for sentence in sentences(earlier)}
    new = sentences(text)
    return bool(new) and all(sentence in said for sentence in new)


def promises_front_desk(text: str) -> bool:
    return bool(_FRONT_DESK_PROMISE.search(_flat(text)))


def front_desk_has_it(lead: LeadView, history: list[Turn], actions: list[AgentAction]) -> bool:
    """The front desk really was brought in: in this run (a hand-off, a report, a failed booking's
    card, ...), by an open review or a callback on the lead, or by the front desk / English-only
    text earlier in the chat (a test chat from an older tab has no side panel codes)."""
    for action in actions:
        code = action.result_code or ""
        if (action.kind in ("hand_off", "emergency") or code.startswith(_FRONT_DESK_CODE_PREFIXES)
                or code in _FRONT_DESK_RESULT_CODES):
            return True
    if lead.needs_review or lead.status == "callback_scheduled":
        return True
    return any(turn.role == "elliot" and any(
        filled_from(turn.text, texts.DRAFTS[key]) for key in ("front_desk", "english_only"))
        for turn in history)


# Full dates with a year, as people write them: 8/23/1985, 08-23-85, 1985-08-23, Aug 23, 1985,
# August 23rd 1985, 23 August 1985 (book-11, round 2: "Got it, Aug 23, 1985.").
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_NUMERIC_DATE = re.compile(r"(?<![\d/.-])(\d{1,4})[/.-](\d{1,2})[/.-](\d{2,4})(?![\d/-])")
_MONTH_FIRST = re.compile(rf"\b{_MONTH}\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+"
                          r"(?P<year>\d{4}|'\d{2})\b")
_DAY_FIRST = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH},?\s+"
                        r"(?P<year>\d{4})\b")


def _years(value: str) -> list[int]:
    digits = value.lstrip("'")
    if len(digits) == 4:
        return [int(digits)]
    return [1900 + int(digits), 2000 + int(digits)] if len(digits) == 2 else []


def _day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def full_dates(text: str | None) -> set[date]:
    """Every date with a year in the text (both readings of 3/4/85; both centuries of '85)."""
    flat = _flat(text)
    found: set[date | None] = set()
    for first, second, third in _NUMERIC_DATE.findall(flat):
        if len(first) == 4:
            found.add(_day(int(first), int(second), int(third)))
            continue
        if len(first) > 2:
            continue
        for year in _years(third):
            found.add(_day(year, int(first), int(second)))
            found.add(_day(year, int(second), int(first)))
    for pattern in (_MONTH_FIRST, _DAY_FIRST):
        for match in pattern.finditer(flat):
            for year in _years(match["year"]):
                found.add(_day(year, _MONTHS[match["mon"]], int(match["day"])))
    return {value for value in found if value is not None}


def birth_dates(lead: LeadView, history: list[Turn], today: date) -> set[date]:
    """The birthday on file and any full date at least a year back in the patient's own texts (a
    corrected birthday: "no, it's August 23 1985")."""
    dates = {lead.date_of_birth} if lead.date_of_birth else set()
    oldest = today - timedelta(days=365)
    for turn in history:
        if turn.role == "patient":
            dates |= {value for value in full_dates(turn.text) if value <= oldest}
    return dates


def writes_birth_date(text: str, lead: LeadView, history: list[Turn], today: date) -> bool:
    """The model's text holds the patient's full date of birth, year included."""
    written = full_dates(text)
    return bool(written and written & birth_dates(lead, history, today))


_ASKS_BIRTH = re.compile(r"\b(?:date of birth|birth ?date|birthday|b-?day|dob|d\.o\.b|born)\b")
_ASKS = re.compile(r"\?|\b(?:send|type|text|share|tell|confirm|what'?s|what is|give)\b")


def asks_birth_date(text: str) -> bool:
    """The text asks for a date of birth ("To confirm it's you, what's your date of birth?", "And the
    year you were born?", "send your DOB again")."""
    flat = text.lower().replace("’", "'")
    # In one sentence: "Your date of birth is confirmed. Which time works?" asks nothing about it.
    return any(_ASKS_BIRTH.search(part) and _ASKS.search(part)
               for part in re.findall(r"[^.!?\n]+[.!?]*", flat))


_ASKS_LAST_NAME = re.compile(r"\b(?:last\s+name|surname|family\s+name)\b")


def asks_last_name(text: str) -> bool:
    """The text asks for a last name ("And what's your last name?", "send your first and last
    name"), in one sentence with a question or a request."""
    flat = text.lower().replace("’", "'")
    return any(_ASKS_LAST_NAME.search(part) and _ASKS.search(part)
               for part in re.findall(r"[^.!?\n]+[.!?]*", flat))


def last_name_on_file(lead: LeadView) -> bool:
    return not lead.is_new_person and bool((lead.last_name or "").strip())


def claims_booking(text: str, *, other_outcome: bool = False) -> bool:
    flat = _flat(text)
    if _BOOKED_CLAIM.search(flat):
        return True
    return bool(not other_outcome and _SET_CLAIM.search(flat) and not re.search(r"\bcall", flat))


def looks_like_card(text: str, *, has_visit: bool = False) -> bool:
    """`has_visit`: the lead has a visit on the books, so reading it back is an answer."""
    flat = _flat(text)
    return bool(_CARD_ASK.search(flat) or (not has_visit and _CARD_READBACK.search(flat)))


def has_booked_visit(lead: LeadView) -> bool:
    """A visit on the books (status Booked, or Context's Booked visit line): restating it is true."""
    return lead.status == "booked" or bool((lead.booked_visit or "").strip())


def pure_acknowledgement(text: str | None) -> bool:
    """"ok", "thanks!", "Got it, thank you", "thanks, I'll wait", an emoji alone."""
    if text is None or text.strip() == MEDIA_ONLY:
        return False
    words = _words(text)
    return not words or bool(_ACK.match(words))


def _latest_patient(history: list[Turn]) -> int | None:
    return max((i for i, turn in enumerate(history) if turn.role == "patient"), default=None)


def said_yes_to_card(history: list[Turn]) -> bool:
    """Elliot's last text is the read-back card and the patient's latest text is a plain yes."""
    last = max((i for i, turn in enumerate(history) if turn.role == "elliot"), default=None)
    if last is None or texts.parse_card(history[last].text) is None:
        return False
    replies = [turn.text for turn in history[last + 1:]
               if turn.role == "patient" and turn.text.strip() and turn.text.strip() != MEDIA_ONLY]
    if not replies or clear_no(replies[-1]) or _YES_BUT.search(_flat(replies[-1])):
        return False
    return bool(_YES.match(_words(replies[-1])))


# An iPhone / Android reaction sent as a text: 'Liked "Great choice! ..."', 'Reacted 👍 to "..."'.
_REACTION = re.compile(r"^(?:liked|loved|laughed at|emphasi[sz]ed|disliked|questioned|"
                       r"reacted \S{1,8} to)\s*[\"“']", re.IGNORECASE)


def reacted_to_card(history: list[Turn]) -> bool:
    """Elliot's last text is the read-back card and the patient's latest text is a reaction to it."""
    last = max((i for i, turn in enumerate(history) if turn.role == "elliot"), default=None)
    latest = _latest_patient(history)
    if last is None or latest is None or latest < last:
        return False
    return (texts.parse_card(history[last].text) is not None
            and bool(_REACTION.match(history[latest].text.strip())))


def finished_before(history: list[Turn], lead: LeadView) -> bool:
    """Elliot's texts just before the patient's latest one closed something (booked, hand-off,
    opt-out, emergency, callback), and did not end on a question."""
    latest = _latest_patient(history)
    if latest is None:
        return False
    before = [i for i, turn in enumerate(history[:latest]) if turn.role == "patient"]
    start = (before[-1] + 1) if before else 0
    block = [turn.text.strip() for turn in history[start:latest] if turn.role == "elliot"]
    if not block or block[-1].endswith("?"):
        return False
    for text in block:
        if text == texts.EMERGENCY_TEXT or _OPT_OUT_LINE.search(_flat(text)):
            return True
        if any(filled_from(text, texts.DRAFTS[key])
               for key in ("booked", "front_desk", "english_only")):
            return True
    # A Studio-edited fixed text is not recognised above: the lead's state says it.
    return lead.status in _DONE_STATUSES or lead.booking_paused


def _check_answer(text: str, *, lead: LeadView, history: list[Turn], actions: list[AgentAction],
                  fixed: bool, today: date | None = None,
                  after_repeat: bool = False,
                  birthday_done: bool = False) -> tuple[str, str | None, str] | None:
    """None when the model's answer may go out; else (guard, tool to force or None, note).

    `after_repeat`: the model was told it repeated itself and may now answer NO_REPLY.
    `birthday_done`: the chat already confirmed the date of birth (sms_agent_birthday)."""
    codes = {action.result_code for action in actions}
    kinds = {action.kind for action in actions}
    if text and not _is_empty_answer(text):
        if writes_birth_date(text, lead, history, today or datetime.now(UTC).date()):
            return "birth_date", None, BIRTH_DATE_NOTE
        if birthday_done and asks_birth_date(text):
            # Owner, 2026-11-03: once given in the chat, the date of birth is never asked again.
            return "asked_birth_date", None, ASKED_BIRTH_DATE_NOTE
        if last_name_on_file(lead) and asks_last_name(text):
            # x-04 (2026-11-03): "And what's your last name?" when it is on file.
            return "asked_last_name", None, ASKED_LAST_NAME_NOTE
        booked_here = bool(codes & {"BOOKED", "ALREADY_BOOKED"})
        other_outcome = any(code.startswith(("REPORTED", "HANDED_OFF", "ALREADY_", "EMERGENCY"))
                            for code in codes)
        # A booked lead's visit is on the books: "you're booked for Tue at 9" is then true.
        has_visit = has_booked_visit(lead)
        if (not booked_here and not has_visit
                and claims_booking(text, other_outcome=other_outcome)):
            if said_yes_to_card(history) and "book" not in kinds:
                return "booking_claim", "book_visit", BOOK_NOTE
            return "booking_claim", None, CLAIM_NOTE
        if "CARD_SENT" not in codes and looks_like_card(text, has_visit=has_visit):
            if _CLOCK_TIME.search(text) and "confirm_visit" not in kinds:
                return "card_text", "confirm_visit", CARD_FORCED_NOTE
            return "card_text", None, CARD_NOTE
        if promises_front_desk(text) and not front_desk_has_it(lead, history, actions):
            # x-04 (round 3): "our front desk will finish setting up your visit" with no hand-off:
            # nobody would have called the patient.
            return "front_desk_promise", None, FRONT_DESK_NOTE
        if repeats_recent(text, history):
            return "repeat", None, REPEAT_NOTE
        return None
    if fixed or after_repeat or "CARD_ALREADY_SENT" in codes or reacted_to_card(history):
        # The card is still their latest text from Elliot and asks for a YES: a reaction to it
        # needs no answer (2026-11-03: the same card is never sent twice in a row).
        return None
    latest = _latest_patient(history)
    if latest is not None and pure_acknowledgement(history[latest].text) and finished_before(
            history, lead):
        return None
    return "empty", None, EMPTY_NOTE


def run_agent(*, lead: LeadView, history: list[Turn], tools: AgentTools, now: datetime,
              front_desk_open: bool, settings: Settings, client=None, model: str | None = None,
              booking_link: str | None = None, timezone: str | None = None,
              services: str = "", service_aliases: str = "",
              trace: WorkflowTrace | None = None) -> AgentReply:
    """One reply to the patient's latest texts. Never raises for a model problem: `error` is set.

    A fixed text from a tool (card, booked, front desk, 911, intro) is sent word for word; the model
    then gets one more round to add a short text of its own, or nothing. Once a tool has acted (a
    booking made, a card opened), its fixed text is returned even if a later model round fails, so
    the patient always hears about what was done.
    """
    deadline = time.monotonic() + settings.sms_agent_timeout_seconds
    actions: list[AgentAction] = []
    sent_first: list[str] = []
    sent_last: list[str] = []
    opted_out = False
    try:
        client = client or default_client(settings)
        tool_specs = load_tools()
        # "Date of birth confirmed in this chat" (owner, 2026-11-03: never asked again once given);
        # the tools' own reading, so the Context and the booking agree.
        state = getattr(tools, "birthday_state", None)
        settled = (state() if callable(state)
                   else sms_agent_birthday.birthday_state(lead, history))
        birthday = sms_agent_birthday.context_line(settled)
        items: list[dict[str, Any]] = [
            {"role": "developer", "content": load_prompt()},
            {"role": "developer", "content": context_block(
                lead, now=now, front_desk_open=front_desk_open, booking_link=booking_link,
                front_desk_phone=settings.clinic_front_desk_phone, timezone=timezone,
                services=services, service_aliases=service_aliases,
                birthday_confirmed=birthday)},
            *history_items(history, timezone),
        ]
    except Exception as exc:  # noqa: BLE001 - a missing prompt file must not crash the worker
        return AgentReply([], actions, error=f"setup_failed:{type(exc).__name__}")

    def failed(error: str) -> AgentReply:
        if trace:
            trace.log("sms_agent_failed", error=error, actions=len(actions))
        if sent_first or sent_last:
            return _finish(sent_first, "", sent_last, actions, opted_out)
        return AgentReply([], actions, error=error, opted_out=opted_out)

    model = model or settings.sms_agent_model
    max_rounds = settings.sms_agent_max_rounds
    retried: set[str] = set()       # guards already re-run once in this reply
    forced: str | None = None       # tool the next round must call (a guard's re-run)
    for round_number in range(max_rounds + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return failed("timeout")
        last = round_number == max_rounds
        request: dict[str, Any] = {
            "model": model, "input": items, "tools": tool_specs, "store": False,
            "timeout": min(remaining, CALL_TIMEOUT_SECONDS),
        }
        if is_reasoning_model(model):
            request["include"] = ["reasoning.encrypted_content"]
        if last:
            # Tool rounds used up: the model must answer now, in words.
            request["tool_choice"] = "none"
        elif forced:
            request["tool_choice"] = {"type": "function", "name": forced}
        forced = None
        try:
            response = client.responses.create(**request)
        except Exception as exc:  # noqa: BLE001 - timeout, rate limit, network: no reply, staff flagged
            name = type(exc).__name__
            if "Timeout" not in name:
                return failed(f"model_error:{name}")
            # One call hung (fact-09): try it once more if the overall time allows.
            left = deadline - time.monotonic()
            if left < RETRY_MIN_SECONDS:
                return failed("timeout")
            if trace:
                trace.log("sms_agent_model_retry", reason="timeout", round=round_number + 1)
            request["timeout"] = min(left, CALL_TIMEOUT_SECONDS)
            try:
                response = client.responses.create(**request)
            except Exception as again:  # noqa: BLE001 - as above
                name = type(again).__name__
                return failed("timeout" if "Timeout" in name else f"model_error:{name}")
        if time.monotonic() > deadline:
            return failed("timeout")
        output = list(_get(response, "output", []) or [])
        calls = [item for item in output if _get(item, "type") == "function_call"]
        if not calls:
            answer = _clean(_output_text(response))
            verdict = _check_answer(answer, lead=lead, history=history, actions=actions,
                                    fixed=bool(sent_first or sent_last),
                                    today=now.astimezone(CLINIC_TZ).date(),
                                    after_repeat="repeat" in retried,
                                    birthday_done=settled.confirmed is not None)
            if verdict is not None:
                guard, tool, note = verdict
                if guard in retried or last:
                    # Never the false claim or an invented card; nothing invented in its place.
                    if trace:
                        trace.log("sms_agent_guard", guard=guard, step="dropped")
                    if guard in ("empty", "repeat"):
                        # Silence (or only this run's fixed texts) rather than the same text again.
                        return _finish(sent_first, "", sent_last, actions, opted_out)
                    return failed(f"guard:{guard}")
                retried.add(guard)
                # The re-run counts toward the rounds; a forced tool needs a tool round left.
                forced = tool if round_number + 1 < max_rounds else None
                if trace:
                    trace.log("sms_agent_guard", guard=guard,
                              step=f"forced_{forced}" if forced else "note")
                items.append({"role": "developer", "content": note})
                continue
            if trace:
                trace.log("sms_agent_replied", rounds=round_number + 1, actions=len(actions))
            return _finish(sent_first, answer, sent_last, actions, opted_out)
        if last:
            return failed("round_cap")
        items.extend(_as_input(item) for item in output)
        for call in calls:
            name = _get(call, "name", "")
            try:
                arguments = json.loads(_get(call, "arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise TypeError("arguments must be an object")
            except (ValueError, TypeError):
                result = ToolResult("BAD_ARGUMENTS: send the tool's arguments as a JSON object.",
                                    "unknown", f"{name}: unreadable arguments")
            else:
                result = call_tool(tools, name, arguments, trace)
            actions.append(AgentAction(kind=result.kind, summary=result.summary,
                                       result_code=result.action_code or result.code,
                                       reason=result.reason))
            opted_out = opted_out or result.opted_out
            bucket = sent_last if result.send_last else sent_first
            bucket.extend(text for text in result.fixed_texts if text not in bucket)
            items.append({"type": "function_call_output", "call_id": _get(call, "call_id"),
                          "output": result.output})
    return failed("round_cap")  # not reached: the last round offers no tools


# --- live and sandbox --------------------------------------------------------------------------------

def reply_live(trace: WorkflowTrace, *, lead_id: str, job_id: int, settings: Settings | None = None,
               client=None, providers=None, now: datetime | None = None) -> AgentReply:
    """Reply for a real lead (sms-agent-worker). Reads the lead and texts, runs the agent with
    LiveTools. The caller sends the texts and must not hold a lock on the lead row meanwhile: a
    booking or report here locks it in its own transaction."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    with transaction() as conn:
        # Only the texts this job answers: the worker moved its last_inbound_id to the newest text
        # under the lead lock; a later one belongs to the next job.
        job = conn.execute("select last_inbound_id from sms_agent_jobs where id=%s",
                           (job_id,)).fetchone()
        live = load_live(conn, lead_id, now=now,
                         up_to_inbound_id=job["last_inbound_id"] if job else None)
    desk_open = clinic_front_desk_open(live.lead.clinic, now, live.holidays)
    tools = LiveTools(trace, lead_id=lead_id, job_id=job_id, lead=live.lead, history=live.history,
                      settings=settings, practice_id=live.practice_id, providers=providers,
                      birthday_history=live.birthday_history)
    trace.log("sms_agent_started", history=len(live.history), front_desk_open=desk_open)
    return run_agent(lead=live.lead, history=live.history, tools=tools, now=now,
                     front_desk_open=desk_open, settings=settings, client=client,
                     booking_link=live.booking_link, timezone=str(CLINIC_TZ),
                     services=live.services, service_aliases=live.service_aliases, trace=trace)


# Test chat statuses -> the cadence state a real lead with that status would have.
_SANDBOX_CADENCE = {
    "new": "pending", "booked": "completed", "declined": "terminated",
    "closed_no_response": "terminated", "do_not_contact": "terminated",
    "needs_attention": "paused", "invalid_phone": "paused",
}


def sandbox_lead(setup: SandboxSetup) -> LeadView:
    new = setup.who == "new"
    status = "new" if new else (setup.status or "in_progress")
    return sms_agent_sandbox_setup.apply_setup_extras(LeadView(
        is_new_person=new,
        first_name=setup.first_name.strip(),
        last_name=setup.last_name.strip(),
        date_of_birth=setup.date_of_birth,
        clinic=setup.clinic.strip(),
        service=setup.service.strip(),
        referral_word="" if new else setup.referral_word.strip(),
        status=status,
        cadence_state=_SANDBOX_CADENCE.get(status, "active"),
        needs_review=status == "needs_attention",
        review_reason="made-up review (test chat)" if status == "needs_attention" else "",
        booked_visit="booked (test chat: no visit details set)" if status == "booked" else "",
        recent_calls=[],
    ), setup)


# --- what Elliot did earlier in a test chat ----------------------------------------------------------
#
# The sandbox writes nothing, so each turn started from the setup alone: a case change reported on
# turn 1 and "book me" on turn 2 booked in the test chat where live refuses (the case change card
# stops booking). The state is rebuilt from the route's `prior_actions` (the side panel codes of
# the earlier replies, oldest first: ToolResult.action_code) and from Elliot's own fixed texts in
# the history (the emergency text, the booked text), so a chat without prior_actions still keeps
# the plain cases.

# Live, these open a card that stops booking by text (or the booking engine's own card).
_PAUSING_CODES = {"REPORTED_CASE_CHANGE", "REPORTED_WRONG_PERSON", "HANDED_OFF_PAUSED",
                  "EMERGENCY_SENT", *FRONT_DESK_CODES}
# The lead status a live report leaves (lead_status.report_lead_status).
_STATUS_AFTER = {"REPORTED_DECLINED": "declined", "REPORTED_CALLBACK_SCHEDULED": "callback_scheduled",
                 "REPORTED_BOOKING_LINK": "booking_link_sent",
                 "REPORTED_WRONG_PERSON": "invalid_phone"}
_CADENCE_AFTER = {"declined": "terminated", "invalid_phone": "paused",
                  "callback_scheduled": "active", "booking_link_sent": "active"}
# Template words this long or longer must all be in a text for it to count as the booked text (the
# shorter ones, like "You're all set,", change with an empty first name).
_TEMPLATE_PART_CHARS = 20


@dataclass
class EarlierInChat:
    booked: bool = False
    paused: bool = False         # a card that stops booking (case change, emergency, ...)
    front_desk: bool = False     # Elliot's front-desk card that lets booking go on
    opted_out: bool = False      # do_not_contact by text: the real system stops replying
    status: str | None = None    # the status a report left
    # For the repeat checks (SandboxTools.earlier): "case_change", "emergency" (within the hour),
    # "hand_off:front_desk" (booking goes on) / "hand_off:paused".
    done: set[str] = field(default_factory=set)
    # Failed bookings by code (sms_agent_tools.REPEATED_FAILURES): the second one for the same
    # reason hands off (SandboxTools.earlier_failures).
    failures: dict[str, int] = field(default_factory=dict)


def filled_from(text: str, body: str) -> bool:
    """`text` is `body` with its placeholders filled in (the long fixed parts, in order)."""
    parts = [" ".join(part.split()) for part in re.split(r"\{[a-z_]+\}", body or "")]
    parts = [part for part in parts if len(part) >= _TEMPLATE_PART_CHARS]
    flat = " ".join(str(text or "").split())
    position = 0
    for part in parts:
        position = flat.find(part, position)
        if position < 0:
            return False
        position += len(part)
    return bool(parts)


# Side panel codes -> what the repeat checks count as done (SandboxTools.earlier).
_DONE_AFTER = {"HANDED_OFF": "hand_off:front_desk", "HANDED_OFF_PAUSED": "hand_off:paused",
               "REPORTED_CASE_CHANGE": "case_change"}
_HAND_OFF_CODES = {"HANDED_OFF", "HANDED_OFF_PAUSED", "ALREADY_HANDED_OFF"}


def earlier_in_chat(history: list[Turn], prior_actions: list[Any] | None,
                    booked_body: str | None = None, *, hand_off_bodies: tuple[str, ...] = (),
                    now: datetime | None = None) -> EarlierInChat:
    state = EarlierInChat()
    emergency_code = False
    for action in prior_actions or []:
        code = str(_get(action, "result_code", "") or "").strip().upper()
        if code == "BOOKED":
            state.booked = True
        if code in _PAUSING_CODES:
            state.paused = True
        if code == "HANDED_OFF":
            state.front_desk = True
        if code == "REPORTED_DO_NOT_CONTACT":
            state.opted_out = True
        if code in _STATUS_AFTER:
            state.status = _STATUS_AFTER[code]
        reason = str(_get(action, "reason", "") or "").strip().lower()
        if code in _HAND_OFF_CODES and reason in SMS_FRONT_DESK_REASONS:
            # The tab sent the reason back (x-02): only the same reason is a repeat, like live.
            state.done.add(f"hand_off:{reason}")
        elif code in _DONE_AFTER:
            state.done.add(_DONE_AFTER[code])
        emergency_code = emergency_code or code == "EMERGENCY_SENT"
        if str(_get(action, "kind", "") or "") == "book" and code in REPEATED_FAILURES:
            state.failures[code] = state.failures.get(code, 0) + 1
    emergency_times: list[datetime | None] = []
    for turn in history:
        if turn.role != "elliot":
            continue
        if turn.text.strip() == texts.EMERGENCY_TEXT:
            state.paused = True
            emergency_times.append(turn.at)
        elif booked_body and filled_from(turn.text, booked_body):
            state.booked = True
        elif any(filled_from(turn.text, body) for body in hand_off_bodies if body):
            state.done.add("hand_off:front_desk")
    # The emergency text counts as a repeat for an hour (live: the card's time); a chat that only
    # has the side panel's code has no time: it counts.
    if emergency_times:
        if any(at is None or now is None
               or now - (at if at.tzinfo else at.replace(tzinfo=CLINIC_TZ)) <= EMERGENCY_REPEAT_WINDOW
               for at in emergency_times):
            state.done.add("emergency")
    elif emergency_code:
        state.done.add("emergency")
    return state


def apply_earlier(lead: LeadView, state: EarlierInChat) -> LeadView:
    """The test chat's lead as the live LeadView would read after those earlier actions."""
    status, cadence = lead.status, lead.cadence_state
    if state.status and status not in {"booked", "do_not_contact"}:
        status, cadence = state.status, _CADENCE_AFTER.get(state.status, cadence)
    updates: dict[str, Any] = {}
    if state.booked:
        # A booking settles nothing else here: a case change card stays, like mark_booked.
        status, cadence = "booked", "completed"
        updates["booked_visit"] = lead.booked_visit or "booked earlier in this chat (test chat)"
    if state.paused or state.front_desk:
        updates.update(
            needs_review=True,
            review_reason=lead.review_reason or "Elliot opened a card earlier in this chat (test chat)",
            # Only Elliot's bookable front-desk card(s) open: booking by text goes on.
            front_desk_only=not state.paused and (not lead.needs_review or lead.front_desk_only))
        # open_front_desk_card: a lead still in the cadence is paused; a final one keeps its status.
        if status not in FINAL_STATUSES and status != "invalid_phone" and cadence not in {
                "completed", "terminated"}:
            status, cadence = "needs_attention", "paused"
    return replace(lead, status=status, cadence_state=cadence, **updates)


def reply_sandbox(*, setup: SandboxSetup, history: list[Turn], settings: Settings | None = None,
                  client=None, trace: WorkflowTrace | None = None,
                  prior_actions: list[Any] | None = None) -> AgentReply:
    """Test chat reply: a made-up lead, SandboxTools (reads the real slot cache, writes nothing,
    never calls Stride or Twilio). `prior_actions`: the earlier replies' actions ({kind,
    result_code, reason?}, oldest first), so this turn knows what Elliot already did."""
    settings = settings or get_settings()
    trace = trace or WorkflowTrace("sms_agent_sandbox", "api")
    lead = sandbox_lead(setup)
    if lead.status == "do_not_contact":
        # The real system never answers a Do Not Contact lead (services/sms_inbound.should_reply).
        return AgentReply([], [AgentAction("fixed_text", "Would not reply: Do Not Contact",
                                           "NO_REPLY")], no_reply=True)
    if earlier_in_chat([], prior_actions).opted_out:
        # Elliot opted the patient out earlier in this chat: live, no reply ever again.
        return AgentReply([], [AgentAction("fixed_text", "Would not reply: opted out earlier",
                                           "NO_REPLY")], no_reply=True)
    with transaction() as conn:
        conn.execute("set transaction read only")
        row = conn.execute(
            "select p.id,ps.holidays,ps.booking_link_url,"
            "(select string_agg(ct.name, ', ' order by ct.name) from case_types ct "
            " where ct.practice_id=p.id and ct.is_active) as services,"
            "(select string_agg(ct.name||' = '||array_to_string(ct.aliases, ', '), '; ' "
            " order by ct.name) from case_types ct where ct.practice_id=p.id and ct.is_active "
            " and cardinality(ct.aliases)>0) as service_aliases "
            "from practices p left join practice_settings ps on ps.practice_id=p.id "
            "where p.slug=%s",
            (setup.practice_slug,),
        ).fetchone()
        booked_body = texts.template_body(conn, "booked", row["id"]) if row else None
        hand_off_bodies = tuple(texts.template_body(conn, key, row["id"])
                                for key in ("front_desk", "english_only")) if row else ()
    if not row:
        return AgentReply([], [], error="practice_not_found")
    clock = setup.clock
    if clock is not None and clock.tzinfo is None:
        clock = clock.replace(tzinfo=CLINIC_TZ)
    now = clock or datetime.now(UTC)
    earlier = earlier_in_chat(history, prior_actions, booked_body,
                              hand_off_bodies=hand_off_bodies, now=now)
    lead = apply_earlier(lead, earlier)
    if setup.front_desk == "open":
        desk_open = True
    elif setup.front_desk == "closed":
        desk_open = False
    else:
        desk_open = clinic_front_desk_open(lead.clinic, now, row["holidays"] or [])
    tools = SandboxTools(trace, lead=lead, history=history, practice_id=row["id"],
                         now_local=clock.astimezone(ZoneInfo(str(CLINIC_TZ))) if clock else None,
                         settings=settings, earlier=earlier.done,
                         earlier_failures=earlier.failures)
    return run_agent(lead=lead, history=history, tools=tools, now=now, front_desk_open=desk_open,
                     settings=settings, client=client, model=setup.model,
                     booking_link=row["booking_link_url"], timezone=str(CLINIC_TZ),
                     services=row["services"] or "", service_aliases=row["service_aliases"] or "",
                     trace=trace)
