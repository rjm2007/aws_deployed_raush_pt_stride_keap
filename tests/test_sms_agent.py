"""SMS agent loop (services/sms_agent.py) with a fake OpenAI client and fake tools: no network, no DB.

Covers: tool routing by name, fixed texts sent word for word (card last, others first), the model's
extra text, empty answer = no reply, round cap, timeout and model errors = nothing sent, a fixed text
kept when a later round fails, request shape (store=False, reasoning passed back), bad arguments.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from datetime import time as dtime

import pytest

from rpt_agent.config import get_settings
from rpt_agent.services import sms_agent
from rpt_agent.services import sms_agent_texts as texts
from rpt_agent.services.sms_agent import (
    AgentReply,
    LeadView,
    SandboxSetup,
    Turn,
    reply_sandbox,
    run_agent,
)
from rpt_agent.services.sms_agent_tools import ToolResult

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
TOOL_SPECS = [{"type": "function", "name": name, "description": "", "parameters": {}}
              for name in ("find_times", "confirm_visit", "book_visit", "hand_off", "send_intro",
                           "report_outcome", "flag_emergency", "list_therapists")]


@pytest.fixture(autouse=True)
def fixtures(monkeypatch):
    # Small fixtures in place of config/sms_agent_prompt.md and sms_agent_tools.json.
    monkeypatch.setattr(sms_agent, "load_prompt", lambda: "PROMPT")
    monkeypatch.setattr(sms_agent, "load_tools", lambda: TOOL_SPECS)


def settings(**update):
    return get_settings().model_copy(update={"sms_agent_max_rounds": 3,
                                             "sms_agent_timeout_seconds": 20.0, **update})


def lead(**update):
    values = {"is_new_person": False, "first_name": "Sam", "last_name": "Test",
              "date_of_birth": date(1990, 3, 14), "clinic": "Laguna Niguel",
              "service": "Physical Therapy", "referral_word": "knee", "status": "in_progress",
              "cadence_state": "active", "needs_review": False, "review_reason": "",
              "booked_visit": "", "recent_calls": []}
    values.update(update)
    return LeadView(**values)


class FakeResponses:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **request):
        self.requests.append({**request, "input": list(request["input"])})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


class FakeClient:
    def __init__(self, *script):
        self.responses = FakeResponses(script)


def text(value):
    return {"output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": value}]}]}


def calls(*pairs):
    output = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}]
    for index, (name, arguments) in enumerate(pairs):
        output.append({"type": "function_call", "name": name, "call_id": f"call_{index}",
                       "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)})
    return {"output": output}


class FakeTools:
    """Records calls; replies from a table (name -> ToolResult)."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []

    def _reply(self, name, **arguments):
        self.calls.append((name, arguments))
        return self.replies.get(name) or ToolResult(f"OK: {name}", name, name)

    def find_times(self, *, when, specific_date=None, month=None, time_of_day=None,
                   therapist=None, clinic=None):
        return self._reply("find_times", when=when, specific_date=specific_date,
                           time_of_day=time_of_day, therapist=therapist, clinic=clinic)

    def list_therapists(self, *, clinic=None):
        return self._reply("list_therapists", clinic=clinic)

    def confirm_visit(self, *, date, time, therapist=None, clinic=None):
        return self._reply("confirm_visit", date=date, time=time, therapist=therapist)

    def book_visit(self, *, date, time, therapist=None, first_name=None, last_name=None,
                   date_of_birth=None, clinic=None):
        return self._reply("book_visit", date=date, time=time)

    def report_outcome(self, *, status, notes=None, callback_type=None, delay_minutes=None,
                       callback_datetime_iso=None):
        return self._reply("report_outcome", status=status)

    def hand_off(self, *, reason, notes=None):
        return self._reply("hand_off", reason=reason)

    def flag_emergency(self):
        return self._reply("flag_emergency")

    def send_intro(self):
        return self._reply("send_intro")


def run(client, tools=None, history=None, **kwargs) -> AgentReply:
    return run_agent(lead=kwargs.pop("lead", lead()),
                     history=history or [Turn("patient", "Hi, anything Thursday?", None)],
                     tools=tools or FakeTools(), now=NOW, front_desk_open=True,
                     settings=kwargs.pop("settings", settings()), client=client, **kwargs)


def test_plain_answer_is_the_reply_and_request_is_private():
    client = FakeClient(text("We're open until 7:00 PM today."))
    reply = run(client)
    assert reply.texts == ["We're open until 7:00 PM today."]
    assert not reply.no_reply and reply.error is None
    request = client.responses.requests[0]
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["model"] == settings().sms_agent_model
    roles = [item["role"] for item in request["input"]]
    assert roles == ["developer", "developer", "user"]
    assert request["input"][0]["content"] == "PROMPT"
    assert "Context" in request["input"][1]["content"]


def test_model_choice_overrides_the_setting():
    client = FakeClient(text("Hi!"))
    run(client, model="gpt-test-mini")
    assert client.responses.requests[0]["model"] == "gpt-test-mini"


