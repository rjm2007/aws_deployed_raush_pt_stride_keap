"""Test the booking voice agent with one real call.

    uv run --frozen python scripts/test_booking_call.py --phone +919876543210 --name "Dev Chaudhary"
    uv run --frozen python scripts/test_booking_call.py --phone +919876543210 --name "Dev Chaudhary" \\
        --clinic "Dana Point" --keep

1. Creates one test lead in the database (is_test, cadence paused, texts off), so nothing else
   ever calls or texts it.
2. Asks Vapi to call --phone right now with the NEW booking assistant.
3. Prints the transcript, tool calls and the booking result when the call ends.
4. Deletes the test lead again (unless --keep), so it is on the dashboard only during the test.

Runs against the database and Vapi in .env; uses the live booking tools. Never touches the old
outreach assistant or any real lead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
NEW_ASSISTANT = json.loads((ROOT / "config" / "vapi_booking_tools.json").read_text())["assistant_id"]
VAPI = "https://api.vapi.ai"
CLINICS = ["Laguna Niguel", "Dana Point", "Mission Viejo"]


def vapi(settings, method: str, path: str, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {settings.vapi_api_key}"}
    for attempt in range(4):  # Vapi returns the odd transient 5xx
        response = httpx.request(method, f"{VAPI}{path}", headers=headers, timeout=30, **kwargs)
        if response.status_code < 500:
            response.raise_for_status()
            return response.json()
        time.sleep(2 * (attempt + 1))
    response.raise_for_status()
    return {}


def create_test_lead(transaction, args) -> tuple[str, int, str]:
    first, _, last = args.name.strip().partition(" ")
    with transaction() as conn:
        settings = conn.execute(
            "select p.id,ps.vapi_phone_number_id from practices p "
            "join practice_settings ps on ps.practice_id=p.id where p.slug='rausch-pt'"
        ).fetchone()
        lead = conn.execute(
            "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
            "date_of_birth,status,cadence_state,is_test,sms_opt_out,location,lead_type,status_reason) "
            "values(%s,'dashboard',%s,%s,%s,%s,%s,'in_progress','paused',true,true,%s,%s,"
            "'booking agent test call') returning id",
            (settings["id"], args.name.strip(), first, last or first, args.phone, args.dob,
             args.clinic, args.case),
        ).fetchone()["id"]
        # The call's own outreach step, already in flight: the end-of-call report settles it like a
        # cadence call, and the worker never claims it.
        event = conn.execute(
            "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for,day_offset) "
            "values(%s,1,'call','in_flight',now(),0) returning id",
            (lead,),
        ).fetchone()["id"]
    return str(lead), event, settings["vapi_phone_number_id"]


def delete_test_lead(transaction, lead_id: str) -> None:
    """Same clean-up as the dashboard's Delete (rows that do not cascade go first)."""
    with transaction() as conn:
        for table in ("appointments", "dashboard_sms_requests", "sms_messages", "notification_log",
                      "outreach_events"):
            conn.execute(f"delete from {table} where lead_id=%s", (lead_id,))
        conn.execute("delete from integration_outbox where aggregate_id=%s", (lead_id,))
        conn.execute("delete from leads where id=%s and is_test", (lead_id,))


def print_transcript(call: dict) -> None:
    artifact = call.get("artifact") or {}
    for message in artifact.get("messages") or call.get("messages") or []:
        role = message.get("role")
        if role == "system":
            continue
        at = f"{message.get('secondsFromStart', 0):6.1f}s"
        if message.get("toolCalls"):
            for tool_call in message["toolCalls"]:
                function = tool_call.get("function", {})
                print(f"{at}  TOOL   {function.get('name')} {function.get('arguments')}")
        elif role in {"tool_call_result", "tool"}:
            print(f"{at}  RESULT {str(message.get('result') or message.get('content'))[:220]}")
        else:
            print(f"{at}  {role.upper():6} {message.get('message') or message.get('content')}")
    print(f"\nCall ended: {call.get('endedReason')}")


