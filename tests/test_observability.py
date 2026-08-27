import json
import logging

from rpt_agent.observability import JsonFormatter, WorkflowTrace, redact


def test_redaction_removes_phi_and_secrets():
    value = redact({
        "authorization": "Bearer top-secret",
        "phone": "+1 555 555 1212",
        "email": "patient@example.com",
        "safe": "call +1 (555) 555-9999 or a@b.com",
        "nested": {"date_of_birth": "1990-01-01"},
    })
    encoded = json.dumps(value)
    assert "top-secret" not in encoded
    assert "555" not in encoded
    assert "patient@example.com" not in encoded
    assert "1990-01-01" not in encoded


def test_redaction_keeps_safe_uuid_and_workflow_date():
    value = redact({
        "lead_id": "2e992600-1ea0-43f3-b4db-297d33cdd4fa",
        "start_date": "2026-08-26",
        "note": "call +1 555 555 1212",
    })
    assert value["lead_id"] == "2e992600-1ea0-43f3-b4db-297d33cdd4fa"
    assert value["start_date"] == "2026-08-26"
    assert value["note"] == "call [REDACTED_PHONE]"


def test_json_formatter_has_trace_and_ordered_steps(caplog):
    trace = WorkflowTrace("test_flow", "test", "trace-123")
    with caplog.at_level(logging.INFO):
        trace.log("validation_passed", lead_id="safe-id")
        trace.complete()
    records = [record for record in caplog.records if getattr(record, "trace_id", "") == "trace-123"]
    assert [record.details["step"] for record in records] == sorted(record.details["step"] for record in records)
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(records[-1]))
    assert payload["trace_id"] == "trace-123"
    assert payload["workflow"] == "test_flow"
