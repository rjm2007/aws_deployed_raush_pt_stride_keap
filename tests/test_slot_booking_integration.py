"""Voice-agent booking (find-slots / book-appointment) against a real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied,
never production). Stride is faked so every outcome can be forced: slot gone,
overlap, duplicate patient, unclear timeout, one-evaluation-per-case, slow calls.
Each test builds its own practice, clinic, case type, clinicians and 555 lead.
"""

import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderError, Slot
from rpt_agent.services import availability_sync, slot_booking
from rpt_agent.services.availability_sync import run_sync

pytestmark = pytest.mark.integration

TODAY = date(2026, 6, 15)  # pinned demo "today"; demo slots exist May-July 2026
DAY1, DAY2, DAY3 = "2026-06-16", "2026-06-17", "2026-06-22"
PATIENT = {"first_name": "Rpt", "last_name": "Tester", "date_of_birth": "1990-04-26"}


class FakeStride:
    """Stride stand-in. availability() reads `open`; create() follows a script per resource."""

    def __init__(self):
        self.open: dict[int, dict[str, list[str]]] = {}
        self.script: dict[str, list] = {"patients": [], "cases": [], "appointments": []}
        self.calls: list[tuple[str, object]] = []
        # Unique per test: the tests share one database, and Stride ids are unique.
        self.next_id = 10**9 + uuid4().int % 10**9 * 1000
        self.availability_error: ProviderError | None = None

    def stride_availability(self, trace, *, location, duration, clinician_ids, start_date,
                            end_date):
        self.calls.append(("availability", (location, duration, clinician_ids, start_date,
                                            end_date)))
        if self.availability_error:
            raise self.availability_error
        wanted = {int(value) for value in str(clinician_ids).split(",")}
        slots = []
        for clinician, days in self.open.items():
            if clinician not in wanted:
                continue
            for day, times in days.items():
                if start_date.isoformat() <= day <= end_date.isoformat():
                    slots.extend(Slot(clinician, "US/Eastern", day, value) for value in times)
        return slots

    def stride_create(self, trace, resource, payload):
        self.calls.append((resource, payload))
        planned = self.script[resource].pop(0) if self.script[resource] else None
        if isinstance(planned, Exception):
            raise planned
        self.next_id += 1
        return planned or self.next_id

    def count(self, kind):
        return sum(1 for name, _ in self.calls if name == kind)


def stride_error(code, detail, *, ambiguous=False):
    return ProviderError("stride", code, detail, ambiguous=ambiguous)


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    for key, value in {
        "SUPABASE_DB_URL": url,
        "BOOKING_TODAY_OVERRIDE": TODAY.isoformat(),
        "BOOKING_MIN_NOTICE_MINUTES": "0",
        "VAPI_WEBHOOK_SECRET": "test-vapi-secret",
        "SHEET_SYNC_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    # A sync makes one Stride request per 31-day chunk; skip the rate-limit pause between them.
    monkeypatch.setattr(availability_sync, "STRIDE_REQUEST_GAP_SECONDS", 0)
    get_settings.cache_clear()
    get_pool.cache_clear()
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    slug = f"test-booking-{uuid4().hex[:10]}"
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic Practice',%s,"
        "'America/Los_Angeles') returning id", (slug,),
    ).fetchone()["id"]
    conn.execute(
        "insert into practice_settings(practice_id,stride_location_timezone,stride_booking_enabled) "
        "values(%s,'America/New_York',true)", (practice,),
    )
    location = conn.execute(
        "insert into booking_locations(practice_id,name,stride_location_id,timezone) "
        "values(%s,'Laguna Niguel',3170,'America/New_York') returning id", (practice,),
    ).fetchone()["id"]
    case_type = conn.execute(
        "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes,"
        "is_default) values(%s,'Physical Therapy',1452,60,true) returning id", (practice,),
    ).fetchone()["id"]
    clinicians = {}
    for stride_id, name in ((5981, "Thao Nguyen"), (5982, "Alan Rome")):
        clinicians[stride_id] = conn.execute(
            "insert into clinicians(practice_id,stride_user_id,display_name) values(%s,%s,%s) "
            "returning id", (practice, stride_id, name),
        ).fetchone()["id"]
        conn.execute(
            "insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
            "values(%s,%s,%s)", (clinicians[stride_id], location, case_type),
        )

    class Env:
        db = conn
        practice_id = practice
        stride = FakeStride()
        trace = WorkflowTrace("test_booking", "test")

        def settings(self, **update):
            return get_settings().model_copy(update=update)

        def lead(self, *, location_name="Laguna Niguel", lead_type="knee", is_test=True):
            return str(conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,"
                "phone_e164,date_of_birth,status,cadence_state,is_test,location,lead_type) "
                "values(%s,'dashboard','Rpt Tester','Rpt','Tester',%s,'1990-04-26',"
                "'in_progress','active',%s,%s,%s) returning id",
                (practice, f"+1555{uuid4().int % 10**7:07d}", is_test, location_name, lead_type),
            ).fetchone()["id"])

        def sync(self, **update):
            return run_sync(self.trace, providers=self.stride, settings=self.settings(**update),
                            practice_id=practice)

        def find(self, lead_id, call_id="call-1", **kwargs):
            kwargs.setdefault("when", "earliest")
            return slot_booking.find_slots(self.trace, lead_id=lead_id, call_id=call_id,
                                           providers=self.stride,
                                           settings=self.settings(), **kwargs)

        def book(self, lead_id, call_id="call-1", settings=None, **arguments):
            return slot_booking.book_slot(self.trace, lead_id=lead_id, call_id=call_id,
                                          arguments={**PATIENT, **arguments},
                                          providers=self.stride,
                                          settings=settings or self.settings())

        def one(self, sql, *args):
            return conn.execute(sql, args).fetchone()

    environment = Env()
    environment.location_id = location
    environment.clinicians = clinicians
    yield environment
    conn.close()
    get_settings.cache_clear()
    get_pool.cache_clear()


def open_days(env, **per_clinician):
    env.stride.open = {int(k[1:]): v for k, v in per_clinician.items()}


# --------------------------------------------------------------------------- sync

def test_sync_caches_slots_and_marks_vanished_ones_gone(env):
    open_days(env, c5981={DAY1: ["09:00:00", "10:00:00"]}, c5982={DAY1: ["09:00:00"]})
    result = env.sync()
    assert result["errors"] == 0 and result["slots"] == 3
    open_days(env, c5981={DAY1: ["10:00:00"]}, c5982={DAY1: ["09:00:00"]})
    env.sync()
    statuses = {
        (row["clinician_id"], str(row["local_time"])): row["status"]
        for row in env.db.execute(
            "select clinician_id,local_time,status from availability_slots "
            "where booking_location_id=%s", (env.location_id,),
        ).fetchall()
    }
    thao, alan = env.clinicians[5981], env.clinicians[5982]
    assert statuses[(thao, "09:00:00")] == "gone"
    assert statuses[(thao, "10:00:00")] == "open"
    assert statuses[(alan, "09:00:00")] == "open"
    # One Stride request per location + visit length, all clinicians together.
    request = next(args for name, args in env.stride.calls if name == "availability")
    assert request[0] == 3170 and request[1] == 60 and request[2] == "5981,5982"
    assert request[3] == TODAY


