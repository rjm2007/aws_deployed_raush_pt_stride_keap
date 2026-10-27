"""The hang-up safety net skips every tool waiting line when it looks for Elliot's last line.

Why (2026-10-27): the human copy of Elliot plays "Perfect, booking that now." after the patient's
yes; taken as Elliot's last line, it hid the confirmation. The Docker image does not ship config/,
so hang_up_booking keeps the lines as a constant; this test keeps it in sync with both tool files.
"""
import json
from pathlib import Path

import pytest

from rpt_agent.services.hang_up_booking import (
    BOOKING_WAITING_LINES,
    TOOL_WAITING_LINES,
    _is_waiting_line,
    find_confirmation,
)

CONFIG = Path(__file__).parents[1] / "config"
CONFIRM = ("So that's Tuesday the sixteenth at nine in the morning with Thao at our Laguna Niguel "
           "office. Shall I book it?")


def _config_lines(tool: str | None = None) -> set[str]:
    lines = set()
    for name in ("vapi_booking_tools.json", "vapi_booking_tools_human.json"):
        tools = json.loads((CONFIG / name).read_text(encoding="utf-8"))["tools"]
        lines |= {m["content"] for t in tools for m in t.get("messages", [])
                  if m["type"] in ("request-start", "request-response-delayed")
                  and tool in (None, t["function"]["name"])}
    return lines


def test_constant_matches_both_tool_files():
    assert set(TOOL_WAITING_LINES) == _config_lines()
    assert set(BOOKING_WAITING_LINES) == _config_lines("book_appointment")


@pytest.mark.parametrize("line", sorted(_config_lines()) + [
    "One moment, please, while I check our available",  # cut short at the hang-up
    "Perfect, booking that",
    "Perfect, booking that now. It'll just",
    "Sorry, a few more seconds.", "Just a sec.", "One moment.", "Give me a moment.",
    "Hold on a sec.", "This'll just take a sec.",
])
def test_waiting_lines_and_fillers_are_skipped(line):
    assert _is_waiting_line(line)


@pytest.mark.parametrize("line", [
    CONFIRM, "Sure.", "Okay.", "Great.", "Perfect.",
    "Tuesday the sixteenth I've got nine or ten with Thao. Would either of those work?",
    "Perfect, booking that now for Tuesday at nine. Shall I go ahead?",
])
def test_elliots_own_lines_are_not_skipped(line):
    assert not _is_waiting_line(line)


@pytest.mark.parametrize("wait", ["Perfect, booking that now. It'll just take a few seconds.",
                                  "Thanks for waiting, nearly there.", "Just a sec."])
def test_confirmation_is_found_behind_the_waiting_lines(wait):
    turns = [("bot", CONFIRM), ("user", "Yes."), ("bot", wait)]
    found = find_confirmation(turns)
    assert found and found[0] == 0 and found[2]
    turns = [("bot", CONFIRM), ("user", "Yes."),
             ("bot", "Perfect, booking that now. It'll just take a few seconds."),
             ("bot", "Thanks for waiting, nearly there.")]
    assert find_confirmation(turns)[0] == 0


@pytest.mark.parametrize("cut", ["Perfect,", "Great,", "Okay,", "Okay, booking", "Thanks"])
def test_booking_waiting_line_cut_to_one_word_after_the_yes_is_skipped(cut):
    found = find_confirmation([("bot", CONFIRM), ("user", "Yes."), ("bot", cut)])
    assert found and found[0] == 0 and found[2]


@pytest.mark.parametrize("turns", [
    # Not right after a yes: Elliot's "Okay." is his own line.
    [("bot", CONFIRM), ("user", "Can we do ten instead?"), ("bot", "Okay.")],
    [("bot", CONFIRM), ("user", "No."), ("bot", "Perfect.")],
    # Not after the confirmation: an offer, then "yes", then "Perfect." is not a confirmation.
    [("bot", "Tuesday the sixteenth I've got nine or ten with Thao. Would either of those work?"),
     ("user", "Yes."), ("bot", "Perfect.")],
])
def test_a_lone_acknowledgment_elsewhere_is_not_skipped(turns):
    assert find_confirmation(turns) is None
    assert not _is_waiting_line(turns[-1][1])