def test_tools_are_routed_by_name_and_results_fed_back_with_reasoning():
    tools = FakeTools(find_times=ToolResult("OPENINGS for PT: (1) ...", "find_times", "Looked"))
    client = FakeClient(
        calls(("find_times", {"when": "specific_date", "specific_date": "2026-11-05",
                              "time_of_day": "morning", "therapist": None, "clinic": None,
                              "month": None})),
        text("Here's what I have open Thu Nov 5:\n8:00 AM with Thao\nWhich one works for you?"),
    )
    reply = run(client, tools)
    assert tools.calls == [("find_times", {"when": "specific_date", "specific_date": "2026-11-05",
                                           "time_of_day": "morning", "therapist": None,
                                           "clinic": None})]
    assert reply.texts[0].startswith("Here's what I have open")
    assert [(a.kind, a.result_code) for a in reply.actions] == [("find_times", "OPENINGS")]
    second = client.responses.requests[1]["input"]
    # The reasoning item and the function call go back with the tool's output.
    assert {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"} in second
    output = [item for item in second if item.get("type") == "function_call_output"]
    assert output == [{"type": "function_call_output", "call_id": "call_0",
                       "output": "OPENINGS for PT: (1) ..."}]


def test_fixed_text_is_sent_word_for_word_and_model_may_add_one_text():
    handed = ToolResult("HANDED_OFF: sent", "hand_off", "Opened a card: insurance",
                        fixed_texts=["FRONT DESK TEXT"])
    client = FakeClient(calls(("hand_off", {"reason": "insurance", "notes": None})),
                        text("In the meantime, want me to find you a time?"))
    reply = run(client, FakeTools(hand_off=handed))
    assert reply.texts == ["FRONT DESK TEXT", "In the meantime, want me to find you a time?"]
    assert reply.actions[0].result_code == "HANDED_OFF"
    assert reply.actions[0].reason is None


def test_hand_off_action_carries_its_reason():
    # x-02: the test chat sends the reason back in prior_actions (routes/sms_agent_test.py).
    handed = ToolResult("HANDED_OFF: sent", "hand_off", "card", fixed_texts=["FRONT DESK TEXT"],
                        reason="insurance")
    client = FakeClient(calls(("hand_off", {"reason": "insurance"})), text("NO_REPLY"))
    reply = run(client, FakeTools(hand_off=handed))
    assert (reply.actions[0].result_code, reply.actions[0].reason) == ("HANDED_OFF", "insurance")


def test_card_goes_last_after_the_models_own_text():
    card = ToolResult("CARD_SENT: sent", "confirm_visit", "Read back",
                      fixed_texts=["CARD"], send_last=True)
    client = FakeClient(calls(("confirm_visit", {"date": "2026-11-05", "time": "10:30 AM",
                                                 "therapist": "Alan Rome", "clinic": None})),
                        text("Thanks for confirming!"))
    reply = run(client, FakeTools(confirm_visit=card))
    assert reply.texts == ["Thanks for confirming!", "CARD"]


def test_empty_answer_after_a_fixed_text_sends_only_the_fixed_text():
    intro = ToolResult("INTRO_SENT: sent", "fixed_text", "Sent intro", fixed_texts=["INTRO"])
    client = FakeClient(calls(("send_intro", {})), text(""))
    reply = run(client, FakeTools(send_intro=intro))
    assert reply.texts == ["INTRO"] and not reply.no_reply


BOOKED_TEXT = texts.render(None, "booked", first_name="Sam", day="Thursday, November 5",
                           time="10:30 AM", therapist="Thao Nguyen", clinic="Laguna Niguel")
AFTER_BOOKING = [Turn("patient", "yes", None), Turn("elliot", BOOKED_TEXT, None),
                 Turn("patient", "thanks!", None)]


@pytest.mark.parametrize("answer", ["", "   ", "NO_REPLY", "(empty answer)", "(no reply)", "NO_REPLY."])
def test_empty_answer_is_no_reply(answer):
    # A plain thank-you after the booked text: no answer needed, no second model call.
    client = FakeClient(text(answer))
    reply = run(client, history=AFTER_BOOKING, lead=lead(status="booked"))
    assert reply.texts == [] and reply.no_reply and reply.error is None
    assert len(client.responses.requests) == 1


def test_markdown_is_stripped():
    reply = run(FakeClient(text("**Dana Point**: 24582 Del Prado Ave")))
    assert reply.texts == ["Dana Point: 24582 Del Prado Ave"]


def test_round_cap_forces_a_text_answer_then_errors():
    loop = calls(("find_times", {"when": "earliest"}))
    client = FakeClient(loop, loop, loop, loop)
    reply = run(client, settings=settings(sms_agent_max_rounds=3))
    assert reply.error == "round_cap" and reply.texts == []
    assert len(client.responses.requests) == 4
    assert client.responses.requests[-1]["tool_choice"] == "none"
    assert all("tool_choice" not in r for r in client.responses.requests[:-1])


def test_round_cap_last_round_text_is_sent():
    loop = calls(("find_times", {"when": "earliest"}))
    client = FakeClient(loop, loop, text("Here's what I have open: ..."))
    reply = run(client, settings=settings(sms_agent_max_rounds=2))
    assert reply.texts == ["Here's what I have open: ..."] and reply.error is None


class APITimeoutError(Exception):
    pass


def test_model_timeout_sends_nothing():
    # Timed out, tried once more, timed out again: nothing sent.
    client = FakeClient(APITimeoutError("slow"), APITimeoutError("slow"))
    reply = run(client)
    assert reply.texts == [] and reply.error == "timeout" and not reply.no_reply
    assert len(client.responses.requests) == 2


def test_model_error_sends_nothing():
    reply = run(FakeClient(RuntimeError("boom")))
    assert reply.error == "model_error:RuntimeError" and reply.texts == []


def test_overall_deadline(monkeypatch):
    clock = iter([0.0, 0.0, 25.0, 25.0])
    monkeypatch.setattr(sms_agent.time, "monotonic", lambda: next(clock))
    reply = run(FakeClient(text("late answer")))
    assert reply.error == "timeout" and reply.texts == []


def test_fixed_text_survives_a_later_model_failure():
    """A booking made, then the model times out: the patient still gets the booked text."""
    booked = ToolResult("BOOKED: sent", "book", "Booked", fixed_texts=["BOOKED TEXT"])
    client = FakeClient(calls(("book_visit", {"date": "2026-11-05", "time": "10:30 AM"})),
                        APITimeoutError("slow"))
    reply = run(client, FakeTools(book_visit=booked))
    assert reply.texts == ["BOOKED TEXT"] and reply.error is None


def test_unknown_tool_and_bad_arguments_go_back_to_the_model():
    client = FakeClient(calls(("delete_everything", {}), ("find_times", "not json")),
                        text("Sorry, could you say that again?"))
    tools = FakeTools()
    reply = run(client, tools)
    assert tools.calls == []
    codes = [a.result_code for a in reply.actions]
    assert codes == ["UNKNOWN_TOOL", "BAD_ARGUMENTS"]
    outputs = [i["output"] for i in client.responses.requests[1]["input"]
               if i.get("type") == "function_call_output"]
    assert outputs[0].startswith("UNKNOWN_TOOL") and outputs[1].startswith("BAD_ARGUMENTS")


def test_missing_required_argument_is_reported_not_raised():
    client = FakeClient(calls(("confirm_visit", {"date": "2026-11-05"})), text("Which time?"))
    tools = FakeTools()
    reply = run(client, tools)
    assert tools.calls == [] and reply.actions[0].result_code == "MISSING_ARGUMENT"


def test_tool_value_error_becomes_its_reply():
    class Tools(FakeTools):
        def find_times(self, **kwargs):
            raise ValueError("UNKNOWN_LOCATION: we have no clinic called Irvine.")
    client = FakeClient(calls(("find_times", {"when": "earliest", "clinic": "Irvine"})),
                        text("We don't have an Irvine office."))
    reply = run(client, Tools())
    assert reply.actions[0].result_code == "UNKNOWN_LOCATION"


def test_history_roles_cadence_and_staff_are_labelled_not_assistant():
    history = [Turn("cadence", "Day 0 text", NOW), Turn("patient", "who is this", NOW),
               Turn("elliot", "Hi Sam, it's Elliot", NOW), Turn("staff", "Staff note", NOW),
               Turn("patient", "ok when?", NOW)]
    client = FakeClient(text("When works?"))
    run(client, history=history)
    items = client.responses.requests[0]["input"][2:]
    assert [i["role"] for i in items] == ["developer", "user", "assistant", "developer", "user"]
    assert "automatic schedule" in items[0]["content"] and "Day 0 text" in items[0]["content"]
    assert "clinic staff" in items[3]["content"]
    # Patient and Elliot turns carry their clinic time, so old texts read as old.
    assert items[2]["content"] == "[Mon Nov 2, 10:00 AM] Hi Sam, it's Elliot"
    assert items[1]["content"] == "[Mon Nov 2, 10:00 AM] who is this"


def test_turns_without_a_time_have_no_stamp():
    client = FakeClient(text("When works?"))
    run(client, history=[Turn("patient", "hi", None)])
    assert client.responses.requests[0]["input"][2]["content"] == "hi"


def test_a_copied_time_stamp_is_not_sent():
    reply = run(FakeClient(text("[Mon Nov 2, 10:00 AM] Sure, Thursday works.")))
    assert reply.texts == ["Sure, Thursday works."]


def test_context_block_has_the_lead_and_never_a_phone():
    client = FakeClient(text("Hi"))
    run(client, lead=lead(booked_visit="Tue Nov 3 at 9:00 AM with Alan Rome at Laguna Niguel",
                          recent_calls=["Mon Nov 2, 9:00 AM: answered. Summary: call me at [number]"]),
        booking_link="https://book.example/x", services="Pelvic Health, Physical Therapy")
    context = client.responses.requests[0]["input"][1]["content"]
    assert "Front desk open now: yes" in context
    assert "Sam Test" in context
    # Owner, 2026-11-03: the date on file is never shown to the model (it is never read to the
    # patient); check_birthday compares in code.
    assert "Birthday on file: yes" in context and "1990" not in context and "March 14" not in context
    assert "Date of birth confirmed in this chat: no" in context
    assert "Booked visit: Tue Nov 3 at 9:00 AM with Alan Rome" in context
    assert "https://book.example/x" in context
    assert "Services we book: Pelvic Health, Physical Therapy" in context
    assert "Monday, November 2, 2026, 10:00 AM clinic time" in context
    assert "+1" not in context


def test_context_names_a_front_desk_only_review_so_booking_goes_on():
    # Owner, 2026-11-02: Elliot's own front-desk card (insurance, ...) does not stop booking; the
    # model must not read it as "waiting for staff review".
    client = FakeClient(text("Hi"))
    run(client, lead=lead(status="needs_attention", cadence_state="paused", needs_review=True,
                          review_reason="insurance or costs", front_desk_only=True))
    context = client.responses.requests[0]["input"][1]["content"]
    assert ("Needs review: front desk only (insurance or costs; booking by text still works)"
            in context)
    assert "waiting for staff review" not in context
    client = FakeClient(text("Hi"))
    run(client, lead=lead(status="needs_attention", cadence_state="paused", needs_review=True,
                          review_reason="Patient wants a different case"))
    context = client.responses.requests[0]["input"][1]["content"]
    assert "Needs review: yes (Patient wants a different case)" in context
    assert "waiting for staff review" in context


def test_missing_prompt_file_is_an_error_not_a_crash(monkeypatch):
    def missing():
        raise FileNotFoundError("config/sms_agent_prompt.md")
    monkeypatch.setattr(sms_agent, "load_prompt", missing)
    reply = run(FakeClient())
    assert reply.error == "setup_failed:FileNotFoundError" and reply.texts == []


# The real loader, captured at import before the autouse fixture replaces the module attribute.
LOAD_TOOLS = sms_agent.load_tools


def test_load_tools_accepts_both_shapes(tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps([{"type": "function", "name": "find_times", "description": "d",
                                 "parameters": {"type": "object"}, "strict": True}]))
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps({"tools": [{"type": "function", "function": {
        "name": "hand_off", "parameters": {"type": "object"}}}]}))
    assert LOAD_TOOLS(str(flat)) == [{"type": "function", "name": "find_times", "description": "d",
                                      "parameters": {"type": "object"}, "strict": True}]
    assert LOAD_TOOLS(str(nested))[0]["name"] == "hand_off"