def test_sync_failure_is_recorded_and_does_not_wipe_cache(env):
    open_days(env, c5981={DAY1: ["09:00:00"]})
    env.sync()
    env.stride.availability_error = stride_error("503", "stride returned HTTP 503")
    assert env.sync()["errors"] == 1
    state = env.one("select last_error,slot_count from availability_sync_state "
                    "where booking_location_id=%s and chunk=0", env.location_id)
    assert state["last_error"] and state["slot_count"] == 1
    assert env.one("select count(*) n from availability_slots where booking_location_id=%s "
                   "and status='open'", env.location_id)["n"] == 1


# --------------------------------------------------------------------------- find

def test_find_offers_three_nearest_distinct_times_and_remembers_them(env):
    open_days(env, c5981={DAY1: ["09:00:00", "10:00:00", "11:00:00"], DAY2: ["08:00:00"]},
              c5982={DAY1: ["09:00:00"]})
    env.sync()
    lead = env.lead()
    message = env.find(lead)
    assert message.startswith("OPENINGS for Physical Therapy at Laguna Niguel:")
    assert "Tuesday, June 16 (2026-06-16) at 9:00 AM" in message
    assert "10:00 AM" in message and "11:00 AM" in message
    assert "June 17" not in message  # only the three nearest
    assert message.count("9:00 AM") == 1  # same time with two clinicians is offered once
    assert env.one("select count(*) n from slot_offers where call_id='call-1' and lead_id=%s",
                   lead)["n"] == 3


def test_find_by_therapist_and_unknown_therapist(env):
    open_days(env, c5981={DAY1: ["09:00:00"]}, c5982={DAY1: ["13:00:00"]})
    env.sync()
    lead = env.lead()
    message = env.find(lead, clinician_name="Dr. Rome")
    assert "with Alan Rome" in message and "Thao" not in message
    unknown = env.find(lead, clinician_name="Smith")
    assert unknown.startswith("NO_SUCH_THERAPIST") and "Alan Rome, Thao Nguyen" in unknown


def test_find_windows_time_of_day_and_fallback(env):
    open_days(env, c5981={DAY1: ["09:00:00", "14:00:00"], DAY3: ["10:00:00"]})
    env.sync()
    lead = env.lead()
    afternoon = env.find(lead, when="tomorrow", time_of_day="afternoon")
    assert "2:00 PM" in afternoon and "9:00 AM" not in afternoon
    next_week = env.find(lead, when="next_week")  # TODAY is Monday 15 June -> 22-28 June
    assert next_week.startswith("OPENINGS") and "June 22" in next_week and "June 16" not in next_week
    empty_day = env.find(lead, when="specific_date", specific_date=date(2026, 6, 18))
    assert empty_day.startswith("NOTHING_IN_RANGE") and "June 22" in empty_day
    with pytest.raises(ValueError, match="PAST_DATE"):
        env.find(lead, when="specific_date", specific_date=date(2026, 6, 1))


def test_find_uses_location_said_on_the_call_and_rejects_unknown_clinic(env):
    lead = env.lead(location_name="Dana Point")
    with pytest.raises(ValueError, match="UNKNOWN_LOCATION.*Laguna Niguel"):
        env.find(lead)
    open_days(env, c5981={DAY1: ["09:00:00"]})
    env.sync()
    assert env.find(lead, location_name="laguna niguel").startswith("OPENINGS")


def test_find_refreshes_stale_cache_inline(env):
    open_days(env, c5981={DAY1: ["09:00:00"]})
    lead = env.lead()  # no sync has run yet
    assert env.find(lead).startswith("OPENINGS")
    assert env.stride.count("availability") == 1


def test_find_reports_system_down_when_stride_fails_and_cache_is_empty(env):
    env.stride.availability_error = stride_error("503", "stride returned HTTP 503")
    with pytest.raises(ProviderError):
        env.find(env.lead())


# --------------------------------------------------------------------------- book

def booked_setup(env):
    open_days(env, c5981={DAY1: ["09:00:00", "10:00:00"], DAY2: ["08:00:00"]})
    env.sync()
    lead = env.lead()
    env.find(lead)
    return lead


def test_book_happy_path_creates_patient_case_appointment(env):
    lead = booked_setup(env)
    message = env.book(lead, date=DAY1, time="9 AM", email="Rpt@Example.com")
    assert message.startswith("BOOKED: Tuesday, June 16 at 9:00 AM with Thao Nguyen")
    patient = next(p for name, p in env.stride.calls if name == "patients")
    assert patient["date_of_birth"] == "1990-04-26"
    assert patient["primary_address"]["address_1"] == "Address not provided"
    assert patient["contact_info"]["mobile_phone_number"].startswith("555")
    assert patient["contact_info"]["personal_email"] == "rpt@example.com"
    case = next(p for name, p in env.stride.calls if name == "cases")
    assert case["title"] == "Knee"
    appointment = next(p for name, p in env.stride.calls if name == "appointments")
    assert appointment["appointment_type"] == 1452 and appointment["location"] == 3170
    assert appointment["primary_attendee"] == 5981 and appointment["is_pending"] is True
    assert appointment["start_date_utc"] == "2026-06-16T13:00:00+00:00"  # 9 AM Eastern
    assert appointment["end_date_utc"] == "2026-06-16T14:00:00+00:00"
    row = env.one("select status,cadence_state,stride_patient_id,stride_case_id from leads "
                  "where id=%s", lead)
    assert row["status"] == "booked" and row["cadence_state"] == "completed"
    assert row["stride_patient_id"] and row["stride_case_id"]
    appt = env.one("select state,stride_appointment_id,stride_case_id from appointments "
                   "where lead_id=%s", lead)
    assert appt["state"] == "scheduled" and appt["stride_case_id"] == row["stride_case_id"]
    assert env.one("select status from availability_slots s join appointments a on a.slot_id=s.id "
                   "where a.lead_id=%s", lead)["status"] == "booked"
    assert env.one("select status from slot_holds where lead_id=%s", lead)["status"] == "booked"
    assert env.one("select count(*) n from notification_log where lead_id=%s "
                   "and notification_type='sms_appointment_booked'", lead)["n"] == 1
    # Test leads never reach Keap.
    assert env.one("select count(*) n from integration_outbox where destination='keap' "
                   "and payload->>'lead_id'=%s", lead)["n"] == 0
    # The booked slot is no longer offered to anyone.
    assert "9:00 AM" not in env.find(env.lead(), call_id="call-2")


def test_book_real_lead_queues_keap_handoff(env):
    open_days(env, c5981={DAY1: ["09:00:00"]})
    env.sync()
    lead = env.lead(is_test=False)
    env.find(lead)
    assert env.book(lead, date=DAY1, time="09:00").startswith("BOOKED")
    assert env.one("select count(*) n from integration_outbox where destination='keap' "
                   "and payload->>'lead_id'=%s", lead)["n"] == 1


def test_book_twice_reports_existing_booking_without_calling_stride(env):
    lead = booked_setup(env)
    env.book(lead, date=DAY1, time="9:00 AM")
    calls = len(env.stride.calls)
    again = env.book(lead, date=DAY2, time="8:00 AM")
    assert again.startswith("ALREADY_BOOKED") and "June 16 at 9:00 AM" in again
    assert len(env.stride.calls) == calls


