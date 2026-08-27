from fastapi.testclient import TestClient

from rpt_agent.mock_server import app
from rpt_agent.security import sign_handoff

client = TestClient(app)


def setup_function():
    client.post("/mock/reset")


def test_stride_happy_path_and_duplicate():
    patient = {
        "first_name": "Synthetic", "last_name": "Person", "date_of_birth": "1990-01-01",
        "contact_info": {"mobile_phone_number": "5555551212"}, "primary_address": {},
    }
    first = client.post("/mock/stride/v1/patients/", json=patient)
    assert first.status_code == 200 and first.json()["id"]
    duplicate = client.post("/mock/stride/v1/patients/", json=patient)
    assert duplicate.status_code == 400
    case = client.post("/mock/stride/v1/cases/", json={"patient_id": first.json()["id"], "title": "PT"})
    availability = client.get("/mock/stride/v1/scheduling/availabilities/", params={
        "location": 3, "duration": 60, "clinician_ids": "42",
        "start_date": "2026-08-24", "end_date": "2026-08-25",
    })
    assert case.status_code == 200
    assert availability.status_code == 200
    assert availability.json()[0]["clinician_id"] == 42


def test_scenarios_and_provider_ids():
    created = client.post("/mock/vapi/calls", json={}, headers={"X-Trace-ID": "trace-x"})
    assert created.status_code == 201
    assert created.json()["id"].startswith("mock-call-")
    malformed = client.post("/mock/vapi/calls", json={}, headers={"X-Mock-Scenario": "malformed"})
    assert "id" not in malformed.json()
    rate = client.post("/mock/twilio/messages", headers={"X-Mock-Scenario": "rate_limit"})
    assert rate.status_code == 429
    tool = client.get("/mock/vapi/tool-call/booked").json()
    assert tool["message"]["toolCallList"][0]["arguments"]["outcome"] == "booked"
    report = client.get("/mock/vapi/end-of-call/voicemail").json()
    assert report["message"]["type"] == "end-of-call-report"


def test_keap_receiver_deduplicates_event_id():
    payload = {"event_id": "event-1", "event_type": "appointment.booked.v1"}
    import json

    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-RPT-Signature": sign_handoff(body)}
    assert client.post("/mock/keap/events", content=body, headers=headers).json()["duplicate"] is False
    assert client.post("/mock/keap/events", content=body, headers=headers).json()["duplicate"] is True
