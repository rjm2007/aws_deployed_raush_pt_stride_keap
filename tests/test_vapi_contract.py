import json

from rpt_agent.vapi_contract import (
    outcome_from_ended_reason,
    parse_tool_calls,
    parse_tool_request,
    tool_error,
    tool_success,
)


def test_current_vapi_shape_preserves_order_and_ids():
    body = {"message": {"toolCallList": [
        {"id": "call-1", "name": "availability", "arguments": {"days": 3}},
        {"id": "call-2", "name": "book", "arguments": {"slot_token": "abc"}},
    ]}}
    calls = parse_tool_calls(body)
    assert [call.tool_call_id for call in calls] == ["call-1", "call-2"]
    assert calls[0].arguments == {"days": 3}


def test_legacy_vapi_shape_accepts_json_argument_string():
    body = {"message": {"toolCalls": [{
        "id": "legacy-1", "function": {"name": "status", "arguments": '{"outcome":"booked"}'},
    }]}}
    call = parse_tool_calls(body)[0]
    assert call.name == "status"
    assert call.arguments["outcome"] == "booked"


def test_transport_context_overrides_model_generated_ids():
    body = {"message": {
        "call": {"assistantOverrides": {"variableValues": {
            "lead_id": "trusted-lead", "outreach_event_id": "42",
        }}},
        "toolCallList": [{
            "id": "current-1", "name": "book",
            "arguments": {"lead_id": "hallucinated-lead", "outreach_event_id": 999},
        }],
    }}
    call = parse_tool_calls(body)[0]
    assert call.arguments["lead_id"] == "trusted-lead"
    assert call.arguments["outreach_event_id"] == "42"


def test_direct_tool_request_accepts_flat_body_without_returning_secret():
    request = parse_tool_request({"lead_id": "lead-1", "vapi_secret": "secret"})
    assert request.tool_call_id is None
    assert request.arguments == {"lead_id": "lead-1"}


def test_current_shape_accepts_json_argument_string():
    body = {"message": {"toolCallList": [{
        "id": "current-json", "name": "availability", "arguments": '{"days":2}',
    }]}}
    assert parse_tool_calls(body)[0].arguments == {"days": 2}


def test_current_shape_accepts_nested_function_contract():
    body = {"message": {"toolCallList": [{
        "id": "nested-current",
        "function": {"name": "check_availability", "arguments": '{"days":4}'},
    }]}}
    call = parse_tool_calls(body)[0]
    assert call.tool_call_id == "nested-current"
    assert call.name == "check_availability"
    assert call.arguments == {"days": 4}


def test_results_are_single_line_strings():
    success = tool_success("x", {"status": "ok", "text": "one\ntwo"})
    error = tool_error("y", "bad\nrequest")
    assert success["toolCallId"] == "x"
    assert isinstance(success["result"], str)
    assert "\n" not in success["result"]
    assert json.loads(success["result"])["status"] == "ok"
    assert error == {"toolCallId": "y", "error": "bad request"}


def test_invalid_payload_has_no_calls():
    assert parse_tool_calls({}) == []
    assert parse_tool_calls({"message": {"toolCallList": "wrong"}}) == []


def test_vapi_busy_and_unanswered_are_normal_no_answer_outcomes():
    assert outcome_from_ended_reason("customer-busy") == "no_answer"
    assert outcome_from_ended_reason("customer-did-not-answer") == "no_answer"
    assert outcome_from_ended_reason("voicemail") == "voicemail"
    assert outcome_from_ended_reason("assistant-ended-call") == "manual"