def test_book_time_not_offered_returns_alternatives(env):
    lead = booked_setup(env)
    message = env.book(lead, date=DAY1, time="3 PM")
    assert message.startswith("NOT_AVAILABLE") and "Other openings" in message
    assert env.stride.count("patients") == 0


def test_book_slot_gone_on_live_check(env):
    lead = booked_setup(env)
    open_days(env, c5981={DAY1: ["10:00:00"], DAY2: ["08:00:00"]})  # 9 AM taken in Stride
    message = env.book(lead, date=DAY1, time="9:00 AM")
    assert message.startswith("TAKEN") and "10:00 AM" in message
    assert env.stride.count("patients") == 0
    assert env.one("select state from appointments where lead_id=%s", lead)["state"] == "failed"
    assert env.one("select status from slot_holds where lead_id=%s", lead)["status"] == "released"
    assert env.one("select status from availability_slots where booking_location_id=%s and "
                   "local_date=%s and local_time='09:00'", env.location_id, DAY1)["status"] == "gone"
    # The patient picks another time: same lead books fine.
    assert env.book(lead, date=DAY1, time="10:00 AM").startswith("BOOKED")


def test_overlap_then_retry_reuses_patient_and_case(env):
    lead = booked_setup(env)
    env.stride.script["appointments"] = [
        stride_error("400", "overlapping appointment"),
    ]
    first = env.book(lead, date=DAY1, time="9:00 AM")
    assert first.startswith("TAKEN")
    assert env.stride.count("patients") == 1 and env.stride.count("cases") == 1
    second = env.book(lead, date=DAY1, time="10:00 AM")
    assert second.startswith("BOOKED")
    # No second patient or case: the ids saved on the lead were reused.
    assert env.stride.count("patients") == 1 and env.stride.count("cases") == 1


def test_patient_already_exists_flags_for_staff(env):
    lead = booked_setup(env)
    env.stride.script["patients"] = [stride_error("400", "already exists")]
    message = env.book(lead, date=DAY1, time="9:00 AM")
    assert message.startswith("PATIENT_EXISTS")
    row = env.one("select needs_review,review_reason from leads where id=%s", lead)
    assert row["needs_review"] and "already exists" in row["review_reason"]
    assert env.stride.count("appointments") == 0


def test_unclear_result_is_never_retried(env):
    lead = booked_setup(env)
    env.stride.script["appointments"] = [
        stride_error("timeout", "stride timed out", ambiguous=True),
    ]
    message = env.book(lead, date=DAY1, time="9:00 AM")
    assert message.startswith("UNCONFIRMED")
    assert env.one("select state,needs_staff_review from appointments where lead_id=%s",
                   lead) == {"state": "unknown", "needs_staff_review": True}
    calls = len(env.stride.calls)
    assert env.book(lead, date=DAY1, time="10:00 AM").startswith("UNCONFIRMED")
    assert len(env.stride.calls) == calls


def test_case_with_an_evaluation_gets_a_fresh_case(env):
    lead = booked_setup(env)
    env.stride.script["appointments"] = [
        stride_error("403", "case already has an initial evaluation"),
    ]
    message = env.book(lead, date=DAY1, time="9:00 AM")
    assert message.startswith("BOOKED")
    assert env.stride.count("cases") == 2
    cases = [p for name, p in env.stride.calls if name == "appointments"]
    assert cases[0]["case_id"] != cases[1]["case_id"]


def test_two_calls_cannot_hold_the_same_slot(env):
    lead = booked_setup(env)
    other = env.lead()
    slot = env.one("select id from availability_slots where booking_location_id=%s and "
                   "local_date=%s and local_time='09:00'", env.location_id, DAY1)
    env.db.execute("insert into slot_holds(slot_id,lead_id,call_id,expires_at) "
                   "values(%s,%s,'call-other',now()+interval '5 minutes')", (slot["id"], other))
    message = env.book(lead, date=DAY1, time="9:00 AM")
    assert message.startswith(("NOT_AVAILABLE", "TAKEN"))
    assert env.stride.count("patients") == 0
    # An expired hold no longer blocks.
    env.db.execute("update slot_holds set expires_at=now()-interval '1 minute' where call_id='call-other'")
    assert env.book(lead, date=DAY1, time="9:00 AM").startswith("BOOKED")


def test_tool_stops_before_the_deadline(env):
    lead = booked_setup(env)
    tight = env.settings(booking_tool_deadline_seconds=5.0, booking_stride_timeout_seconds=6.0)
    message = env.book(lead, settings=tight, date=DAY1, time="9:00 AM")
    assert message.startswith("FOLLOW_UP")
    assert env.stride.count("patients") == 0
    assert env.one("select state from appointments where lead_id=%s", lead)["state"] == "failed"


def test_booking_switched_off(env):
    lead = booked_setup(env)
    env.db.execute("update practice_settings set stride_booking_enabled=false where practice_id=%s",
                   (env.practice_id,))
    with pytest.raises(slot_booking.BookingToolError):
        env.book(lead, date=DAY1, time="9:00 AM")


def test_bad_patient_details_are_sent_back_to_the_agent(env):
    lead = booked_setup(env)
    with pytest.raises(ValueError, match="BAD_DATE_OF_BIRTH"):
        env.book(lead, date=DAY1, time="9:00 AM", date_of_birth="26/04/1990")
    with pytest.raises(ValueError, match="MISSING_NAME"):
        env.book(lead, date=DAY1, time="9:00 AM", last_name="")


# --------------------------------------------------------------------------- HTTP

def vapi_body(lead_id, name, arguments, call_id="vapi-call-1"):
    return {"message": {
        "type": "tool-calls",
        "call": {"id": call_id, "assistantOverrides": {"variableValues": {"lead_id": lead_id}}},
        "toolCallList": [{"id": "tool-1", "type": "function",
                          "function": {"name": name, "arguments": arguments}}],
    }}


def test_http_endpoints_follow_vapi_contract(env, monkeypatch):
    from rpt_agent.api import app

    open_days(env, c5981={DAY1: ["09:00:00"]})
    env.sync()
    lead = env.lead()
    monkeypatch.setattr(slot_booking, "_fast_providers", lambda settings: env.stride)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-vapi-secret"}

    found = client.post("/api/v1/tools/find-slots", headers=headers,
                        json=vapi_body(lead, "find_slots", {"when": "earliest"}))
    assert found.status_code == 200
    item = found.json()["results"][0]
    assert item["toolCallId"] == "tool-1" and item["result"].startswith("OPENINGS")
    assert "\n" not in item["result"]

    # The model cannot point the tool at another patient.
    spoof = vapi_body(lead, "find_slots", {"when": "earliest", "lead_id": str(uuid4())})
    assert client.post("/api/v1/tools/find-slots", headers=headers,
                       json=spoof).json()["results"][0]["result"].startswith("OPENINGS")

    bad = client.post("/api/v1/tools/find-slots", headers=headers,
                      json=vapi_body(lead, "find_slots", {"when": "someday"}))
    assert bad.status_code == 200 and bad.json()["results"][0]["result"].startswith("BAD_WHEN")

    unlinked = {"message": {"type": "tool-calls", "call": {"id": "x"}, "toolCallList": [
        {"id": "tool-9", "function": {"name": "find_slots", "arguments": {"when": "earliest"}}}]}}
    reply = client.post("/api/v1/tools/find-slots", headers=headers, json=unlinked)
    assert reply.status_code == 200 and "error" in reply.json()["results"][0]

    booked = client.post("/api/v1/tools/book-appointment", headers=headers, json=vapi_body(
        lead, "book_appointment", {**PATIENT, "date": DAY1, "time": "9:00 AM"}))
    assert booked.status_code == 200
    assert booked.json()["results"][0]["result"].startswith("BOOKED")

    assert client.post("/api/v1/tools/find-slots", json=vapi_body(
        lead, "find_slots", {"when": "earliest"})).status_code == 401


