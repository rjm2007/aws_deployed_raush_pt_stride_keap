from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from datetime import date, datetime, timedelta
from itertools import count
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .observability import WorkflowTrace, configure_logging

configure_logging("mock-provider")
app = FastAPI(title="RPT Deterministic Provider Mocks", version="0.1.0")
_ids = count(int(time.time()))  # restart-safe: never re-issues an id a previous run stored
_lock = threading.Lock()
_patients: dict[tuple[str, str, str], int] = {}
_cases: dict[int, int] = {}
_appointments: dict[tuple[int, str], int] = {}
_handoffs: dict[str, dict[str, Any]] = {}


def scenario(request: Request) -> str:
    return request.headers.get("x-mock-scenario", "success")


async def apply_scenario(value: str) -> None:
    if value in {"delay", "timeout"}:
        await asyncio.sleep(15 if value == "timeout" else 1)
    if value in {"provider_error", "failure"}:
        raise HTTPException(status_code=503, detail="mock provider error")
    if value == "rate_limit":
        raise HTTPException(status_code=429, detail="mock rate limit")


@app.get("/health")
def health():
    return {"status": "ok", "service": "mock-provider"}


@app.post("/mock/vapi/calls", status_code=201)
async def create_call(request: Request):
    trace = WorkflowTrace("mock_vapi_call", "mock-provider", request.headers.get("x-trace-id", ""))
    selected = scenario(request)
    trace.log("mock_scenario_selected", scenario=selected)
    await apply_scenario(selected)
    if selected == "malformed":
        return {"unexpected": True}
    call_id = f"mock-call-{next(_ids)}"
    trace.complete(call_id=call_id)
    return {"id": call_id, "status": "queued"}


@app.get("/mock/vapi/tool-call/{outcome}")
def generated_tool_call(outcome: str, lead_id: str = "synthetic-lead", event_id: int = 1):
    allowed = {"booked", "no_answer", "voicemail", "declined", "callback", "malformed"}
    if outcome not in allowed:
        raise HTTPException(status_code=400, detail="unknown mock outcome")
    if outcome == "malformed":
        return {"message": {"toolCallList": [{"unexpected": True}]}}
    mapped = "not_interested" if outcome == "declined" else outcome
    return {"message": {"type": "tool-calls", "toolCallList": [{
        "id": f"mock-tool-{next(_ids)}", "name": "record_call_outcome",
        "arguments": {"lead_id": lead_id, "outreach_event_id": event_id, "outcome": mapped},
    }]}}


@app.get("/mock/vapi/end-of-call/{outcome}")
def generated_end_of_call(outcome: str, lead_id: str = "synthetic-lead", event_id: int = 1):
    reasons = {
        "booked": "assistant-ended-call", "no_answer": "customer-did-not-answer",
        "voicemail": "voicemail", "declined": "customer-ended-call", "callback": "assistant-ended-call",
    }
    if outcome not in reasons:
        raise HTTPException(status_code=400, detail="unknown mock outcome")
    now = datetime.now().astimezone()
    return {"message": {"type": "end-of-call-report", "endedReason": reasons[outcome],
        "startedAt": (now - timedelta(seconds=30)).isoformat(), "endedAt": now.isoformat(),
        "call": {"id": f"mock-call-{next(_ids)}", "assistantOverrides": {"variableValues": {
            "lead_id": lead_id, "outreach_event_id": str(event_id)
        }}}}}


@app.post("/mock/twilio/messages", status_code=201)
async def create_message(request: Request):
    trace = WorkflowTrace("mock_twilio_message", "mock-provider", request.headers.get("x-trace-id", ""))
    selected = scenario(request)
    trace.log("mock_scenario_selected", scenario=selected)
    await apply_scenario(selected)
    if selected == "malformed":
        return {"unexpected": True}
    sid = f"SM{next(_ids):032d}"
    trace.complete(provider_message_id=sid)
    return {"sid": sid, "status": "queued"}