def test_real_config_files_load_and_name_every_contract_tool():
    sms_agent._load_tools.cache_clear()
    names = {tool["name"] for tool in LOAD_TOOLS()}
    assert names == {"find_times", "list_therapists", "confirm_visit", "book_visit",
                     "report_outcome", "hand_off", "flag_emergency", "send_intro",
                     "check_birthday"}


# --- sandbox entry (fake connection: proves it opens only a read-only transaction) ------------------

class FakeConn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        conn = self

        class Rows:
            def fetchone(self):
                if "from practices" in sql:
                    return {"id": 1, "holidays": [], "booking_link_url": "https://book.example/x",
                            "services": "Physical Therapy", "service_aliases": None}
                return None
        conn.last = sql
        return Rows()


def test_reply_sandbox_do_not_contact_never_answers(monkeypatch):
    monkeypatch.setattr(sms_agent, "transaction", lambda: pytest.fail("no DB for DNC"))
    reply = reply_sandbox(setup=SandboxSetup(status="do_not_contact"),
                          history=[Turn("patient", "hi", None)], client=FakeClient())
    assert reply.no_reply and reply.texts == []


def test_reply_sandbox_runs_read_only_with_the_chosen_model_and_forced_front_desk(monkeypatch):
    conn = FakeConn()

    @contextmanager
    def fake_transaction():
        yield conn
    monkeypatch.setattr(sms_agent, "transaction", fake_transaction)
    client = FakeClient(text("Hi!"))
    setup = SandboxSetup(first_name="Sam", clinic="Mission Viejo", front_desk="closed",
                         clock=datetime(2026, 11, 3, 10, 0),  # noqa: DTZ001 - the tab sends clinic local time
                         model="gpt-test")
    reply = reply_sandbox(setup=setup, history=[Turn("patient", "hi", None)], client=client)
    assert reply.texts == ["Hi!"]
    assert conn.sql[0] == "set transaction read only"
    request = client.responses.requests[0]
    assert request["model"] == "gpt-test"
    context = request["input"][1]["content"]
    assert "Front desk open now: no" in context  # forced closed, although 10 AM on a Tuesday
    assert "Tuesday, November 3, 2026, 10:00 AM" in context


def test_reply_sandbox_front_desk_auto_uses_the_clock(monkeypatch):
    conn = FakeConn()

    @contextmanager
    def fake_transaction():
        yield conn
    monkeypatch.setattr(sms_agent, "transaction", fake_transaction)
    client = FakeClient(text("Hi!"))
    # Sunday: closed.
    reply_sandbox(setup=SandboxSetup(clock=datetime(2026, 11, 8, 10, 0)),  # noqa: DTZ001 - local time
                  history=[Turn("patient", "hi", None)], client=client)
    assert "Front desk open now: no" in client.responses.requests[0]["input"][1]["content"]
    client = FakeClient(text("Hi!"))
    reply_sandbox(setup=SandboxSetup(clock=datetime(2026, 11, 9, 10, 0)),  # noqa: DTZ001 - local time
                  history=[Turn("patient", "hi", None)], client=client)
    assert "Front desk open now: yes" in client.responses.requests[0]["input"][1]["content"]


def test_sandbox_lead_new_person_and_statuses():
    new = sms_agent.sandbox_lead(SandboxSetup(who="new", referral_word="knee", status="booked"))
    assert new.is_new_person and new.status == "new" and new.referral_word == ""
    review = sms_agent.sandbox_lead(SandboxSetup(status="needs_attention"))
    assert review.needs_review and review.cadence_state == "paused"
    booked = sms_agent.sandbox_lead(SandboxSetup(status="booked"))
    assert booked.booked_visit and booked.cadence_state == "completed"


def test_the_agents_own_opt_out_is_passed_to_the_worker():
    # The worker still sends "We won't text you again." after Elliot opted the patient out.
    opted = ToolResult("REPORTED: texts stopped", "report", "Opted out of texts", opted_out=True)
    client = FakeClient(calls(("report_outcome", {"status": "do_not_contact"})),
                        text("Understood. We won't text you again."))
    reply = run(client, FakeTools(report_outcome=opted))
    assert reply.opted_out and reply.texts == ["Understood. We won't text you again."]
    assert not run(FakeClient(text("Hi!"))).opted_out


# --- reasoning include only for reasoning models (2026-11-02 review) ------------------------------

@pytest.mark.parametrize("model,reasoning", [
    ("gpt-5.5", True), ("gpt-5-mini", True), ("gpt-6", True), ("o4-mini", True), ("o3", True),
    ("openai/gpt-5-mini", True), ("gpt-4.1", False), ("gpt-4.1-mini", False), ("gpt-4o", False),
])
def test_reasoning_include_only_for_reasoning_models(model, reasoning):
    # gpt-4.1 picked in the test chat got include=["reasoning.encrypted_content"]: a 400, a 502.
    assert sms_agent.is_reasoning_model(model) is reasoning
    client = FakeClient(text("Hi!"))
    run(client, model=model)
    assert ("include" in client.responses.requests[0]) is reasoning


# --- a tool error after a booking keeps the booked text (2026-11-02 review) -----------------------