def test_offers_from_the_call_are_preferred_when_two_clinicians_share_a_time(env):
    open_days(env, c5981={DAY1: ["09:00:00"]}, c5982={DAY1: ["09:00:00"]})
    env.sync()
    lead = env.lead()
    message = env.find(lead)
    offered = "Thao Nguyen" if "Thao Nguyen" in message else "Alan Rome"
    booked = env.book(lead, date=DAY1, time="9:00 AM")
    assert f"with {offered}" in booked


def test_clock_parsing_and_windows():
    assert slot_booking.parse_clock("9 am").hour == 9
    assert slot_booking.parse_clock("2:30 p.m.").strftime("%H:%M") == "14:30"
    assert slot_booking.parse_clock("14:30").minute == 30
    with pytest.raises(ValueError):
        slot_booking.parse_clock("noonish")
    monday = datetime(2026, 6, 17, 10, tzinfo=UTC)  # a Wednesday
    assert slot_booking.date_window("this_week", monday, 31) == (date(2026, 6, 17), date(2026, 6, 21))
    assert slot_booking.date_window("next_week", monday, 31) == (date(2026, 6, 22), date(2026, 6, 28))
    assert slot_booking.date_window("in_two_weeks", monday, 31) == (date(2026, 6, 29), date(2026, 7, 5))
    assert slot_booking.date_window("earliest", monday, 31)[1] == date(2026, 6, 17) + timedelta(days=30)


def test_blank_today_override_means_unset(monkeypatch):
    from rpt_agent.config import Settings

    monkeypatch.setenv("BOOKING_TODAY_OVERRIDE", "")
    assert Settings(_env_file=None).booking_today_override is None
    monkeypatch.setenv("BOOKING_TODAY_OVERRIDE", "2026-06-15")
    assert Settings(_env_file=None).booking_today_override == TODAY


def test_booking_settles_the_call_so_a_later_summary_cannot_undo_it(env):
    lead = booked_setup(env)
    event = env.db.execute(
        "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for) "
        "values(%s,1,'call','in_flight',now()) returning id", (lead,),
    ).fetchone()["id"]
    assert env.book(lead, date=DAY1, time="9:00 AM",
                    outreach_event_id=str(event)).startswith("BOOKED")
    row = env.one("select status,outcome,settled_by from outreach_events where id=%s", event)
    assert row == {"status": "delivered", "outcome": "booked", "settled_by": "tool"}


def test_spelled_names_split_by_the_voice_model_are_rejoined():
    assert slot_booking.clean_spelled_name("Chaub h ary") == "Chaubhary"
    assert slot_booking.clean_spelled_name("C H A U B H A R Y") == "Chaubhary"
    assert slot_booking.clean_spelled_name("De La Cruz") == "De La Cruz"
    assert slot_booking.clean_spelled_name("Gallina") == "Gallina"


def add_pelvic_service(env, *, minutes=45, appointment_type=1453):
    """A second service seen only by Alan Rome, with its own type and visit length."""
    case_type = env.db.execute(
        "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes) "
        "values(%s,'Physical Therapy - Pelvic',%s,%s) returning id",
        (env.practice_id, appointment_type, minutes),
    ).fetchone()["id"]
    env.db.execute(
        "insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
        "values(%s,%s,%s)", (env.clinicians[5982], env.location_id, case_type),
    )
    env.db.execute(
        "update clinicians set credentials='PT, DPT',bio='Focuses on pelvic health.' where id=%s",
        (env.clinicians[5982],),
    )


def test_service_said_on_the_call_changes_clinicians_and_visit_length(env):
    add_pelvic_service(env)
    open_days(env, c5981={DAY1: ["09:00:00"]}, c5982={DAY1: ["13:00:00"]})
    env.sync()
    lead = env.lead()
    message = env.find(lead, case_type_name="pelvic")
    assert message.startswith("OPENINGS for Physical Therapy - Pelvic") and "Alan Rome" in message
    assert "Thao" not in message
    booked = env.book(lead, date=DAY1, time="1:00 PM", case_type="Physical Therapy - Pelvic")
    assert booked.startswith("BOOKED") and "45 minutes" in booked
    appointment = next(p for name, p in env.stride.calls if name == "appointments")
    assert appointment["appointment_type"] == 1453
    assert appointment["end_date_utc"] == "2026-06-16T17:45:00+00:00"  # 1 PM Eastern + 45 min


def test_unknown_service_lists_the_services_we_book(env):
    add_pelvic_service(env)
    with pytest.raises(ValueError, match="UNKNOWN_SERVICE.*Physical Therapy, Physical Therapy - Pelvic"):
        env.find(env.lead(), case_type_name="Massage")


def test_list_providers_comes_from_assignments_with_profile(env):
    add_pelvic_service(env)
    lead = env.lead()
    general = slot_booking.list_providers(lead_id=lead, settings=env.settings())
    assert general.startswith("PROVIDERS for Physical Therapy at Laguna Niguel")
    assert "Alan Rome (PT, DPT; Focuses on pelvic health.)" in general and "Thao Nguyen" in general
    pelvic = slot_booking.list_providers(lead_id=lead, case_type_name="pelvic",
                                         settings=env.settings())
    assert "Alan Rome" in pelvic and "Thao" not in pelvic


def test_call_variables_name_the_matched_service_and_all_services(env):
    add_pelvic_service(env)
    lead = env.lead(lead_type="knee")  # no service called 'knee': the default applies
    values = slot_booking.call_case_variables(env.db, lead)
    assert values == {"case_type": "Physical Therapy",
                      "case_types_offered": "Physical Therapy, Physical Therapy - Pelvic",
                      "service_aliases": ""}


@pytest.mark.parametrize(("sheet_case", "expected"), [
    ("Pelvic", "Physical Therapy - Pelvic"),        # was the default case before 2026-10-14
    ("pelvic floor", "Physical Therapy - Pelvic"),  # alias
    ("Ortho", "Physical Therapy"),                  # alias
    ("Physical Therapy - Pelvic", "Physical Therapy - Pelvic"),
    ("knee", "Physical Therapy"),                   # nothing fits: the practice default
])
def test_sheet_case_maps_to_the_right_service(env, sheet_case, expected):
    add_pelvic_service(env)
    env.db.execute("update case_types set aliases=array['ortho','orthopedic'] "
                   "where practice_id=%s and name='Physical Therapy'", (env.practice_id,))
    env.db.execute("update case_types set aliases=array['pelvic floor','pelvic health'] "
                   "where practice_id=%s and name='Physical Therapy - Pelvic'", (env.practice_id,))
    lead = env.lead(lead_type=sheet_case)
    assert slot_booking.call_case_variables(env.db, lead)["case_type"] == expected


