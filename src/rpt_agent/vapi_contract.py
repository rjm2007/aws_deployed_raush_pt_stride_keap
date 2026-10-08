from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolRequest:
    tool_call_id: str | None
    call_id: str | None
    arguments: dict[str, Any]


def extract_vapi_context(body: dict[str, Any]) -> dict[str, Any]:
    """Extract transport-supplied values without trusting model-generated arguments.

    Vapi has emitted these values at several nesting levels over time. Keeping this
    small compatibility layer lets the workflow use IDs injected when the outbound
    call was created, rather than asking the voice model to repeat them correctly.
    """
    if not isinstance(body, dict):
        return {}
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    # Vapi's Chat API (text testing) sends the same overrides on `chat` instead of `call`.
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    candidates = [
        ((call.get("assistantOverrides") or {}).get("variableValues") or {}),
        ((chat.get("assistantOverrides") or {}).get("variableValues") or {}),
        call.get("variableValues") or {},
        message.get("variableValues") or {},
        body.get("variableValues") or {},
        body.get("variables") or {},
    ]
    # Vapi has moved these between payload shapes (call, chat, assistant). Any
    # other variableValues block Vapi sent counts too, but never one inside the
    # tool-call lists, which hold what the model typed.
    candidates.extend(values for _, values in _variable_blocks(message))
    trusted: dict[str, Any] = {}
    for values in candidates:
        if isinstance(values, dict):
            for key in ("lead_id", "outreach_event_id"):
                if values.get(key) not in (None, ""):
                    trusted.setdefault(key, values[key])
    return trusted


MODEL_CONTROLLED = {"toolCallList", "toolCalls", "toolWithToolCallList", "messages", "artifact"}


def _variable_blocks(node: Any, path: str = "message", depth: int = 0):
    """Yield (path, dict) for every variableValues block Vapi itself sent."""
    if depth > 5 or not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in MODEL_CONTROLLED:
            continue
        if key == "variableValues" and isinstance(value, dict):
            yield f"{path}.{key}", value
        elif isinstance(value, dict):
            yield from _variable_blocks(value, f"{path}.{key}", depth + 1)


def variable_source(body: dict[str, Any]) -> str | None:
    """Where the lead id came from, for logs (a path, never a value)."""
    message = body.get("message") if isinstance(body, dict) else None
    for path, values in _variable_blocks(message if isinstance(message, dict) else {}):
        if values.get("lead_id"):
            return path
    return None


def _coerce_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def outcome_from_ended_reason(ended_reason: str) -> str:
    """Map provider telephony outcomes; conversational tools own answered-call outcomes."""
    reason = ended_reason.strip().lower()
    if "voicemail" in reason:
        return "voicemail"
    if reason in {"customer-busy", "customer-did-not-answer"}:
        return "no_answer"
    if "sip-408" in reason or "sip-480" in reason:
        return "no_answer"
    return "manual"


def parse_tool_calls(body: dict[str, Any]) -> list[ToolCall]:
    """Parse current Vapi toolCallList plus the documented legacy OpenAI-style shape."""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, dict):
        return []
    trusted = extract_vapi_context(body)
    current = message.get("toolCallList")
    if isinstance(current, list):
        calls: list[ToolCall] = []
        for item in current:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            arguments = _coerce_arguments(
                item.get(
                    "arguments",
                    item.get("parameters", function.get("arguments", function.get("parameters", {}))),
                )
            )
            calls.append(
                ToolCall(
                    str(item["id"]),
                    str(item.get("name") or function.get("name") or ""),
                    {**arguments, **trusted},
                )
            )
        return calls

    legacy = message.get("toolCalls")
    if not isinstance(legacy, list):
        return []
    calls = []
    for item in legacy:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        arguments = _coerce_arguments(function.get("arguments", {}))
        calls.append(ToolCall(
            str(item["id"]),
            str(function.get("name") or item.get("name") or ""),
            {**arguments, **trusted},
        ))
    return calls


def parse_tool_request(body: dict[str, Any]) -> ToolRequest:
    """Accept one Vapi tool envelope or a flat JSON body for direct endpoint testing."""
    calls = parse_tool_calls(body)
    message = body.get("message") if isinstance(body, dict) else None
    message = message if isinstance(message, dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    # A chat turn gets a new id every message; its session (if any) is the stable key.
    call_id = call.get("id") or chat.get("sessionId") or body.get("call_id")
    if calls:
        return ToolRequest(calls[0].tool_call_id, str(call_id) if call_id else None, calls[0].arguments)
    arguments = dict(body) if isinstance(body, dict) else {}
    arguments.pop("vapi_secret", None)
    return ToolRequest(None, str(call_id) if call_id else None, arguments)


def _single_line(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, separators=(",", ":"), default=str)
    return " ".join(text.splitlines())


def tool_success(call_id: str, value: Any, name: str | None = None) -> dict[str, str]:
    result = {"toolCallId": call_id, "result": _single_line(value)}
    if name:
        result["name"] = name
    return result


def tool_error(call_id: str, value: Any, name: str | None = None) -> dict[str, str]:
    result = {"toolCallId": call_id, "error": _single_line(value)}
    if name:
        result["name"] = name
    return result


def direct_tool_response(tool_call_id: str | None, message: str) -> dict[str, Any]:
    """Return Vapi's result envelope, or a small flat response for Postman/terminal calls."""
    if tool_call_id:
        return {"results": [tool_success(tool_call_id, message)]}
    return {"message": _single_line(message)}
