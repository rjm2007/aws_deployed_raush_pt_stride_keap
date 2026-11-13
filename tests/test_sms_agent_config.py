"""The real SMS agent prompt and tool schemas (config/sms_agent_prompt.md, config/sms_agent_tools.json).

The other SMS agent tests use small fixtures in their place; these check the shipped files: strict
schemas, only real tools and Context labels named, and the 2026-11-02 eval fixes (gpt-5.5: false
"booked" texts, silence after the birthday yes, repeated hand-offs) not taught back by an example.
"""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.services import sms_agent
from rpt_agent.services import sms_agent_texts as texts
from rpt_agent.services.sms_agent_context import LeadView, context_block
from rpt_agent.services.sms_agent_tools import KINDS, AgentTools

PROMPT = sms_agent.PROMPT_FILE.read_text(encoding="utf-8")
TOOLS = sms_agent.load_tools(str(sms_agent.TOOLS_FILE))
BY_NAME = {tool["name"]: tool for tool in TOOLS}
SERVICES = ["Physical Therapy", "Physical Therapy - Pelvic", None]
EXAMPLES = re.findall(r"<example>\n(.*?)\n</example>", PROMPT, re.DOTALL)


def _objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


def test_tools_are_the_nine_tools_the_code_dispatches():
    assert set(BY_NAME) == set(KINDS)
    assert len(TOOLS) == len(BY_NAME) == 9


@pytest.mark.parametrize("name", sorted(KINDS))
def test_tool_schema_is_strict(name):
    # OpenAI strict mode: every object closed, every property required, optional = nullable.
    tool = BY_NAME[name]
    assert tool["type"] == "function" and tool["strict"] is True
    for obj in _objects(tool["parameters"]):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    for prop in tool["parameters"]["properties"].values():
        assert prop.get("description")
        if "enum" in prop and isinstance(prop["type"], list):
            assert (None in prop["enum"]) == ("null" in prop["type"])


@pytest.mark.parametrize("name", sorted(KINDS))
def test_tool_parameters_reach_the_code_or_are_dropped_safely(name):
    # call_tool drops unknown arguments, so a parameter the code does not take yet is harmless; a
    # required argument of the code must be in the schema.
    method = getattr(AgentTools, name)
    params = inspect.signature(method).parameters
    needed = {key for key, p in params.items() if key != "self" and p.default is inspect.Parameter.empty}
    assert needed <= set(BY_NAME[name]["parameters"]["properties"])


@pytest.mark.parametrize("name", ["find_times", "confirm_visit", "book_visit"])
def test_service_parameter_is_nullable_and_only_for_someone_new(name):
    # np-03 (2026-11-02): a new person asked for pelvic and was booked for Physical Therapy.
    prop = BY_NAME[name]["parameters"]["properties"]["service"]
    assert prop["type"] == ["string", "null"]
    assert prop["enum"] == SERVICES
    assert "someone new" in prop["description"]
    assert "null for a patient with a referral" in prop["description"]


def test_list_therapists_has_no_service_parameter():
    assert "service" not in BY_NAME["list_therapists"]["parameters"]["properties"]


def test_prompt_names_only_real_tools_and_values():
    # Every snake_case word in the prompt is a tool, a parameter or an enum value of one (or the
    # voice tool name tool replies still use, which the prompt translates).
    known = set(BY_NAME) | {"book_appointment"}
    for tool in TOOLS:
        for key, prop in tool["parameters"]["properties"].items():
            known.add(key)
            known.update(value for value in prop.get("enum", []) if value)
    words = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", PROMPT))
    assert words - known == set()