def test_alias_said_on_the_call_picks_the_service(env):
    add_pelvic_service(env)
    env.db.execute("update case_types set aliases=array['pelvic floor'] "
                   "where practice_id=%s and name='Physical Therapy - Pelvic'", (env.practice_id,))
    open_days(env, c5981={DAY1: ["09:00:00"]}, c5982={DAY1: ["13:00:00"]})
    env.sync()
    message = env.find(env.lead(), case_type_name="Pelvic Floor")
    assert message.startswith("OPENINGS for Physical Therapy - Pelvic")


def test_call_variables_list_the_everyday_words_for_each_service(env):
    add_pelvic_service(env)
    env.db.execute("update case_types set aliases=array['ortho','orthopedic'] "
                   "where practice_id=%s and name='Physical Therapy'", (env.practice_id,))
    values = slot_booking.call_case_variables(env.db, env.lead(lead_type="Ortho"))
    assert values["case_type"] == "Physical Therapy"
    assert values["service_aliases"] == "Physical Therapy = ortho, orthopedic"


def test_booking_keeps_the_birthday_the_patient_confirmed_and_flags_a_difference(env):
    # Client rule 2026-10-14: book with the date the patient says, keep the lead booked, and tell
    # staff (dashboard + Sheet) that the Sheet's date differs.
    from rpt_agent.services.appointment_views import dob_check
    from rpt_agent.services.sheet_sync import build_sheet_snapshot

    open_days(env, c5981={DAY1: ["09:00:00"]})
    env.sync()
    lead = env.lead()
    env.find(lead)
    assert env.book(lead, date=DAY1, time="9:00 AM", date_of_birth="1990-08-19").startswith("BOOKED")
    on_file = env.one("select date_of_birth from leads where id=%s", lead)["date_of_birth"]
    assert env.one("select patient_dob from appointments where lead_id=%s", lead)["patient_dob"].isoformat() == "1990-08-19"
    assert dob_check(env.db, lead) == {"on_file": on_file.isoformat(), "said": "1990-08-19"}
    sheet = build_sheet_snapshot(env.db, str(lead))["sheet"]
    assert sheet["needs_review"].startswith("Check date of birth: Sheet has")
    assert env.one("select status from leads where id=%s", lead)["status"] == "booked"
    # Staff fix the Sheet; the profile sync updates the lead and the warning goes away.
    env.db.execute("update leads set date_of_birth='1990-08-19' where id=%s", (lead,))
    assert dob_check(env.db, lead) is None
    assert build_sheet_snapshot(env.db, str(lead))["sheet"]["needs_review"] == ""


def test_the_confirmation_text_names_the_clinic(env):
    # Clinic request 2026-10-21: the booking confirmation text says where the visit is.
    from rpt_agent.services import delivery

    lead = booked_setup(env)
    assert env.book(lead, date=DAY1, time="9 AM").startswith("BOOKED")
    env.one("update notification_log set next_attempt_at=now() where lead_id=%s returning id", lead)
    phone = env.one("select phone_e164 from leads where id=%s", lead)["phone_e164"]
    sent = []

    class FakeProviders:
        settings = get_settings()

        def send_sms(self, trace, to, body):
            sent.append((to, body))
            return f"SM{uuid4().hex[:30]}"

    delivery.process_pending_integrations(WorkflowTrace("test", "test"), FakeProviders())
    ours = [body for to, body in sent if to == phone]
    assert len(ours) == 1
    assert "confirmed for Tuesday, June 16 at 9:00 AM at our " in ours[0] and " office." in ours[0]


# --------------------------------------------------------------------------- 2026-10-22 fixes

def test_a_clinic_holiday_is_never_offered_or_booked(env):
    """The Stride sandbox has no holiday blocks, so the agent booked Sat July 4 (2026-10-22)."""
    open_days(env, c5981={DAY1: ["09:00:00", "10:00:00"], DAY2: ["08:00:00", "09:00:00", "10:00:00"]})
    env.sync()
    env.db.execute("update practice_settings set holidays=%s::jsonb where practice_id=%s",
                   (f'["{DAY1}", {{"date": "{DAY2}", "close": "10:00"}}]', env.practice_id))
    lead = env.lead()
    offered = env.find(lead)
    assert "June 16" not in offered  # the whole day is closed
    assert "June 17 (2026-06-17) at 8:00 AM" in offered and "9:00 AM" in offered
    assert "10:00 AM" not in offered  # would run past the early close
    creates = sum(env.stride.count(kind) for kind in ("patients", "cases", "appointments"))
    assert not env.book(lead, date=DAY1, time="9:00 AM").startswith("BOOKED")
    assert not env.book(lead, date=DAY2, time="10:00 AM").startswith("BOOKED")
    assert sum(env.stride.count(kind) for kind in ("patients", "cases", "appointments")) == creates


def test_a_booking_saves_the_stride_patient_on_the_appointment_and_says_ten_minutes(env):
    lead = booked_setup(env)
    message = env.book(lead, date=DAY1, time="9:00 AM")
    assert message.startswith("BOOKED") and "more than 10 minutes late" in message
    row = env.one("select a.stride_patient_id,l.stride_patient_id as lead_patient from appointments a "
                  "join leads l on l.id=a.lead_id where a.lead_id=%s", lead)
    assert row["stride_patient_id"] and row["stride_patient_id"] == row["lead_patient"]


def real_today():
    """The real date at the clinic; a named real-calendar date is moved onto the pinned calendar."""
    return datetime.now(slot_booking.ZoneInfo("America/New_York")).date()


def test_a_day_past_the_cache_is_too_far_and_gets_the_nearest_times(env):
    """QA 2026-10-22: a day past the window got NO_OPENINGS although there were many openings.
    Since 2026-10-23 the window is 90 days (BOOKING_CACHE_DAYS): June 15 pinned -> September 12."""
    open_days(env, c5981={DAY1: ["09:00:00", "14:00:00"], DAY2: ["10:00:00"]})
    env.sync()
    lead = env.lead()
    asked = real_today() + timedelta(days=120)  # lands past September 12 on the pinned calendar
    reply = env.find(lead, when="specific_date", specific_date=asked)
    assert reply.startswith(f"NOTHING_IN_RANGE: {slot_booking._spoken_date(asked)} is too far ahead "
                            "to book yet; we can only book up to Saturday, September 12.")
    assert "Tuesday, June 16 (2026-06-16) at 9:00 AM" in reply and "June 17" in reply
    assert "Calendar today: Monday, June 15, 2026." in reply
    # Time of day is kept when it can be; otherwise any time.
    afternoon = env.find(lead, call_id="call-2", when="specific_date", specific_date=asked,
                         time_of_day="afternoon")
    assert "2:00 PM" in afternoon and "9:00 AM" not in afternoon
    evening = env.find(lead, call_id="call-3", when="specific_date", specific_date=asked,
                       time_of_day="evening")
    assert evening.startswith("NOTHING_IN_RANGE") and "9:00 AM" in evening
    # The offered times are remembered, so one can be booked.
    assert env.book(lead, date=DAY1, time="9:00 AM").startswith("BOOKED")