def test_tool_exception_after_a_booking_keeps_the_booked_text():
    # A database error in hand_off after book_visit in the same reply escaped run_agent: the
    # visit was made, and the patient never got the booked text.
    class Tools(FakeTools):
        def hand_off(self, *, reason, notes=None):
            raise RuntimeError("connection lost to patient Sam Test")  # message never logged

    class Trace:
        def __init__(self):
            self.events = []

        def log(self, event, **fields):
            self.events.append((event, fields))

    booked = ToolResult("BOOKED: sent", "book", "Booked", fixed_texts=["BOOKED TEXT"])
    trace = Trace()
    client = FakeClient(calls(("book_visit", {"date": "2026-11-05", "time": "10:30 AM"}),
                              ("hand_off", {"reason": "insurance"})),
                        text("Our team will follow up about your insurance."))
    reply = run(client, Tools(book_visit=booked), trace=trace)
    assert reply.error is None
    assert reply.texts == ["BOOKED TEXT", "Our team will follow up about your insurance."]
    assert [a.result_code for a in reply.actions] == ["BOOKED", "ERROR"]
    outputs = [i["output"] for i in client.responses.requests[1]["input"]
               if i.get("type") == "function_call_output"]
    assert outputs[1].startswith("ERROR:")
    errors = [fields for event, fields in trace.events if event == "sms_agent_tool_error"]
    assert errors == [{"tool": "hand_off", "error_category": "RuntimeError"}]
    assert "Sam" not in json.dumps(trace.events)


# --- the test chat remembers what Elliot did earlier (2026-11-02 review) --------------------------

def test_sandbox_remembers_a_case_change_from_prior_actions():
    # Turn 1 reported a case change (pretend); turn 2 "book me" booked in the test chat where live
    # refuses: the case change card stops booking.
    start = sms_agent.sandbox_lead(SandboxSetup())
    assert not start.booking_paused
    state = sms_agent.earlier_in_chat(
        [], [{"kind": "report", "result_code": "REPORTED_CASE_CHANGE"},
             {"kind": "hand_off", "result_code": "HANDED_OFF_PAUSED"}])
    lead_now = sms_agent.apply_earlier(start, state)
    assert lead_now.booking_paused and lead_now.status == "needs_attention"
    assert lead_now.cadence_state == "paused"


def test_sandbox_front_desk_card_earlier_does_not_stop_booking():
    state = sms_agent.earlier_in_chat([], [{"kind": "hand_off", "result_code": "HANDED_OFF"}])
    lead_now = sms_agent.apply_earlier(sms_agent.sandbox_lead(SandboxSetup()), state)
    assert lead_now.needs_review and lead_now.front_desk_only and not lead_now.booking_paused


def test_sandbox_remembers_a_booking_and_an_emergency_from_the_history_alone():
    from rpt_agent.services import sms_agent_texts as texts

    booked_text = texts.render(None, "booked", first_name="", day="Tuesday, November 3",
                               time="9:00 AM", therapist="Alan Rome", clinic="Laguna Niguel")
    body = texts.DRAFTS["booked"]
    history = [Turn("patient", "yes", None), Turn("elliot", booked_text, None)]
    lead_now = sms_agent.apply_earlier(sms_agent.sandbox_lead(SandboxSetup()),
                                       sms_agent.earlier_in_chat(history, None, body))
    assert lead_now.status == "booked" and lead_now.cadence_state == "completed"
    assert lead_now.booked_visit
    # The patient quoting part of it is not Elliot's text; a plain answer is not the booked text.
    assert not sms_agent.earlier_in_chat([Turn("patient", booked_text, None),
                                          Turn("elliot", "See you then!", None)], None, body).booked
    emergency = [Turn("elliot", texts.EMERGENCY_TEXT, None)]
    assert sms_agent.apply_earlier(sms_agent.sandbox_lead(SandboxSetup()),
                                   sms_agent.earlier_in_chat(emergency, None)).booking_paused


def test_sandbox_declined_earlier_and_case_change_on_a_booked_chat():
    state = sms_agent.earlier_in_chat([], [{"kind": "report", "result_code": "REPORTED_DECLINED"}])
    declined = sms_agent.apply_earlier(sms_agent.sandbox_lead(SandboxSetup()), state)
    assert (declined.status, declined.cadence_state) == ("declined", "terminated")
    # Booked, then a case change: the booking stays (like live), booking again is paused.
    state = sms_agent.earlier_in_chat([], [{"kind": "book", "result_code": "BOOKED"},
                                           {"kind": "report", "result_code": "REPORTED_CASE_CHANGE"}])
    booked = sms_agent.apply_earlier(sms_agent.sandbox_lead(SandboxSetup()), state)
    assert booked.status == "booked" and booked.booking_paused


def test_reply_sandbox_stops_after_an_opt_out_earlier_in_the_chat(monkeypatch):
    monkeypatch.setattr(sms_agent, "transaction", lambda: pytest.fail("no DB after an opt-out"))
    reply = reply_sandbox(setup=SandboxSetup(), history=[Turn("patient", "hi", None)],
                          client=FakeClient(),
                          prior_actions=[{"kind": "report", "result_code": "REPORTED_DO_NOT_CONTACT"}])
    assert reply.no_reply and reply.texts == []


def test_reply_sandbox_passes_the_earlier_state_to_the_model(monkeypatch):
    conn = FakeConn()

    @contextmanager
    def fake_transaction():
        yield conn
    monkeypatch.setattr(sms_agent, "transaction", fake_transaction)
    client = FakeClient(text("Our front desk will reach out."))
    reply_sandbox(setup=SandboxSetup(first_name="Sam"),
                  history=[Turn("patient", "my back too", None), Turn("patient", "book me", None)],
                  client=client,
                  prior_actions=[{"kind": "report", "result_code": "REPORTED_CASE_CHANGE"}])
    context = client.responses.requests[0]["input"][1]["content"]
    assert "Needs review: yes" in context


# --- code guards on the model's own text (gpt-5.5 eval, 2026-11-02) --------------------------------

CARD = texts.confirm_card(date(2026, 11, 5), dtime(10, 30), "Thao Nguyen", "Mission Viejo")
YES_TO_CARD = [Turn("patient", "10:30 works", None), Turn("elliot", CARD, None),
               Turn("patient", "YES", None)]
BIRTHDAY_YES = [Turn("patient", "10:30 AM works for me", None),
                Turn("elliot", "Just to confirm, is your birthday March 14?", None),
                Turn("patient", "Yes", None)]
FALSE_CLAIM = ("All set! You're booked for Thu Nov 5 at 10:30 AM with Thao Nguyen at our Mission "
               "Viejo office.")


class Trace:
    def __init__(self):
        self.events = []

    def log(self, event, **fields):
        self.events.append((event, fields))


def notes(request):
    return [item["content"] for item in request["input"]
            if item.get("role") == "developer" and str(item.get("content", "")).startswith("Code check")]


def test_false_booking_claim_after_a_yes_to_the_card_forces_book_visit():
    # book-03: the patient said YES to the card; the model texted "All set! You're booked" with no
    # book_visit. The claim is never sent; the model must call book_visit.
    booked = ToolResult("BOOKED: sent", "book", "Booked", fixed_texts=["BOOKED TEXT"])
    tools = FakeTools(book_visit=booked)
    trace = Trace()
    client = FakeClient(text(FALSE_CLAIM),
                        calls(("book_visit", {"date": "2026-11-05", "time": "10:30 AM"})),
                        text(""))
    reply = run(client, tools, history=YES_TO_CARD, trace=trace)
    assert reply.texts == ["BOOKED TEXT"] and reply.error is None
    assert client.responses.requests[1]["tool_choice"] == {"type": "function", "name": "book_visit"}
    assert "tool_choice" not in client.responses.requests[2]
    assert notes(client.responses.requests[1]) == [sms_agent.BOOK_NOTE]
    # The dropped answer is not fed back.
    assert FALSE_CLAIM not in json.dumps(client.responses.requests[1]["input"])
    assert ("sms_agent_guard", {"guard": "booking_claim", "step": "forced_book_visit"}) in trace.events
    # Categories only in the log, never the text.
    assert "All set" not in json.dumps(trace.events) and "Thao" not in json.dumps(trace.events)


