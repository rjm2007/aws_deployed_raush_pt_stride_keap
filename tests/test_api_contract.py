import json

from fastapi.testclient import TestClient

import rpt_agent.api as api_module
import rpt_agent.routes.appointments as appointment_routes
import rpt_agent.routes.availability as availability_routes
import rpt_agent.routes.leads as lead_routes
import rpt_agent.routes.tool_request as tool_request_routes
import rpt_agent.routes.vapi as vapi_routes
from rpt_agent.config import get_settings


class FakeBookingService:
    def availability(self, trace, lead_id, start, days=7):
        trace.log("fake_availability", lead_id=lead_id)
        return {"status": "ok", "slots": []}

    def book(self, trace, lead_id, event_id, slot_token, patient_data=None):
        return {"status": "confirmed", "appointment_id": 123}


class FakeDirectBookingService:
    seen = None

    def availability_message(self, trace, **kwargs):
        self.__class__.seen = kwargs
        return "I have 9:00 AM and 10:00 AM open. Which works better?"

    def book_at(self, trace, **kwargs):
        self.__class__.seen = kwargs
        return {
            "status": "confirmed",
            "appointment_id": 123,
            "spoken_confirmation": "Your appointment is confirmed for Thursday at 9:00 AM.",
        }


def test_vapi_tools_require_auth(monkeypatch):
    client = TestClient(api_module.app)
    response = client.post("/api/v1/vapi/tools", json={})
    assert response.status_code == 401


def test_vapi_results_preserve_order_and_business_errors_use_200(monkeypatch):
    monkeypatch.setattr(vapi_routes, "BookingService", FakeBookingService)
    client = TestClient(api_module.app)
    payload = {"message": {"toolCallList": [
        {"id": "one", "name": "availability", "arguments": {"lead_id": "lead-1"}},
        {"id": "two", "name": "unknown", "arguments": {}},
    ]}}
    response = client.post(
        "/api/v1/vapi/tools", json=payload,
        headers={
            "Authorization": f"Bearer {get_settings().vapi_webhook_secret}",
            "X-Trace-ID": "trace-api",
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["toolCallId"] for item in results] == ["one", "two"]
    assert json.loads(results[0]["result"])["status"] == "ok"
    assert "error" in results[1]
    assert "\n" not in results[1]["error"]
    assert response.headers["X-Trace-ID"] == "trace-api"


def test_direct_availability_accepts_flat_body_secret(monkeypatch):
    monkeypatch.setattr(availability_routes, "BookingService", FakeDirectBookingService)
    monkeypatch.setattr(tool_request_routes, "record_integration_event", lambda *args, **kwargs: None)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/v1/tools/check-availability",
        json={
            "lead_id": "lead-1",
            "date": "2026-08-27",
            "time": "9 AM",
            "vapi_secret": get_settings().vapi_webhook_secret,
        },
    )
    assert response.status_code == 200
    assert response.json()["message"].endswith("Which works better?")
    assert FakeDirectBookingService.seen["lead_id"] == "lead-1"


def test_direct_appointment_uses_trusted_transport_lead_and_vapi_envelope(monkeypatch):
    monkeypatch.setattr(appointment_routes, "BookingService", FakeDirectBookingService)
    monkeypatch.setattr(tool_request_routes, "record_integration_event", lambda *args, **kwargs: None)
    client = TestClient(api_module.app)
    payload = {"message": {
        "call": {"id": "call-1", "assistantOverrides": {"variableValues": {
            "lead_id": "trusted-lead", "outreach_event_id": "42",
        }}},
        "toolCallList": [{
            "id": "tool-1",
            "name": "create_appointment",
            "arguments": {
                "lead_id": "model-lead", "date": "2026-08-27", "time": "9 AM",
            },
        }],
    }}
    response = client.post(
        "/api/v1/tools/create-appointment",
        json=payload,
        headers={"Authorization": f"Bearer {get_settings().vapi_webhook_secret}"},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["toolCallId"] == "tool-1"
    assert FakeDirectBookingService.seen["lead_id"] == "trusted-lead"
    assert FakeDirectBookingService.seen["event_id"] == 42


def test_direct_lead_status_threads_call_id_and_status(monkeypatch):
    seen = {}

    def fake_report(trace, **kwargs):
        seen.update(kwargs)
        return "recorded"

    monkeypatch.setattr(lead_routes, "report_lead_status", fake_report)
    monkeypatch.setattr(tool_request_routes, "record_integration_event", lambda *args, **kwargs: None)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/v1/webhooks/vapi/lead-status",
        json={"message": {
            "call": {"id": "call-1", "assistantOverrides": {"variableValues": {
                "lead_id": "lead-1", "outreach_event_id": "7",
            }}},
            "toolCallList": [{
                "id": "tool-status",
                "name": "update_lead_status",
                "arguments": {"status": "declined", "notes": "declined scheduling"},
            }],
        }},
        headers={"X-Vapi-Secret": get_settings().vapi_webhook_secret},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["toolCallId"] == "tool-status"
    assert seen["call_id"] == "call-1"
    assert seen["event_id"] == 7
    assert seen["status"] == "declined"