def test_a_day_past_the_cache_with_nothing_open_says_both(env):
    lead = env.lead()
    open_days(env)
    env.sync()
    asked = real_today() + timedelta(days=120)
    reply = env.find(lead, when="specific_date", specific_date=asked)
    assert reply.startswith(f"NO_OPENINGS: {slot_booking._spoken_date(asked)} is too far ahead to "
                            "book yet; we can only book up to Saturday, September 12. No Physical "
                            "Therapy at")
    assert "in the next 90 days" in reply


def test_booking_a_day_past_the_cache_offers_the_nearest_times(env):
    open_days(env, c5981={DAY1: ["09:00:00"]})
    env.sync()
    lead = env.lead()
    reply = env.book(lead, date="2026-09-20", time="9:00 AM")
    assert reply.startswith("NOT_AVAILABLE") and "too far ahead to book yet" in reply
    assert "up to Saturday, September 12" in reply and "June 16 (2026-06-16) at 9:00 AM" in reply


def test_too_far_without_a_pinned_day_uses_the_real_calendar(env):
    """Production has no BOOKING_TODAY_OVERRIDE: the asked day and the last day are real dates."""
    settings = env.settings(booking_today_override=None)
    today = slot_booking.booking_now(settings, "America/New_York").date()
    soon = (today + timedelta(days=2)).isoformat()
    open_days(env, c5981={soon: ["09:00:00"]})
    env.sync(booking_today_override=None)
    lead = env.lead()

    def find(**kwargs):
        return slot_booking.find_slots(env.trace, lead_id=lead, call_id="call-real",
                                       providers=env.stride, settings=settings, **kwargs)

    asked = today + timedelta(days=95)
    reply = find(when="specific_date", specific_date=asked)
    last = today + timedelta(days=89)
    assert reply.startswith(f"NOTHING_IN_RANGE: {slot_booking._spoken_date(asked)} is too far ahead")
    assert f"up to {slot_booking._spoken_date(last)}." in reply and f"({soon}) at 9:00 AM" in reply
    # The last day is still inside the window: not "too far", and the opening 2 days out is offered.
    inside = find(when="specific_date", specific_date=last)
    assert inside.startswith("NOTHING_IN_RANGE: no ") and "too far" not in inside
    assert f"Nearest openings: (1) {slot_booking._spoken_date(today + timedelta(days=2))}" in inside


def test_a_late_day_with_nothing_after_it_offers_the_nearest_earlier_times(env):
    """QA 2026-10-22: the last day of the window (Thanksgiving) got NO_OPENINGS; only later days
    were searched, never the open days before it."""
    open_days(env, c5981={DAY1: ["09:00:00", "14:00:00"]})
    env.sync()
    lead = env.lead()
    last = date(2026, 7, 15)
    reply = env.find(lead, when="specific_date", specific_date=last)
    assert reply.startswith("NOTHING_IN_RANGE: no Physical Therapy at Laguna Niguel openings for "
                            "that time or later. Nearest openings: (1) Tuesday, June 16")
    afternoon = env.find(lead, call_id="call-2", when="specific_date", specific_date=last,
                         time_of_day="afternoon")
    assert "Nearest openings: (1) Tuesday, June 16 (2026-06-16) at 2:00 PM" in afternoon
    evening = env.find(lead, call_id="call-3", when="specific_date", specific_date=last,
                       time_of_day="evening")
    assert "Nearest openings: (1) Tuesday, June 16 (2026-06-16) at 9:00 AM" in evening
    # Booking a taken late day offers the earlier times too, not "no other openings soon".
    booked = env.book(lead, date=last.isoformat(), time="9:00 AM")
    assert booked.startswith("NOT_AVAILABLE") and "Other openings: (1) Tuesday, June 16" in booked
    assert "too far" not in booked


def test_a_real_past_day_on_the_pinned_calendar_is_past(env):
    with pytest.raises(ValueError, match="PAST_DATE"):
        env.find(env.lead(), when="specific_date", specific_date=date(2026, 10, 1))


# --------------------------------------------------------------------------- 90 days (2026-10-23)
# BOOKING_CACHE_DAYS=90 from the pinned June 15: chunk 0 = Jun 15-Jul 15 (the default search window),
# chunk 1 = Jul 16-Aug 15, chunk 2 = Aug 16-Sep 12. Stride allows at most 31 days per request.
DAY45, DAY80 = "2026-07-30", "2026-09-03"  # Thursday July 30, Thursday September 3
LIMITS = ("Last bookable day: Saturday, September 12, 2026; searches without a named date or month "
          "look up to Wednesday, July 15.")


def availability_requests(env):
    return [args for name, args in env.stride.calls if name == "availability"]


def age_chunks(env, minutes, *, chunks=(1, 2)):
    env.db.execute(
        "update availability_sync_state set last_success_at=now()-make_interval(mins=>%s) "
        "where booking_location_id=%s and chunk=any(%s)", (minutes, env.location_id, list(chunks)),
    )


def test_a_sync_asks_stride_for_90_days_in_three_chunks_of_at_most_31_days(env):
    open_days(env)
    result = env.sync()
    requests = availability_requests(env)
    assert result["requests"] == 3 and len(requests) == 3
    windows = [(start, end) for *_, start, end in requests]
    assert windows == [(date(2026, 6, 15), date(2026, 7, 15)), (date(2026, 7, 16), date(2026, 8, 15)),
                       (date(2026, 8, 16), date(2026, 9, 12))]
    assert all((end - start).days + 1 <= 31 for start, end in windows)
    rows = env.db.execute("select chunk,window_start,window_end from availability_sync_state "
                          "where booking_location_id=%s order by chunk", (env.location_id,)).fetchall()
    assert [(r["chunk"], r["window_start"], r["window_end"]) for r in rows] == [
        (index, start, end) for index, (start, end) in enumerate(windows)]
    # Any cache length is cut the same way.
    chunks = availability_sync.cache_chunks(TODAY, 100)
    assert [c.end - c.start for c in chunks] == [timedelta(days=30)] * 3 + [timedelta(days=6)]


def test_the_first_chunk_refreshes_every_cycle_and_later_chunks_every_30_minutes(env):
    open_days(env)
    env.sync()
    env.stride.calls.clear()
    assert env.sync()["requests"] == 1  # 5 minutes later: only the first 31 days
    assert [args[3] for args in availability_requests(env)] == [TODAY]
    age_chunks(env, 29)
    assert env.sync()["requests"] == 1
    age_chunks(env, 30)  # BOOKING_FAR_SYNC_SECONDS=1800
    env.stride.calls.clear()
    assert env.sync()["requests"] == 3
    # A failed later chunk is tried again on the next cycle, not 30 minutes later.
    env.stride.calls.clear()
    env.db.execute("update availability_sync_state set last_error='HTTP 503' where "
                   "booking_location_id=%s and chunk=2", (env.location_id,))
    assert env.sync()["requests"] == 2
    # A new day moves every window, so every chunk is fetched again.
    assert env.sync(booking_today_override=TODAY + timedelta(days=1))["requests"] == 3


def test_a_slot_that_disappears_from_a_later_chunk_becomes_gone(env):
    open_days(env, c5981={DAY45: ["09:00:00"], DAY80: ["10:00:00"]})
    env.sync()
    open_days(env, c5981={DAY80: ["10:00:00"]})
    env.sync()  # later chunks not due yet: the cache still has July 30
    status = "select status from availability_slots where booking_location_id=%s and local_date=%s"
    assert env.one(status, env.location_id, DAY45)["status"] == "open"
    age_chunks(env, 31)
    env.sync()
    assert env.one(status, env.location_id, DAY45)["status"] == "gone"
    assert env.one(status, env.location_id, DAY80)["status"] == "open"