def test_false_booking_claim_without_a_yes_gets_a_note_not_a_forced_booking():
    # book-12: "Booked: Wed Nov 4 ..." when no card had been sent.
    client = FakeClient(text("Booked: Wed Nov 4 at 1:00 PM with Alan Rome at Laguna Niguel."),
                        text("Which time works best for you?"))
    reply = run(client, history=BIRTHDAY_YES)
    assert reply.texts == ["Which time works best for you?"]
    assert "tool_choice" not in client.responses.requests[1]
    assert notes(client.responses.requests[1]) == [sms_agent.CLAIM_NOTE]


def test_a_second_false_claim_sends_nothing_and_flags():
    trace = Trace()
    client = FakeClient(text(FALSE_CLAIM), text("You're all booked, see you then!"))
    reply = run(client, history=BIRTHDAY_YES, trace=trace)
    assert reply.texts == [] and reply.error == "guard:booking_claim"
    assert ("sms_agent_guard", {"guard": "booking_claim", "step": "dropped"}) in trace.events


def test_false_claim_after_a_fixed_text_keeps_the_fixed_text_only():
    handed = ToolResult("HANDED_OFF: sent", "hand_off", "card", fixed_texts=["FRONT DESK TEXT"])
    client = FakeClient(calls(("hand_off", {"reason": "insurance"})),
                        text("You're booked for Thursday!"), text("Your visit is booked."))
    reply = run(client, FakeTools(hand_off=handed), history=BIRTHDAY_YES)
    assert reply.texts == ["FRONT DESK TEXT"] and reply.error is None


@pytest.mark.parametrize("answer,lead_status,codes_tool", [
    # The visit is on the books: saying so is true.
    ("You're booked for Tue Nov 3 at 9:00 AM with Alan.", "booked", None),
    # A callback closed with "all set" is not a booking claim.
    ("You're all set, we'll call you tomorrow at 10:00 AM.", "in_progress", "report_outcome"),
    ("Nothing is booked yet. Which time works?", "in_progress", None),
    ("I haven't booked anything yet: which day works?", "in_progress", None),
])
def test_true_or_unrelated_texts_pass_the_booking_guard(answer, lead_status, codes_tool):
    tools = FakeTools(report_outcome=ToolResult("REPORTED: callback_scheduled.", "report", "r",
                                                action_code="REPORTED_CALLBACK_SCHEDULED"))
    script = [calls((codes_tool, {"status": "callback_scheduled"}))] if codes_tool else []
    client = FakeClient(*script, text(answer))
    reply = run(client, tools, history=BIRTHDAY_YES, lead=lead(status=lead_status))
    assert reply.texts == [answer]


def test_booked_in_this_run_allows_the_models_see_you_then():
    booked = ToolResult("BOOKED: sent", "book", "Booked", fixed_texts=["BOOKED TEXT"])
    client = FakeClient(calls(("book_visit", {"date": "2026-11-05", "time": "10:30 AM"})),
                        text("See you then!"))
    reply = run(client, FakeTools(book_visit=booked), history=YES_TO_CARD)
    assert reply.texts == ["BOOKED TEXT", "See you then!"]


@pytest.mark.parametrize("own_card", [
    # book-02 / book-11 / book-04 / book-12: the model's own card, "holding", a promised card.
    ("Great. Please review this visit:\nSat Nov 7, 2026 at 8:00 AM\nThao Nguyen, DPT\nReply YES "
     "and I'll book it."),
    "I'm holding Fri Nov 6 at 9:00 AM with Alan for you. Please reply YES and I'll book it.",
    "Great, I'll send the visit card for Tue Nov 10 at 8:00 AM. Reply YES to that card.",
    '[Visit card sent for Wed Nov 4, 1:00 PM with Alan Rome at Laguna Niguel: "Reply YES"]',
])
def test_the_models_own_card_with_a_time_forces_confirm_visit(own_card):
    card = ToolResult("CARD_SENT: sent", "confirm_visit", "Read back", fixed_texts=["CARD"],
                      send_last=True)
    client = FakeClient(text(own_card),
                        calls(("confirm_visit", {"date": "2026-11-06", "time": "9:00 AM"})),
                        text(""))
    reply = run(client, FakeTools(confirm_visit=card), history=BIRTHDAY_YES)
    assert reply.texts == ["CARD"]
    assert client.responses.requests[1]["tool_choice"] == {"type": "function",
                                                           "name": "confirm_visit"}
    assert notes(client.responses.requests[1]) == [sms_agent.CARD_FORCED_NOTE]


def test_a_promised_card_without_a_time_gets_a_note():
    client = FakeClient(text("I'll send the visit card next."), text("Which time works for you?"))
    reply = run(client, history=BIRTHDAY_YES)
    assert reply.texts == ["Which time works for you?"]
    assert "tool_choice" not in client.responses.requests[1]
    assert notes(client.responses.requests[1]) == [sms_agent.CARD_NOTE]


def test_reply_yes_next_to_a_real_card_is_fine():
    card = ToolResult("CARD_SENT: sent", "confirm_visit", "Read back", fixed_texts=["CARD"],
                      send_last=True)
    client = FakeClient(calls(("confirm_visit", {"date": "2026-11-06", "time": "9:00 AM"})),
                        text("Just reply YES to the card below."))
    reply = run(client, FakeTools(confirm_visit=card), history=BIRTHDAY_YES)
    assert reply.texts == ["Just reply YES to the card below.", "CARD"]


def test_silence_after_the_birthday_yes_is_nudged_once():
    # book-01 / ls-02 / ...: NO_REPLY after "Yes" to the birthday check, the patient waited.
    card = ToolResult("CARD_SENT: sent", "confirm_visit", "Read back", fixed_texts=["CARD"],
                      send_last=True)
    client = FakeClient(text("NO_REPLY"),
                        calls(("confirm_visit", {"date": "2026-11-05", "time": "10:30 AM"})),
                        text(""))
    reply = run(client, FakeTools(confirm_visit=card), history=BIRTHDAY_YES)
    assert reply.texts == ["CARD"] and not reply.no_reply
    assert notes(client.responses.requests[1]) == [sms_agent.EMPTY_NOTE]
    assert "tool_choice" not in client.responses.requests[1]


def test_still_silent_after_the_nudge_sends_nothing_invented():
    trace = Trace()
    client = FakeClient(text(""), text("NO_REPLY"))
    reply = run(client, history=BIRTHDAY_YES, trace=trace)
    assert reply.texts == [] and reply.no_reply and reply.error is None
    assert ("sms_agent_guard", {"guard": "empty", "step": "dropped"}) in trace.events


@pytest.mark.parametrize("history,status,allowed", [
    # "thanks, I'll wait" after the hand-off text (ho-0x): no second hand-off, no answer.
    ([Turn("patient", "do you take Blue Shield?", None),
      Turn("elliot", texts.render(None, "front_desk"), None),
      Turn("patient", "thanks, I'll wait", None)], "needs_attention", True),
    (AFTER_BOOKING, "booked", True),
    ([Turn("elliot", BOOKED_TEXT, None), Turn("patient", "\U0001F44D", None)], "booked", True),
    ([Turn("elliot", texts.EMERGENCY_TEXT, None), Turn("patient", "ok", None)], "needs_attention",
     True),
    ([Turn("elliot", "Understood. We won't text you again.", None), Turn("patient", "k", None)],
     "in_progress", True),
    # "ok" to a question is an answer: reply.
    ([Turn("elliot", "Want me to look at Thursday instead?", None), Turn("patient", "ok", None)],
     "booked", False),
    # A yes is never a plain thank-you.
    (BIRTHDAY_YES, "in_progress", False),
    # A thank-you with nothing finished before it.
    ([Turn("elliot", "Here's what I have open: 9:00 AM with Thao.", None),
      Turn("patient", "thanks", None)], "in_progress", False),
])
def test_no_reply_only_after_a_thank_you_to_something_finished(history, status, allowed):
    client = FakeClient(text("NO_REPLY"), text("Sure thing."))
    review = status == "needs_attention"
    reply = run(client, history=history, lead=lead(status=status, needs_review=review))
    assert reply.no_reply is allowed
    assert len(client.responses.requests) == (1 if allowed else 2)