def _context_labels(new: bool) -> set[str]:
    lead = LeadView(
        is_new_person=new, first_name="Sam", last_name="Lee", date_of_birth=date(1990, 3, 14),
        clinic="Laguna Niguel", service="Physical Therapy", referral_word="knee", status="booked",
        cadence_state="completed", needs_review=True, review_reason="x",
        booked_visit="Tue Nov 3 at 9:00 AM with Alan at Laguna Niguel", recent_calls=["a call"])
    block = context_block(
        lead, now=datetime(2026, 11, 2, 15, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
        front_desk_open=True, booking_link="https://book.example", front_desk_phone="(949) 555-0100",
        services="Physical Therapy", service_aliases="Physical Therapy = PT")
    return {line[2:].split(":")[0].split(" (")[0] for line in block.splitlines()
            if line.startswith("- ")}


def test_prompt_context_labels_are_written_by_the_code():
    section = PROMPT.split("# Context you get", 1)[1].split('"unknown" means', 1)[0]
    listed = section.split("holds:", 1)[1]
    listed = re.sub(r"\([^)]*\)", "", listed).replace("\n", " ")
    labels = {part.strip(" .") for part in listed.split(",") if part.strip(" .")}
    written = _context_labels(new=False) | _context_labels(new=True)
    written = {label.replace("Recent calls from Elliot", "Recent calls") for label in written}
    assert labels - written == set()


def test_prompt_card_lines_match_the_real_card():
    # The model recognises its card in the history by these lines (book-02, book-06: a yes to the
    # card got another card instead of book_visit).
    assert texts.CARD_FIRST_LINE in PROMPT
    assert texts.CARD_LAST_LINE in PROMPT


def test_examples_never_write_a_card_a_hold_or_a_booking():
    # 2026-11-02 eval: Elliot texted its own card, "I'm holding", and "You're booked" with no
    # book_visit; one example used a made-up therapist surname that showed up in real replies.
    assert EXAMPLES
    for example in EXAMPLES:
        sent = "\n".join(line for line in example.splitlines() if not line.startswith("Tool:"))
        for phrase in ("Reply YES", "holding", "You're all set", "You're booked", "All set", "Rome"):
            assert phrase not in sent, (phrase, example)
        # Tool calls are their own lines, never inside a text (book-12 sent "[Visit card sent ...]").
        assert "[" not in sent.split("Patient:", 1)[-1].replace("[Front desk phone]", "")


def test_examples_never_read_a_birthday_to_the_patient_and_check_it_in_code():
    # Owner, 2026-11-03 (test chat looped on "is your birthday November 1?"): the patient types
    # their date of birth; check_birthday compares; confirm_visit follows in the same turn.
    assert "is your birthday" not in PROMPT
    # 2026-11-04: the question is ours (confirm_visit), naming the time; a valid answer to it gets
    # the card from code.
    assert texts.BIRTHDAY_QUESTION_END in PROMPT
    assert "(Code step: confirm_visit" in PROMPT and "(Code step: book_visit" in PROMPT
    checked = 0
    for example in EXAMPLES:
        lines = example.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("Tool: check_birthday") and line.endswith(
                    ("-> MATCH", "-> NEW", "-> CONFIRMED_DIFFERENT")):
                checked += 1
                assert lines[i + 1].startswith("Tool: confirm_visit"), example
    assert checked >= 1


def test_prompt_never_asks_for_the_date_of_birth_again_once_confirmed():
    flat = " ".join(PROMPT.split())
    assert "Date of birth confirmed in this chat: yes = the identity step is done" in flat
    assert "Never ask for the date of birth again" in flat
    assert "Never send the same card twice in a row" in flat
    assert "HANDED_OFF from book_visit = it failed twice for the same reason" in flat


def test_every_example_tool_line_names_a_real_tool():
    for example in EXAMPLES:
        for line in example.splitlines():
            if line.startswith("Tool:"):
                assert line.split()[1] in BY_NAME, line


def test_prompt_keeps_the_eval_fixes():
    # One line per finding, so a later edit cannot drop a rule silently.
    for rule in (
        "confirm_visit in this same turn",            # birthday yes -> card at once
        "ALREADY_HANDED_OFF",                         # repeated hand-off
        "when=earliest, no question",                 # "any is fine"
        "when=tomorrow, the day in\n     Tomorrow in Context",  # "tmrw"
        "Our calls end at 7:00 PM.",                  # after-hours callback
        "We don't make calls on Sundays.",            # Sunday callback
        "Never apologize for a delay",
        "closed Saturday and Sunday",                 # Mission Viejo hours
        "this is Elliot from Rausch Physical Therapy. Could you ask",  # family member
        "Want me to find you a time?",                # new person with a question
        "write nothing about the booking yourself (NO_REPLY",            # failed booking (Emma 11-11)
    ):
        assert rule in PROMPT, rule


@pytest.mark.parametrize("name,codes", [
    # Review, 2026-11-02: each code the tools reply with is explained in that tool's description
    # (flag_emergency said ALREADY_HANDED_OFF where the code replies ALREADY_SENT).
    ("flag_emergency", ["EMERGENCY_SENT", "ALREADY_SENT"]),
    ("hand_off", ["HANDED_OFF", "ALREADY_HANDED_OFF"]),
    ("report_outcome", ["REPORTED", "ALREADY_BOOKED", "ALREADY_REPORTED"]),
    ("find_times", ["OPENINGS", "NO_SUCH_THERAPIST", "CASE_CHANGE"]),
    ("confirm_visit", ["CARD_SENT", "NOT_AVAILABLE", "ALREADY_BOOKED", "WHICH_THERAPIST",
                       "NO_SUCH_THERAPIST", "CASE_CHANGE"]),
    ("book_visit", ["BOOKED", "NOT_CONFIRMED", "CASE_CHANGE"]),
])
def test_tool_descriptions_explain_the_codes_the_code_replies(name, codes):
    for code in codes:
        assert re.search(rf"\b{code}\b", BY_NAME[name]["description"]), (name, code)
    if name == "flag_emergency":
        assert "ALREADY_HANDED_OFF" not in BY_NAME[name]["description"]