@app.post("/mock/stride/v1/patients/")
async def create_patient(request: Request):
    await apply_scenario(scenario(request))
    data = await request.json()
    required = ("first_name", "last_name", "date_of_birth", "contact_info", "primary_address")
    if not all(key in data for key in required):
        raise HTTPException(status_code=400, detail="Malformed request")
    key = (data["first_name"].strip().lower(), data["last_name"].strip().lower(), data["date_of_birth"])
    with _lock:
        if key in _patients or scenario(request) == "duplicate_patient":
            raise HTTPException(status_code=400, detail="Patient already exists")
        patient_id = next(_ids)
        _patients[key] = patient_id
    return {"id": patient_id}


@app.post("/mock/stride/v1/cases/")
async def create_case(request: Request):
    await apply_scenario(scenario(request))
    data = await request.json()
    if not data.get("patient_id") or not data.get("title"):
        raise HTTPException(status_code=400, detail="Patient does not exist")
    with _lock:
        case_id = next(_ids)
        _cases[case_id] = int(data["patient_id"])
    return {"id": case_id}


@app.get("/mock/stride/v1/scheduling/availabilities/")
async def availability(
    request: Request, location: int, duration: int, clinician_ids: str,
    start_date: date, end_date: date,
):
    await apply_scenario(scenario(request))
    if (end_date - start_date).days > 31:
        raise HTTPException(status_code=400, detail="Date-range error")
    if scenario(request) == "unavailable_slot":
        return []
    results = []
    for clinician in clinician_ids.split(","):
        current = start_date
        row: dict[str, Any] = {"timezone": "America/Los_Angeles", "clinician_id": int(clinician)}
        while current <= end_date:
            if current.weekday() < 5:
                row[current.isoformat()] = ["09:00:00", "10:30:00", "13:30:00"]
            current += timedelta(days=1)
        results.append(row)
    return results


@app.post("/mock/stride/v1/appointments/")
async def create_appointment(request: Request):
    await apply_scenario(scenario(request))
    data = await request.json()
    required = {"case_id", "primary_attendee", "location", "appointment_type",
                "start_date_utc", "end_date_utc"}
    if not required.issubset(data):
        raise HTTPException(status_code=400, detail="Invalid formatting")
    try:
        datetime.fromisoformat(data["start_date_utc"])
        datetime.fromisoformat(data["end_date_utc"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid formatting") from exc
    key = (int(data["case_id"]), data["start_date_utc"])
    with _lock:
        if key in _appointments or scenario(request) in {"overlap", "duplicate_appointment"}:
            raise HTTPException(status_code=400, detail="Appointment Date+Case combination already exists")
        appointment_id = next(_ids)
        _appointments[key] = appointment_id
    return {"id": appointment_id}


@app.post("/mock/keap/events")
async def keap_handoff(request: Request):
    await apply_scenario(scenario(request))
    body = await request.body()
    supplied = request.headers.get("x-rpt-signature", "")
    expected = hmac.new(get_settings().keap_handoff_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid handoff signature")
    payload = json.loads(body)
    event_id = payload.get("event_id")
    if not event_id:
        raise HTTPException(status_code=400, detail="missing event_id")
    duplicate = event_id in _handoffs
    _handoffs[event_id] = payload
    return JSONResponse({"accepted": True, "duplicate": duplicate}, status_code=200)


@app.get("/mock/twilio/inbound/{command}")
def generated_twilio_inbound(command: str, phone: str = "+15555550123"):
    if command.lower() not in {"stop", "call", "hello"}:
        raise HTTPException(status_code=400, detail="unknown inbound scenario")
    return {"From": phone, "Body": command.upper(), "MessageSid": f"SM{next(_ids):032d}"}


@app.get("/mock/twilio/status/{status}")
def generated_twilio_status(status: str, sid: str = "SM00000000000000000000000000000001"):
    if status not in {"queued", "sent", "delivered", "undelivered", "failed"}:
        raise HTTPException(status_code=400, detail="unknown delivery status")
    return {"MessageSid": sid, "MessageStatus": status}


@app.get("/mock/keap/events")
def list_handoffs():
    return {"events": list(_handoffs.values())}


@app.post("/mock/reset")
def reset():
    with _lock:
        _patients.clear()
        _cases.clear()
        _appointments.clear()
        _handoffs.clear()
    return {"ok": True}