@pytest.mark.parametrize("value,ack", [
    ("ok", True), ("Okay, thanks.", True), ("thank you so much!", True), ("k", True),
    ("Got it, thank you", True), ("thanks, I'll wait", True), ("\U0001F44D\U0001F44D", True),
    ("yes", False), ("ok can you book me tuesday?", False), ("no thanks", False),
    ("(sent a picture or file, no text)", False),
])
def test_pure_acknowledgement(value, ack):
    assert sms_agent.pure_acknowledgement(value) is ack


@pytest.mark.parametrize("value,yes", [
    ("YES", True), ("ye bet", True), ("Yes please book it", True), ("ya", True),
    ("no", False), ("yes but wait", False), ("what about 9?", False),
])
def test_said_yes_to_card(value, yes):
    history = [Turn("elliot", CARD, None), Turn("patient", value, None)]
    assert sms_agent.said_yes_to_card(history) is yes
    # Not a yes to the card when Elliot's last text is something else.
    assert not sms_agent.said_yes_to_card([Turn("elliot", "Is your birthday March 14?", None),
                                           Turn("patient", value, None)])


def test_guard_reruns_count_toward_the_round_cap():
    # One tool round: the forced booking needs a tool round, so the re-run is the last (text only)
    # round with the note; a second claim there is dropped.
    client = FakeClient(text(FALSE_CLAIM), text(FALSE_CLAIM))
    reply = run(client, history=YES_TO_CARD, settings=settings(sms_agent_max_rounds=1))
    assert reply.texts == [] and reply.error == "guard:booking_claim"
    assert len(client.responses.requests) == 2
    assert client.responses.requests[1]["tool_choice"] == "none"


# --- review fixes (2026-11-02) --------------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    "Here's your visit: Tue Nov 3 at 9:00 AM with Alan at our Laguna Niguel office.",
    "You're booked for Tue Nov 3 at 9:00 AM. See you then!",
])
@pytest.mark.parametrize("status", ["booked", "needs_attention"])
def test_a_lead_with_a_visit_on_the_books_gets_its_visit_read_back(answer, status):
    # "when is my appointment?": the read-back is true for a booked lead (also one whose status is
    # not Booked but Context shows the visit). Dropped twice it failed the reply and flagged staff.
    client = FakeClient(text(answer))
    visit = lead(status=status, booked_visit="Tue Nov 3 at 9:00 AM with Alan at Laguna Niguel")
    reply = run(client, history=[Turn("patient", "when is my appointment again?", None)],
                lead=visit)
    assert reply.texts == [answer] and reply.error is None
    assert len(client.responses.requests) == 1


def test_a_booked_lead_still_cannot_get_a_homemade_card():
    # A copied card on a booked lead would let a "yes" pass the booking guard: still never sent.
    client = FakeClient(text(CARD), text("You're booked for Thu Nov 5 at 10:30 AM."))
    reply = run(client, history=[Turn("patient", "can I move it?", None)],
                lead=lead(status="booked", booked_visit="Thu Nov 5 at 10:30 AM with Thao"))
    assert reply.texts == ["You're booked for Thu Nov 5 at 10:30 AM."]
    assert notes(client.responses.requests[1]) == [sms_agent.CARD_FORCED_NOTE]


@pytest.mark.parametrize("value", [
    "yes but can we do 10 instead?", "Yes, actually make it Friday", "ok another time maybe",
])
def test_a_yes_that_asks_for_something_else_never_forces_a_booking(value):
    history = [Turn("elliot", CARD, None), Turn("patient", value, None)]
    assert not sms_agent.said_yes_to_card(history)
    client = FakeClient(text(FALSE_CLAIM), text("Sure, let me look at 10:00 AM."))
    run(client, history=history)
    assert "tool_choice" not in client.responses.requests[1]
    assert notes(client.responses.requests[1]) == [sms_agent.CLAIM_NOTE]


@pytest.mark.parametrize("value", ["see you then!", "Ok I'll call them", "will call them, thx"])
def test_the_prompts_closings_are_acknowledgements(value):
    # config/sms_agent_prompt.md "When to answer NO_REPLY" names "see you then" and "I'll call them".
    assert sms_agent.pure_acknowledgement(value)
    history = [Turn("elliot", BOOKED_TEXT, None), Turn("patient", value, None)]
    client = FakeClient(text("NO_REPLY"))
    reply = run(client, history=history, lead=lead(status="booked"))
    assert reply.no_reply and len(client.responses.requests) == 1


def test_context_spells_out_tomorrow():
    client = FakeClient(text("Hi"))
    run(client)  # NOW = Mon Nov 2, 10:00 AM clinic time
    context = client.responses.requests[0]["input"][1]["content"]
    assert "- Tomorrow: Tuesday, November 3, 2026 (2026-11-03)" in context


def test_sandbox_booked_lead_can_carry_its_visit_and_a_front_desk_only_review():
    # bk-02 / ls-01: the test chat had no way to give a Booked lead its visit, or to say only
    # Elliot's front-desk card is open.
    booked = sms_agent.sandbox_lead(SandboxSetup(status="booked",
                                                 booked_visit="Tue Nov 3 at 9:00 AM with Alan"))
    assert booked.booked_visit == "Tue Nov 3 at 9:00 AM with Alan"
    front_desk = sms_agent.sandbox_lead(SandboxSetup(status="in_progress",
                                                     open_review="front_desk"))
    assert front_desk.needs_review and front_desk.front_desk_only and not front_desk.booking_paused
    assert front_desk.status == "needs_attention"  # an open card pauses a lead in the cadence
    full = sms_agent.sandbox_lead(SandboxSetup(status="needs_attention"))
    assert full.booking_paused
    none = sms_agent.sandbox_lead(SandboxSetup(status="needs_attention", open_review="none"))
    assert not none.needs_review
    # A new person has neither.
    new = sms_agent.sandbox_lead(SandboxSetup(who="new", open_review="full"))
    assert not new.needs_review


def test_earlier_in_chat_knows_what_was_already_handed_off():
    front_desk = texts.render(None, "front_desk")
    state = sms_agent.earlier_in_chat(
        [Turn("elliot", front_desk, None)], None, hand_off_bodies=(texts.DRAFTS["front_desk"],))
    assert state.done == {"hand_off:front_desk"}
    state = sms_agent.earlier_in_chat([], [{"kind": "report", "result_code": "REPORTED_CASE_CHANGE"},
                                           {"kind": "hand_off", "result_code": "HANDED_OFF_PAUSED"},
                                           {"kind": "emergency", "result_code": "EMERGENCY_SENT"}])
    assert state.done == {"case_change", "hand_off:paused", "emergency"}
    # With the reason sent back (x-02) the repeat check gets that reason, not the kind; an unknown
    # reason falls back to the kind, and ALREADY_HANDED_OFF with a reason counts too.
    state = sms_agent.earlier_in_chat([], [
        {"kind": "hand_off", "result_code": "HANDED_OFF", "reason": " Insurance "},
        {"kind": "hand_off", "result_code": "HANDED_OFF_PAUSED", "reason": "nonsense"},
        {"kind": "hand_off", "result_code": "ALREADY_HANDED_OFF", "reason": "wants_person"}])
    assert state.done == {"hand_off:insurance", "hand_off:paused", "hand_off:wants_person"}
    assert state.front_desk and state.paused
    # The emergency text more than an hour ago may go again.
    old = [Turn("elliot", texts.EMERGENCY_TEXT, datetime(2026, 11, 2, 15, 0, tzinfo=UTC))]
    assert "emergency" not in sms_agent.earlier_in_chat(old, None, now=NOW).done
    assert "emergency" in sms_agent.earlier_in_chat(
        old, None, now=datetime(2026, 11, 2, 15, 30, tzinfo=UTC)).done