def vapi_error(error: httpx.HTTPStatusError) -> str:
    try:
        message = error.response.json().get("message")
    except ValueError:
        message = error.response.text[:200]
    return f"Vapi refused the call (HTTP {error.response.status_code}): {message}"


def print_outcome(transaction, lead_id: str) -> None:
    with transaction() as conn:
        lead = conn.execute("select status,needs_review,review_reason from leads where id=%s",
                            (lead_id,)).fetchone()
        appointment = conn.execute(
            "select a.state,a.stride_appointment_id,a.start_utc,c.display_name,bl.name as clinic "
            "from appointments a left join clinicians c on c.stride_user_id=a.clinician_id "
            "left join booking_locations bl on bl.id=a.booking_location_id "
            "where a.lead_id=%s order by a.id desc limit 1",
            (lead_id,),
        ).fetchone()
    print(f"Lead result: {lead['status']}"
          + (f" (needs review: {lead['review_reason']})" if lead["needs_review"] else ""))
    if appointment:
        print(f"Appointment: {appointment['state']} | Stride id {appointment['stride_appointment_id']} | "
              f"{appointment['display_name']} at {appointment['clinic']} | {appointment['start_utc']}")
    else:
        print("Appointment: none")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the booking voice agent with one real call.")
    parser.add_argument("--phone", required=True, help="E.164, for example +919876543210")
    parser.add_argument("--name", required=True, help='First and last name, e.g. "Dev Chaudhary"')
    parser.add_argument("--clinic", default="Laguna Niguel", choices=CLINICS)
    parser.add_argument("--case", default="Physical Therapy")
    parser.add_argument("--dob", default="1990-01-15", help="YYYY-MM-DD, stored on the lead")
    parser.add_argument("--keep", action="store_true", help="keep the test lead afterwards")
    args = parser.parse_args()
    if not re.fullmatch(r"\+\d{8,15}", args.phone):
        sys.exit("--phone must be in E.164 form, for example +919876543210")

    from rpt_agent.config import get_settings
    from rpt_agent.db import transaction
    from rpt_agent.services.slot_booking import call_case_variables

    settings = get_settings()
    lead_id, event_id, phone_number_id = create_test_lead(transaction, args)
    with transaction() as conn:
        case_variables = call_case_variables(conn, lead_id)
    print(f"Test lead {lead_id} created.")
    try:
        call = vapi(settings, "POST", "/call", json={
            "assistantId": NEW_ASSISTANT,
            "phoneNumberId": phone_number_id or settings.vapi_phone_number_id,
            "customer": {"number": args.phone},
            "assistantOverrides": {"variableValues": {
                "lead_id": lead_id, "outreach_event_id": str(event_id),
                "Patient_name": args.name, "patient_name": args.name, "call_attempt": "1",
                "clinic_location": args.clinic, "case_name": args.case, **case_variables,
            }},
        })
        with transaction() as conn:
            conn.execute(
                "update outreach_events set status='attempted',executed_at=now(),provider='vapi',"
                "provider_ref=%s,vapi_call_id=%s where id=%s",
                (call["id"], call["id"], event_id),
            )
        print(f"Calling {args.phone} now with the booking agent. Answer the phone...")
        deadline = time.time() + 20 * 60
        while time.time() < deadline and call.get("status") != "ended":
            time.sleep(4)
            call = vapi(settings, "GET", f"/call/{call['id']}")
        time.sleep(8)  # let the end-of-call report reach the backend
        print("\n=== Transcript ===")
        print_transcript(call)
        print()
        print_outcome(transaction, lead_id)
    except httpx.HTTPStatusError as error:
        print(vapi_error(error))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if args.keep:
            print(f"\nKept test lead {lead_id}.")
        else:
            delete_test_lead(transaction, lead_id)
            print("\nTest lead deleted.")


if __name__ == "__main__":
    main()
