"""Checks for Elliot's human-sounding style (tested on a copy, moved to the main agent 2026-10-27).

The 3f2 confirmation must stay word for word because the hang-up safety net
(services/hang_up_booking.py) parses it.
"""
import json
from pathlib import Path

from rpt_agent.services.hang_up_booking import find_confirmation

CONFIG = Path(__file__).parents[1] / "config"
PROMPT = (CONFIG / "vapi_booking_assistant_prompt.md").read_text(encoding="utf-8")
FLAT = " ".join(PROMPT.split())
RAW_TOOLS = (CONFIG / "vapi_booking_tools.json").read_text(encoding="utf-8")
TOOLS = json.loads(RAW_TOOLS)
CONFIRMATION = ("\"So that's [day] the [date] at [time] with [therapist first name] at our [clinic] "
                "office. Shall I book it?\"")


def _tool(name: str) -> dict:
    return next(t for t in TOOLS["tools"] if t["function"]["name"] == name)


def test_confirmation_is_word_for_word_and_still_parsed():
    assert CONFIRMATION in FLAT
    example = "So that's Thursday the eighteenth at eleven in the morning with Thao at our Laguna Niguel office. Shall I book it?"
    assert example in FLAT
    found = find_confirmation([("bot", example)])
    assert found and found[2] and "thursday the eighteenth" in found[1]


def test_rule_9_no_longer_quotes_the_old_waiting_line_but_still_forbids_it():
    assert "One moment, please, while I check our availability" not in PROMPT
    rule = FLAT[FLAT.index("9. **No made-up failures.**"):FLAT.index("# Style")]
    assert "Never write a waiting line" in rule
    assert "no tool reply after it, the tool was never called: call it now" in rule
    assert "never after an IN_PROGRESS reply" in rule


def test_tools_have_variants_and_no_old_patience_line():
    find = [m for m in _tool("find_slots")["messages"] if m["type"] == "request-start"]
    assert len(find) >= 3
    assert len([m for m in _tool("list_providers")["messages"]
                if m["type"] == "request-start"]) >= 2
    book = _tool("book_appointment")["messages"]
    assert len([m for m in book if m["type"] == "request-start"]) >= 3
    delayed = [m for m in book if m["type"] == "request-response-delayed"]
    assert [m["timingMilliseconds"] for m in delayed] == [12000]
    assert delayed[0]["content"] == "Thanks for waiting, nearly there."
    assert all("few seconds" in m["content"] for m in book if m["type"] == "request-start")
    for tool in TOOLS["tools"]:
        for m in tool["messages"]:
            if m["type"] == "request-start":
                assert m["blocking"] is False and m["content"].strip()
    assert "Thanks for your patience" not in RAW_TOOLS
    assert "One moment, please" not in RAW_TOOLS
    assert "Almost done" not in RAW_TOOLS and "Still with you" not in RAW_TOOLS
    assert "\"credentialId\"" not in RAW_TOOLS and "\"secret\"" not in RAW_TOOLS.lower()


def test_tool_ids_unchanged():
    assert {t["function"]["name"]: t["id"][:8] for t in TOOLS["tools"]} == {
        "find_slots": "f622d276", "book_appointment": "0c8f0b68", "list_providers": "bbd1aed1"}


def test_closing_is_short_and_already_booked_without_time_unchanged():
    step = FLAT[FLAT.index("3h. **"):FLAT.index("3i. **")]
    assert ("\"You're all set! You'll get a text with the details. Just bring a photo ID. "
            "See you [day], [first name]. Goodbye!\"") in step
    assert "ALREADY_BOOKED with a day and time" in step
    assert ("it must always say that they're booked, that a text with the details is coming, to "
            "bring a photo ID, and the day") in step
    assert "more than seven days away, say the weekday and the date" in step
    assert "\"You're already booked with us." in step
    assert "You're all set for [day]" not in FLAT
    assert "See you Thursday, Leah. Goodbye!" in FLAT


def test_shorter_opening_and_small_fixes():
    assert ("We have your referral. Got a couple minutes to set up your first visit?") in FLAT
    assert "We got your referral" not in FLAT
    assert "\"Either of those good?\"" in FLAT and "Any of those good?" not in FLAT
    assert "using the two times you offered (e.g. \"The five or the six?\")" in FLAT
    assert "after a booking close with \"See you [day]. Goodbye!\"" in FLAT
    assert "Do you have a moment?" not in FLAT
    assert "Tuesday, June" not in FLAT and "[day], [date]" not in FLAT
    assert "\"Saturday the twenty-fifth\", \"Monday the twenty-seventh\"" in FLAT
    assert "Saturday the twenty-fifth at ten in the morning with Thao, or Monday the twenty-seventh" in FLAT
    assert "no usable first name" in FLAT and "\"Hi, it's Elliot...\"" in FLAT
    assert "are not sentences for this rule" in FLAT
    # Kept: birthday check, last name first, insurance line.
    assert "your birthday's {{date_of_birth_spoken}}?" in FLAT
    assert "\"And what's your last name?\"" in FLAT
    assert "They can check your insurance and costs for you." in FLAT