# --- round 2 (gpt-5.5 eval, 2026-11-02) -------------------------------------------------------------

def test_timeout_default_is_35_seconds_and_one_call_waits_at_most_20():
    from rpt_agent.config import Settings

    assert Settings.model_fields["sms_agent_timeout_seconds"].default == 35.0
    client = FakeClient(text("Hi!"))
    run(client, settings=settings(sms_agent_timeout_seconds=35.0))
    assert client.responses.requests[0]["timeout"] == sms_agent.CALL_TIMEOUT_SECONDS


def test_a_call_that_times_out_is_tried_once_more():
    # fact-09: one gpt-5.5 call hit the timeout; the same call again answered.
    trace = Trace()
    client = FakeClient(APITimeoutError("slow"), text("We're open until 7:00 PM today."))
    reply = run(client, settings=settings(sms_agent_timeout_seconds=35.0), trace=trace)
    assert reply.texts == ["We're open until 7:00 PM today."] and reply.error is None
    first, second = client.responses.requests
    assert first["input"] == second["input"] and second["timeout"] <= sms_agent.CALL_TIMEOUT_SECONDS
    assert ("sms_agent_model_retry", {"reason": "timeout", "round": 1}) in trace.events


def test_no_retry_without_enough_time_left_and_never_for_other_errors(monkeypatch):
    clock = iter([0.0, 0.0, 30.0])  # deadline, round start, after the timeout: 5 s left of 35
    monkeypatch.setattr(sms_agent.time, "monotonic", lambda: next(clock))
    client = FakeClient(APITimeoutError("slow"), text("never asked"))
    reply = run(client, settings=settings(sms_agent_timeout_seconds=35.0))
    assert reply.error == "timeout" and len(client.responses.requests) == 1
    monkeypatch.undo()
    client = FakeClient(RuntimeError("boom"), text("never asked"))
    assert run(client).error == "model_error:RuntimeError" and len(client.responses.requests) == 1


RUTH = "Ruth needs to text us here herself, or she can call our front desk at (949) 276-5401."
REPEAT_HISTORY = [Turn("patient", "I can book it for her", None), Turn("elliot", RUTH, None),
                  Turn("patient", "her birthday is Nov 30, just book it", None)]


def test_the_same_text_again_is_dropped_and_rerun_with_a_note():
    # x-05 / x-02: Elliot sent the exact same text twice in a row.
    trace = Trace()
    client = FakeClient(text("ruth needs to text us here herself or she can call our front desk "
                             "at 949 276 5401!"),
                        text("I can only book with Ruth herself, sorry."))
    reply = run(client, history=REPEAT_HISTORY, trace=trace)
    assert reply.texts == ["I can only book with Ruth herself, sorry."]
    assert notes(client.responses.requests[1]) == [sms_agent.REPEAT_NOTE]
    assert ("sms_agent_guard", {"guard": "repeat", "step": "note"}) in trace.events


def test_still_the_same_text_sends_nothing_and_no_reply_after_the_note_is_fine():
    reply = run(FakeClient(text(RUTH), text(RUTH)), history=REPEAT_HISTORY)
    assert reply.texts == [] and reply.no_reply and reply.error is None
    # NO_REPLY after the note is accepted without the empty-answer nudge.
    client = FakeClient(text(RUTH), text("NO_REPLY"))
    reply = run(client, history=REPEAT_HISTORY)
    assert reply.no_reply and len(client.responses.requests) == 2


def test_only_the_last_three_texts_count_as_a_repeat():
    older = [Turn("elliot", RUTH, None)] + [Turn("elliot", f"text {n}", None) for n in range(3)]
    assert not sms_agent.repeats_recent(RUTH, older + [Turn("patient", "ok?", None)])
    assert sms_agent.repeats_recent("Text 2.", older)
    assert not sms_agent.repeats_recent("", older)


@pytest.mark.parametrize("value,expected", [
    ("Got it, Aug 23, 1985.", {date(1985, 8, 23)}),
    ("born 8/23/85", {date(1985, 8, 23), date(2085, 8, 23)}),
    ("1985-08-23", {date(1985, 8, 23)}),
    ("23rd of August 1985", {date(1985, 8, 23)}),
    ("August 23rd, 1985", {date(1985, 8, 23)}),
    ("3/4/1990", {date(1990, 3, 4), date(1990, 4, 3)}),
    ("is your birthday August 23?", set()),
    ("Wed Nov 4 at 3:30 PM, call (949) 276-5401", set()),
])
def test_full_dates(value, expected):
    assert sms_agent.full_dates(value) == expected


def test_the_full_birthday_on_file_is_never_texted():
    # book-11: the model texted the birthday with the year.
    trace = Trace()
    client = FakeClient(text("Thanks! I have your birthday as 03/14/1990."),
                        text("Thanks! Which time works for you?"))
    reply = run(client, trace=trace)
    assert reply.texts == ["Thanks! Which time works for you?"]
    assert notes(client.responses.requests[1]) == [sms_agent.BIRTH_DATE_NOTE]
    # The dropped text is not fed back, and the log has no date.
    assert "03/14/1990" not in json.dumps(client.responses.requests[1]["input"])
    assert "1990" not in json.dumps(trace.events)
    # Month and day only is fine.
    assert run(FakeClient(text("Just to confirm, is your birthday March 14?"))).texts == [
        "Just to confirm, is your birthday March 14?"]


def test_a_birthday_the_patient_corrected_in_chat_is_never_texted_back():
    history = [Turn("elliot", "Just to confirm, is your birthday August 22?", None),
               Turn("patient", "no, it's August 23 1985", None)]
    card = ToolResult("CARD_SENT", "confirm_visit", "card", fixed_texts=["CARD"], send_last=True)
    client = FakeClient(calls(("confirm_visit", {"date": "2026-11-06", "time": "9:00 AM"})),
                        text("Got it, Aug 23, 1985."), text("Got it, thanks!"))
    reply = run(client, FakeTools(confirm_visit=card), history=history)
    assert reply.texts == ["Got it, thanks!", "CARD"]
    # A second time: only the fixed texts go; with none, staff are flagged.
    client = FakeClient(text("Got it, 8/23/1985."), text("Your birthday is August 23, 1985."))
    reply = run(client, history=history)
    assert reply.texts == [] and reply.error == "guard:birth_date"
    # A visit date with a year is not a birthday.
    assert run(FakeClient(text("That's Friday, November 6, 2026.")), history=history).texts == [
        "That's Friday, November 6, 2026."]


def test_a_fixed_text_with_the_birthday_is_not_checked():
    fixed = ToolResult("OK", "fixed_text", "x", fixed_texts=["DOB 03/14/1990"])
    reply = run(FakeClient(calls(("send_intro", {})), text("")), FakeTools(send_intro=fixed))
    assert reply.texts == ["DOB 03/14/1990"]


# --- round 3 (gpt-5.5 eval, 2026-11-02) --------------------------------------------------------------

HUSBAND = "This is Ruth's husband. What is this about? Is it about her knee?"
PUSHED = [Turn("patient", HUSBAND, None),
          Turn("elliot", "I can't book through someone else by text. Please have Ruth text us "
                         "directly or call our front desk.", None),
          Turn("patient", HUSBAND, None),
          Turn("elliot", "I can't share details here. Please have Ruth text us directly or call "
                         "our front desk at (949) 276-5401.", None),
          Turn("patient", HUSBAND, None)]


