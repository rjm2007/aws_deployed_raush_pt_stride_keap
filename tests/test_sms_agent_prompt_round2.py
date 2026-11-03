"""Round-2 and round-3 gpt-5.5 eval fixes (2026-11-02) in the shipped SMS prompt and tool descriptions.

One test per finding, so a later edit cannot drop a rule silently. Eval results:
workspace `test/sms-agent-eval-2026-11-02/results-gpt-5.5-round2.json`.
"""

from __future__ import annotations

import re

from rpt_agent.services import sms_agent

PROMPT = sms_agent.PROMPT_FILE.read_text(encoding="utf-8")
TOOLS = {tool["name"]: tool for tool in sms_agent.load_tools(str(sms_agent.TOOLS_FILE))}
EXAMPLES = re.findall(r"<example>\n(.*?)\n</example>", PROMPT, re.DOTALL)


def _flat(text: str) -> str:
    return " ".join(text.split())


FLAT = _flat(PROMPT)


def _texts() -> list[str]:
    return [line[len("Text: "):] for example in EXAMPLES for line in example.splitlines()
            if line.startswith("Text: ")]


def test_service_case_change_names_the_service_on_the_referral():
    # ho-06: "pelvic floor" with a Physical Therapy referral got "Your referral is for a different visit".
    assert ("Your referral is for our [Service] service, so our front desk will set up the [what they "
            "asked for, e.g. pelvic] visit with you.") in FLAT
    assert ("Your referral is for our Physical Therapy service, so our front desk will set up the pelvic "
            "visit with you.") in _texts()
    # The extra line must agree with the front desk text ("they'll reach out"), never "they'll set up".
    assert not any("so they'll set up" in text for text in _texts())


def test_holiday_callback_names_the_holiday_and_keeps_the_patients_time():
    # cb-04: "We don't make calls on holidays." with 8:00 AM instead of naming Thanksgiving and 10 AM.
    assert "Thanksgiving Thu Nov 26 2026" in FLAT
    assert "We're closed for Thanksgiving on Thu Nov 26. Would Fri Nov 27 at 10:00 AM work?" in _texts()
    assert "keep the clock time they asked for if it is inside callback hours" in FLAT
    assert "on holidays" not in PROMPT


def test_plain_yes_after_a_therapist_list_searches_at_once():
    # book-06: "yes" three times after the therapist list before Elliot searched.
    assert ('a plain "yes", "sure", "ok" or "any" = find_times when=earliest at once, with the therapist '
            "they named if any. Never ask about days twice.") in FLAT
    example = next(e for e in EXAMPLES if "list_therapists" in e)
    lines = example.splitlines()
    yes = lines.index("Patient: yes")
    assert lines[yes + 1].startswith("Tool: find_times when=earliest, therapist=")
    assert "a plain 'yes' or 'sure' after your day question or a therapist list" in (
        TOOLS["find_times"]["description"])
    assert "('can I see Alan?') is a search: find_times" in TOOLS["list_therapists"]["description"]


def test_date_of_birth_is_never_written_with_the_year():
    # book-11: "Got it, Aug 23, 1985." Since 2026-11-03 no date of birth is written back at all.
    assert "Never write a date of birth in a text" in FLAT and "never the year" in FLAT
    for text in _texts():
        assert not re.search(r"\b(19|20)\d{2}\b", text), text
    assert "never their date of birth" in TOOLS["confirm_visit"]["description"]


def test_no_clinicians_offers_the_clinics_it_lists():
    # np-03: pelvic at Dana Point got two hand-offs instead of "we see pelvic patients at ...".
    assert 'NO_CLINICIANS with "Offered at": offer those clinics in one short line' in FLAT
    description = TOOLS["find_times"]["description"]
    assert "NO_CLINICIANS" in description and "'Offered at'" in description
    assert "UNKNOWN_SERVICE or NO_CLINICIANS" not in description


def test_missing_name_asks_for_the_first_name_once():
    assert 'MISSING_NAME: ask once "What\'s your first name?"' in FLAT
    assert "MISSING_NAME = we have no first name: ask once" in TOOLS["book_visit"]["description"]


# --- round 3 (2026-11-02, results-gpt-5.5-round3.json) ----------------------------------------------

def test_missing_name_sends_the_card_again_and_never_asks_for_the_last_name():
    # x-04: after the first name the model asked for the last name (on file), then promised the
    # front desk. book_visit right after the name is always NOT_CONFIRMED (the card is not the
    # latest text), so the next step is confirm_visit.
    assert ("with their answer, call confirm_visit for the same time with first_name in that turn "
            "(the card goes out again), then book_visit after their yes") in FLAT
    assert "never the last name: it is on file" in FLAT
    assert "Never hand off for a missing name." in FLAT
    description = TOOLS["book_visit"]["description"]
    assert "with their answer call confirm_visit for the same time" in description


def test_front_desk_is_promised_only_after_a_hand_off():
    assert ("Never say our front desk (or our team) will reach out, call or finish something "
            "unless hand_off did it") in FLAT


def test_the_same_request_again_gets_one_new_line_or_no_reply():
    # x-05: "Please have Ruth text us directly ..." went out three times.
    assert ("The patient sends the same request again after you answered it: reply once with a "
            "short new line") in FLAT
    assert "the same push again gets one short new line or NO_REPLY" in FLAT


def test_a_moved_search_day_is_named():
    # book-12: "nothing this afternoon" for a search of tomorrow.
    assert 'A today or tomorrow search reply names the day it searched ("Searched tomorrow' in FLAT
    assert "'Searched tomorrow, Thu Nov 5'" in TOOLS["find_times"]["description"]


# --- eval 2026-11-03 (results-gpt-5.5-dob.json) -------------------------------------------------------

def test_the_first_name_is_asked_before_the_card_and_never_the_last_name_on_file():
    # x-04: after the yes to the card Elliot asked "what's your last name?" (on file) and later
    # passed the last name as first_name.
    assert "Never ask for a last name that is on file." in FLAT
    assert ("ask only \"What's your first name?\" (never the last name: it is on file), then pass it "
            "as first_name to confirm_visit") in FLAT
    assert "the card comes after the first name, never before" in FLAT
    confirm = TOOLS["confirm_visit"]
    assert "MISSING_NAME = no card was sent" in confirm["description"]
    assert "first_name" in confirm["parameters"]["required"]
    assert confirm["parameters"]["properties"]["first_name"]["type"] == ["string", "null"]


def test_no_remark_on_whether_the_date_of_birth_matches_the_file():
    # dob-01: "Thanks, we already have your identity confirmed."
    assert "Never remark on whether their date matches our records" in FLAT
    assert '"identity confirmed"' in FLAT
