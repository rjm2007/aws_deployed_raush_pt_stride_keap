import json
from pathlib import Path


def test_vapi_tools_are_sync_strict_and_have_concise_start_messages():
    tools = json.loads(Path("config/vapi_tools.json").read_text(encoding="utf-8"))
    assert {tool["function"]["name"] for tool in tools} == {
        "check_availability", "create_appointment", "update_lead_status"
    }
    expected_paths = {
        "check_availability": "/api/v1/tools/check-availability",
        "create_appointment": "/api/v1/tools/create-appointment",
        "update_lead_status": "/api/v1/webhooks/vapi/lead-status",
    }
    for tool in tools:
        assert tool["async"] is False
        assert tool["function"]["strict"] is True
        assert tool["function"]["parameters"]["additionalProperties"] is False
        static_keys = {item["key"] for item in tool["parameters"]}
        assert "lead_id" in static_keys
        assert "lead_id" not in tool["function"]["parameters"]["properties"]
        assert tool["server"]["url"].endswith(expected_paths[tool["function"]["name"]])
        assert tool["server"]["credentialId"]
    assert "outreach_event_id" in {item["key"] for item in tools[1]["parameters"]}
    assert "outreach_event_id" in {item["key"] for item in tools[2]["parameters"]}
    slow_tools = tools[:2]
    assert all(tool["messages"][0]["type"] == "request-start" for tool in slow_tools)


def test_callback_is_recorded_immediately_after_confirmation():
    prompt = Path("config/vapi_assistant_prompt.md").read_text(encoding="utf-8")
    assert "call `update_lead_status` immediately" in prompt
    assert "Do not speak again or end the call until the tool" in prompt


def test_booking_prompt_waits_on_in_progress_instead_of_saying_the_system_is_down():
    # Demo 2026-10-26: book_appointment timed out, the retry answered IN_PROGRESS (the first
    # request was still finishing), and with no rule for it the agent told a booked patient
    # "our booking system isn't responding".
    prompt = Path("config/vapi_booking_assistant_prompt.md").read_text(encoding="utf-8")
    rule = next(line for line in prompt.splitlines() if line.startswith("- IN_PROGRESS →"))
    block = prompt[prompt.index(rule):prompt.index("- PROVIDERS →")]
    # No spoken holding line: rule 4 forbids "one moment" and the tool's own waiting message plays.
    assert "Say nothing; call the booking tool again with the" in block
    assert "at most twice" in block
    assert "UNCONFIRMED line" in block
    assert "never after an IN_PROGRESS reply" in prompt
    retry = prompt[prompt.index("- Anything else, or no answer →"):]
    assert retry.index("IN_PROGRESS") < retry.index("isn't responding")


FRONT_DESK = ("Our front desk team will take care of your appointment and reach out to you shortly. "
              "Thank you, and have a wonderful day. Goodbye!")


def test_booking_prompt_never_promises_a_text_when_the_booking_was_not_made():
    # Owner, 2026-10-26: a booking that did not go through (or is unclear) ends with the front
    # desk line, never "we'll confirm by text" / "our team will call you to finish booking"; the
    # backend puts every such lead in Needs Review.
    prompt = " ".join(Path("config/vapi_booking_assistant_prompt.md").read_text(
        encoding="utf-8").split())
    for code in ("- PATIENT_EXISTS →", "- UNCONFIRMED or FOLLOW_UP →"):
        rule = prompt[prompt.index(code):]
        rule = rule[:rule.index(" - ", 3)]
        assert FRONT_DESK in rule, code
        assert "by text" not in rule and "finish booking" not in rule
    retry = prompt[prompt.index("- Anything else, or no answer →"):]
    assert ("I'm sorry, our booking system isn't responding right now. " + FRONT_DESK) in retry
    assert "confirm your appointment by text" not in prompt
    assert "call you to finish booking" not in prompt
    # A real booking still mentions the confirmation text, which really is sent.
    assert "You'll get a text with the details." in prompt


def test_booking_prompt_asks_last_name_first_and_never_books_without_it():
    # Owner, 2026-10-26: a one-word name on file got booked with last_name "" (MISSING_NAME).
    prompt = " ".join(Path("config/vapi_booking_assistant_prompt.md").read_text(
        encoding="utf-8").split())
    name_step = prompt[prompt.index("3f. **"):prompt.index("3g. **")]
    ask = name_step.index("\"And what's your last name?\"")
    assert ask < name_step.index("- Birthday:")
    assert "one word" in name_step and "wait for the answer" in name_step
    book_step = prompt[prompt.index("3g. **"):prompt.index("3h. **")]
    assert "Never call it with an empty or missing `last_name`" in book_step


def test_booking_replies_tell_the_agent_to_use_the_front_desk_line():
    import inspect

    from rpt_agent.routes import booking_tools
    from rpt_agent.services import slot_booking

    source = inspect.getsource(slot_booking.book_slot) + inspect.getsource(booking_tools)
    assert "confirm their appointment by text" not in source
    assert "call them back" not in source
    assert "front desk team will take care" in slot_booking.FRONT_DESK_LINE


def test_route_error_reply_starts_with_unconfirmed():
    # Tester 2026-10-26: it had no code, so the prompt's "anything else" rule retried a reply that
    # said "Do not try again".
    import inspect

    from rpt_agent.routes import booking_tools

    assert '"UNCONFIRMED: something went wrong while booking. Do not try again.' in \
        inspect.getsource(booking_tools.book_appointment_tool).replace("f\"", "\"")


def test_book_tool_description_matches_the_in_progress_rule():
    tools = json.loads(Path("config/vapi_booking_tools.json").read_text(encoding="utf-8"))
    book = next(t for t in tools["tools"] if t["function"]["name"] == "book_appointment")
    text = book["function"]["description"]
    assert "Call it once" not in text
    assert "IN_PROGRESS" in text and "same details, at most twice" in text
    assert "front desk" in text
