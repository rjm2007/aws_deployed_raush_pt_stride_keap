from fastapi import HTTPException, Request

from ..db import record_integration_event
from ..observability import WorkflowTrace, trace_id_var
from ..security import require_vapi_auth
from ..vapi_contract import ToolRequest, parse_tool_request


async def authenticated_tool_request(
    request: Request, workflow: str
) -> tuple[WorkflowTrace, ToolRequest]:
    trace = WorkflowTrace(workflow, "api", trace_id_var.get())
    try:
        await require_vapi_auth(request)
        trace.log("authentication_passed", provider="vapi")
    except HTTPException:
        trace.log("authentication_failed", provider="vapi", error_category="HTTPException")
        raise
    body = await request.json()
    if not isinstance(body, dict):
        raise TypeError("request body must be a JSON object")
    parsed = parse_tool_request(body)
    record_integration_event(
        trace.trace_id,
        "inbound",
        "vapi",
        workflow,
        "authenticated",
        http_status=200,
    )
    trace.log("request_parsed", tool_call_id=parsed.tool_call_id or "flat")
    return trace, parsed