def test_find_a_date_45_and_80_days_out(env):
    open_days(env, c5981={DAY1: ["09:00:00"], DAY45: ["09:00:00", "14:00:00"],
                          DAY80: ["10:00:00"]})
    env.sync()
    lead = env.lead()
    day45 = env.find(lead, when="specific_date", specific_date=date(2026, 7, 30))
    assert day45.startswith("OPENINGS") and "Thursday, July 30 (2026-07-30) at 9:00 AM" in day45
    assert "2:00 PM" in day45 and "June 16" not in day45
    assert "Last bookable day" not in day45  # a named day inside the window: no limits note
    day80 = env.find(lead, call_id="call-2", when="specific_date", specific_date=date(2026, 9, 3))
    assert day80.startswith("OPENINGS") and "Thursday, September 3 (2026-09-03) at 10:00 AM" in day80
    # A far day with nothing open gets the next times after it, also far.
    empty = env.find(lead, call_id="call-3", when="specific_date", specific_date=date(2026, 8, 20))
    assert empty.startswith("NOTHING_IN_RANGE: no ") and "Next openings: (1) Thursday, September 3" in empty


def test_default_searches_stay_inside_the_first_31_days(env):
    open_days(env, c5981={DAY1: ["09:00:00"], DAY45: ["09:00:00"]}, c5982={DAY80: ["09:00:00"]})
    env.sync()
    lead = env.lead()
    for when in ("earliest", "this_week", "tomorrow"):
        reply = env.find(lead, call_id=f"call-{when}", when=when)
        assert "June 16" in reply and "July 30" not in reply and "September" not in reply.split(
            "Last bookable day")[0]
    # Nothing in the first 31 days: the default search does not reach into later months.
    env.db.execute("update availability_slots set status='gone' where booking_location_id=%s "
                   "and local_date=%s", (env.location_id, DAY1))
    nothing = env.find(lead, call_id="call-empty", when="earliest")
    assert nothing.startswith("NO_OPENINGS") and "in the next 31 days" in nothing
    therapist = env.find(lead, call_id="call-alan", clinician_name="Alan")
    assert therapist.startswith("NO_OPENINGS_FOR_THERAPIST") and "next 31 days" in therapist
    afternoon = env.find(lead, call_id="call-pm", when="next_week", time_of_day="afternoon")
    assert afternoon.startswith("NO_OPENINGS")


def test_next_month_and_a_named_month(env):
    open_days(env, c5981={DAY1: ["09:00:00"], "2026-07-20": ["11:00:00"], "2026-08-10": ["09:00:00"]})
    env.sync()
    lead = env.lead()
    july = env.find(lead, when="next_month")  # pinned June 15 -> July, across chunks 0 and 1
    assert july.startswith("OPENINGS") and "Monday, July 20 (2026-07-20) at 11:00 AM" in july
    assert "June 16" not in july and "August" not in july.split("Last bookable day")[0]
    august = env.find(lead, call_id="call-2", when="specific_month", month=date(2026, 8, 1))
    assert august.startswith("OPENINGS") and "Monday, August 10 (2026-08-10) at 9:00 AM" in august
    # A real month (two months after the real one) lands two months after the pinned one: August.
    real = real_today().replace(day=1)
    two_on = slot_booking._add_months(real, 2)
    moved = env.find(lead, call_id="call-3", when="specific_month", month=two_on)
    assert "August 10 (2026-08-10)" in moved
    # A month with nothing open gets the next times after it.
    september = env.find(lead, call_id="call-4", when="specific_month", month=date(2026, 9, 1))
    assert september.startswith("NOTHING_IN_RANGE") and "June 16" in september
    # Past the last bookable day.
    far = slot_booking._add_months(real, 5)
    too_far = env.find(lead, call_id="call-5", when="specific_month", month=far)
    assert too_far.startswith(f"NOTHING_IN_RANGE: {far.strftime('%B %Y')} is too far ahead to book "
                              "yet; we can only book up to Saturday, September 12.")
    with pytest.raises(ValueError, match="NEED_MONTH"):
        env.find(lead, when="specific_month")


def test_a_far_search_refreshes_only_the_chunks_it_needs(env):
    open_days(env, c5981={DAY1: ["09:00:00"], DAY45: ["09:00:00"]})
    lead = env.lead()  # nothing synced yet
    assert env.find(lead, when="earliest").startswith("OPENINGS")
    assert len(availability_requests(env)) == 1  # a default search: first chunk only, as before
    env.stride.calls.clear()
    assert env.find(lead, call_id="call-2", when="specific_date",
                    specific_date=date(2026, 7, 30)).startswith("OPENINGS")
    assert [args[3] for args in availability_requests(env)] == [date(2026, 7, 16)]
    # A later chunk 40 minutes old is still fine (it refreshes every 30); 50 minutes is not.
    env.stride.calls.clear()
    age_chunks(env, 40, chunks=(1,))
    env.find(lead, call_id="call-3", when="specific_date", specific_date=date(2026, 7, 30))
    assert availability_requests(env) == []
    age_chunks(env, 50, chunks=(1,))
    env.find(lead, call_id="call-4", when="specific_date", specific_date=date(2026, 7, 30))
    assert len(availability_requests(env)) == 1


def test_booking_a_far_slot_rechecks_it_live(env):
    open_days(env, c5981={DAY80: ["10:00:00"]})
    env.sync()
    lead = env.lead()
    env.find(lead, when="specific_date", specific_date=date(2026, 9, 3))
    env.stride.calls.clear()
    booked = env.book(lead, date=DAY80, time="10:00 AM")
    assert booked.startswith("BOOKED: Thursday, September 3 at 10:00 AM with Thao Nguyen")
    live = availability_requests(env)
    assert live == [(3170, 60, "5981", date(2026, 9, 3), date(2026, 9, 3))]
    # Gone in Stride at the last moment: not booked, the slot is marked gone.
    open_days(env, c5981={"2026-09-04": ["10:00:00"]})
    env.sync()
    age_chunks(env, 31)
    other = env.lead()
    open_days(env, c5981={"2026-09-04": ["10:00:00"], "2026-09-08": ["10:00:00"]})
    env.sync()
    env.find(other, call_id="call-9", when="specific_date", specific_date=date(2026, 9, 4))
    open_days(env, c5981={"2026-09-08": ["10:00:00"]})
    taken = env.book(other, call_id="call-9", date="2026-09-04", time="10:00 AM")
    assert taken.startswith("TAKEN") and "September 8" in taken


def test_a_holiday_in_the_second_month_is_never_offered_or_booked(env):
    open_days(env, c5981={DAY45: ["09:00:00"], "2026-07-31": ["09:00:00"]})
    env.sync()
    env.db.execute("update practice_settings set holidays=%s::jsonb where practice_id=%s",
                   (f'["{DAY45}"]', env.practice_id))
    lead = env.lead()
    reply = env.find(lead, when="specific_date", specific_date=date(2026, 7, 30))
    assert reply.startswith("NOTHING_IN_RANGE") and "July 30" not in reply.split("openings:")[1]
    assert "Friday, July 31 (2026-07-31) at 9:00 AM" in reply
    assert not env.book(lead, date=DAY45, time="9:00 AM").startswith("BOOKED")