@pytest.mark.parametrize("answer,repeat", [
    # x-05: the same sentence, now inside a new text, the third time.
    ("Please have Ruth text us directly or call our front desk at (949) 276-5401.", True),
    (("i can't share details here!\nplease have ruth text us directly or call our front desk at "
      "949-276-5401"), True),
    # One new sentence is a new text.
    (("Please have Ruth text us directly or call our front desk at (949) 276-5401. "
      "We're happy to help her then."), False),
    ("Sorry, I can only talk about this with Ruth.", False),
])
def test_a_text_made_only_of_recent_sentences_is_a_repeat(answer, repeat):
    assert sms_agent.repeats_recent(answer, PUSHED) is repeat


def test_sentences_split_on_ends_and_line_breaks_only():
    assert sms_agent.sentences("Hi there! Call (949) 276-5401.\nThanks") == [
        "hithere", "call9492765401", "thanks"]
    assert sms_agent.sentences(" ... ") == []


def test_a_text_of_old_sentences_is_rerun_then_silence():
    # x-05: dropped and re-run with the note; NO_REPLY after it is fine; a second repeat sends
    # nothing.
    client = FakeClient(text("Please have Ruth text us directly or call our front desk at "
                             "(949) 276-5401."), text("NO_REPLY"))
    reply = run(client, history=PUSHED)
    assert reply.no_reply and reply.error is None
    assert notes(client.responses.requests[1]) == [sms_agent.REPEAT_NOTE]
    again = "I can't share details here."
    reply = run(FakeClient(text(again), text(again)), history=PUSHED)
    assert reply.texts == [] and reply.no_reply and reply.error is None


NO_HAND_OFF = ("I couldn't finish booking that by text, so our front desk will finish setting up "
               "your visit.")


@pytest.mark.parametrize("answer,promise", [
    (NO_HAND_OFF, True),
    ("Our front desk team will reach out to you about your visit.", True),
    ("Someone from our front desk will call you soon.", True),
    ("I'll let our front desk know.", True),
    ("I've passed this along to our front desk.", True),
    # A pointer to the front desk is not a promise.
    ("Please have Ruth text us directly or call our front desk at (949) 276-5401.", False),
    ("Or call our front desk: (949) 276-5401", False),
    ("Our front desk can help with that at (949) 276-5401.", False),
])
def test_promises_front_desk(answer, promise):
    assert sms_agent.promises_front_desk(answer) is promise


ASKED_NAME = [Turn("elliot", "What's your first name?", None), Turn("patient", "Quill", None)]


def test_a_front_desk_promise_without_a_hand_off_is_rerun_with_a_note():
    # x-04: "our front desk will finish setting up your visit" with no hand_off: nobody calls.
    trace = Trace()
    client = FakeClient(text(NO_HAND_OFF), text("Thanks! And what's your first name?"))
    reply = run(client, history=ASKED_NAME, trace=trace)
    assert reply.texts == ["Thanks! And what's your first name?"]
    assert notes(client.responses.requests[1]) == [sms_agent.FRONT_DESK_NOTE]
    assert ("sms_agent_guard", {"guard": "front_desk_promise", "step": "note"}) in trace.events
    # The model may call hand_off after the note: then the promise is true.
    handed = ToolResult("HANDED_OFF: sent", "hand_off", "card", fixed_texts=["FRONT DESK TEXT"],
                        action_code="HANDED_OFF_PAUSED", reason="booking_problem")
    client = FakeClient(text(NO_HAND_OFF), calls(("hand_off", {"reason": "booking_problem"})),
                        text(NO_HAND_OFF))
    reply = run(client, FakeTools(hand_off=handed), history=ASKED_NAME)
    assert reply.texts == ["FRONT DESK TEXT", NO_HAND_OFF] and reply.error is None


def test_a_second_front_desk_promise_sends_nothing_invented():
    reply = run(FakeClient(text(NO_HAND_OFF), text(NO_HAND_OFF)), history=ASKED_NAME)
    assert reply.texts == [] and reply.error == "guard:front_desk_promise"


@pytest.mark.parametrize("tool,result,lead_update,history_extra", [
    # A hand-off in this run.
    ("hand_off", ToolResult("HANDED_OFF: sent", "hand_off", "c", fixed_texts=["FD"],
                            action_code="HANDED_OFF_PAUSED"), {}, []),
    # A failed booking's own card (the engine opened it).
    ("book_visit", ToolResult("FOLLOW_UP: not booked", "book", "f", fixed_texts=["FD"]), {}, []),
    # A review that stops booking: the prompt's own line.
    ("book_visit", ToolResult("REVIEW_OPEN: in review", "book", "r"), {}, []),
    # A callback reported in this run: "our team will call you at 10".
    ("report_outcome", ToolResult("REPORTED: callback_scheduled.", "report", "r",
                                  action_code="REPORTED_CALLBACK_SCHEDULED"), {}, []),
    # The front desk already has it from an earlier reply (open review).
    (None, None, {"needs_review": True}, []),
    # The front desk text earlier in the chat (an older test chat tab: no side panel codes).
    (None, None, {}, [Turn("elliot", texts.DRAFTS["front_desk"].replace(
        "{front_desk_phone}", "(949) 276-5401"), None)]),
])
def test_a_true_front_desk_promise_passes(tool, result, lead_update, history_extra):
    answer = "Our front desk team will reach out to you about your visit."
    script = [calls((tool, {"reason": "x", "status": "callback_scheduled", "date": "2026-11-06",
                            "time": "9:00 AM"}))] if tool else []
    tools = FakeTools(**({tool: result} if tool else {}))
    reply = run(FakeClient(*script, text(answer)), tools,
                history=[*history_extra, *ASKED_NAME], lead=lead(**lead_update))
    assert answer in reply.texts and reply.error is None


# --- eval 2026-11-03 (x-04): never ask for a last name that is on file ---------------------------------

@pytest.mark.parametrize("answer,asks", [
    ("And what's your last name?", True),
    ("Could you send your first and last name?", True),
    ("Please text me your surname.", True),
    ("What's your first name?", False),
    ("Thanks! Which time works best?", False),
])
def test_asks_last_name(answer, asks):
    assert sms_agent.asks_last_name(answer) is asks


def test_asking_for_a_last_name_on_file_is_dropped_and_rerun_once():
    no_first = lead(first_name="", last_name="Quill")
    trace = Trace()
    client = FakeClient(text("And what's your last name?"), text("What's your first name?"))
    reply = run(client, history=[Turn("patient", "yes", None)], lead=no_first, trace=trace)
    assert reply.texts == ["What's your first name?"] and reply.error is None
    assert notes(client.responses.requests[1]) == [sms_agent.ASKED_LAST_NAME_NOTE]
    assert ("sms_agent_guard", {"guard": "asked_last_name", "step": "note"}) in trace.events
    # A second miss sends nothing of the model's.
    reply = run(FakeClient(text("What's your last name?"), text("And your last name?")),
                history=[Turn("patient", "yes", None)], lead=no_first)
    assert reply.texts == [] and reply.error == "guard:asked_last_name"
    # No last name on file, or someone new: the question goes out.
    for view in (lead(last_name=""), lead(is_new_person=True, first_name="", last_name="")):
        reply = run(FakeClient(text("What's your last name?")),
                    history=[Turn("patient", "hi", None)], lead=view)
        assert reply.texts == ["What's your last name?"]


def test_context_says_when_only_a_last_name_is_on_file():
    client = FakeClient(text("Hi"))
    run(client, lead=lead(first_name="", last_name="Quill"))
    context = client.responses.requests[0]["input"][1]["content"]
    assert "Name on file: Quill (last name only: no first name on file)" in context
    client = FakeClient(text("Hi"))
    run(client)
    assert "Name on file: Sam Test\n" in client.responses.requests[0]["input"][1]["content"]