def test_90_days_without_a_pinned_day(env):
    settings = env.settings(booking_today_override=None)
    today = slot_booking.booking_now(settings, "America/New_York").date()
    day45, day80 = today + timedelta(days=45), today + timedelta(days=80)
    open_days(env, c5981={day45.isoformat(): ["09:00:00"], day80.isoformat(): ["10:00:00"]})
    result = env.sync(booking_today_override=None)
    assert result["requests"] == 3
    lead = env.lead()

    def find(call_id, **kwargs):
        return slot_booking.find_slots(env.trace, lead_id=lead, call_id=call_id,
                                       providers=env.stride, settings=settings, **kwargs)

    assert f"({day45.isoformat()}) at 9:00 AM" in find("a", when="specific_date", specific_date=day45)
    assert f"({day80.isoformat()}) at 10:00 AM" in find("b", when="specific_date", specific_date=day80)
    month = find("c", when="specific_month", month=day80.replace(day=1))
    assert f"({day80.isoformat()}) at 10:00 AM" in month
    assert find("d", when="earliest").startswith("NO_OPENINGS")  # both are past the first 31 days
    booked = slot_booking.book_slot(env.trace, lead_id=lead, call_id="b",
                                    arguments={**PATIENT, "date": day80.isoformat(),
                                               "time": "10:00 AM"},
                                    providers=env.stride, settings=settings)
    assert booked.startswith("BOOKED")


def test_http_find_slots_takes_a_month(env, monkeypatch):
    from rpt_agent.api import app

    open_days(env, c5981={"2026-08-10": ["09:00:00"]})
    env.sync()
    lead = env.lead()
    monkeypatch.setattr(slot_booking, "_fast_providers", lambda settings: env.stride)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-vapi-secret"}

    def result(arguments):
        reply = client.post("/api/v1/tools/find-slots", headers=headers,
                            json=vapi_body(lead, "find_slots", arguments))
        return reply.json()["results"][0]["result"]

    assert "August 10" in result({"when": "specific_month", "month": "2026-08"})
    assert result({"when": "specific_month", "month": "August"}).startswith("BAD_MONTH")
    assert result({"when": "specific_month", "month": "2026-13"}).startswith("BAD_MONTH")


# ------------------------------------------- last-bookable-day note placement (2026-10-23 sims)
# The agent compared the real date asked (November 30) with the pinned "Last bookable day" (Sep 12)
# and said "too far ahead" although the tool had found times. The note is now only on a too-far reply
# and on searches without a named day or month; a moved day says which day it found times for.

def test_a_named_real_day_moved_by_the_pin_says_it_is_the_day_asked_and_has_no_limits(env):
    asked = real_today() + timedelta(days=38)
    asked += timedelta(days=(3 - asked.weekday()) % 7)  # a Thursday, about 5-6 weeks out
    moved = slot_booking.pinned_calendar_date(asked, datetime.now(UTC), TODAY, 90,
                                              real_today=real_today())
    assert moved != asked and moved.weekday() == 3
    open_days(env, c5981={DAY1: ["09:00:00"], moved.isoformat(): ["09:00:00", "10:00:00"]})
    env.sync()
    lead = env.lead()
    reply = env.find(lead, when="specific_date", specific_date=asked)
    spoken = slot_booking._spoken_date(moved)
    assert reply.startswith("OPENINGS for Physical Therapy at Laguna Niguel for the day asked (on "
                            f"the booking calendar: {spoken}): (1) {spoken} ({moved}) at 9:00 AM")
    assert reply.endswith(slot_booking.BOOK_HINT)
    assert "Last bookable day" not in reply and "Calendar today" not in reply
    assert "too far" not in reply
    # Nothing open on it: the next times, still named as the day asked, still no limits.
    env.db.execute("update availability_slots set status='gone' where booking_location_id=%s "
                   "and local_date=%s", (env.location_id, moved))
    empty = env.find(lead, call_id="call-2", when="specific_date", specific_date=asked)
    assert empty.startswith("NOTHING_IN_RANGE: no Physical Therapy at Laguna Niguel openings for "
                            f"the day asked (on the booking calendar: {spoken}) for that time")
    assert "Last bookable day" not in empty and "too far" not in empty
    # A real month moved by the pin, the same.
    month = slot_booking._add_months(real_today().replace(day=1), 1)
    moved_month = env.find(lead, call_id="call-3", when="specific_month", month=month)
    pinned_month = slot_booking._spoken_month(slot_booking._add_months(TODAY.replace(day=1), 1))
    assert f"for the month asked (on the booking calendar: {pinned_month})" in moved_month
    assert "Last bookable day" not in moved_month


def test_a_pinned_calendar_day_is_kept_and_named_plainly(env):
    open_days(env, c5981={DAY45: ["09:00:00"]})
    env.sync()
    reply = env.find(env.lead(), when="specific_date", specific_date=date(2026, 7, 30))
    assert reply == ("OPENINGS for Physical Therapy at Laguna Niguel: (1) Thursday, July 30 "
                     f"(2026-07-30) at 9:00 AM with Thao Nguyen. {slot_booking.BOOK_HINT}")


def test_the_limits_note_stays_on_default_searches_and_too_far(env):
    open_days(env, c5981={DAY1: ["09:00:00"], DAY2: ["10:00:00"]})
    env.sync()
    lead = env.lead()
    for when in ("earliest", "today", "tomorrow", "this_week", "next_week", "in_two_weeks"):
        reply = env.find(lead, call_id=f"call-{when}", when=when)
        assert reply.endswith(f"Calendar today: Monday, June 15, 2026. {LIMITS}"), when
    morning = env.find(lead, call_id="call-am", when="earliest", time_of_day="morning")
    assert morning.startswith("OPENINGS for Physical Therapy at Laguna Niguel: ") and LIMITS in morning
    far = env.find(lead, call_id="call-far", when="specific_date",
                   specific_date=real_today() + timedelta(days=120))
    assert "is too far ahead to book yet" in far and LIMITS in far


def test_without_a_pinned_day_only_the_limits_note_moves(env):
    """Production (no BOOKING_TODAY_OVERRIDE): a named day reads as before, minus the note."""
    settings = env.settings(booking_today_override=None)
    today = slot_booking.booking_now(settings, "America/New_York").date()
    soon = today + timedelta(days=3)
    open_days(env, c5981={soon.isoformat(): ["09:00:00"]})
    env.sync(booking_today_override=None)
    lead = env.lead()

    def find(call_id, **kwargs):
        return slot_booking.find_slots(env.trace, lead_id=lead, call_id=call_id,
                                       providers=env.stride, settings=settings, **kwargs)

    named = find("a", when="specific_date", specific_date=soon)
    assert named == (f"OPENINGS for Physical Therapy at Laguna Niguel: (1) "
                     f"{slot_booking._spoken_date(soon)} ({soon}) at 9:00 AM with Thao Nguyen. "
                     f"{slot_booking.BOOK_HINT}")
    earliest = find("b", when="earliest")
    assert earliest.startswith(named) and "Last bookable day: " in earliest
    far = find("c", when="specific_date", specific_date=today + timedelta(days=95))
    assert "too far ahead" in far and "Last bookable day: " in far
