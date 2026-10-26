"""Booking views on a real Postgres schema: real Stride import, real SQL.

Needs TEST_DATABASE_URL (a disposable local database). Reuses the Stride import fixture,
which builds its own practice and removes it afterwards.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_stride_import_integration import (  # noqa: F401 (env is a fixture)
    APPOINTMENT_HEADER,
    PATIENT_HEADER,
    appointment,
    csv_bytes,
    env,
    future_day,
    patient,
)

from rpt_agent.routes import dashboard_calendar as calendar
from rpt_agent.security import DashboardActor
from rpt_agent.services import appointment_views as av

pytestmark = pytest.mark.integration


@pytest.fixture
def booked(env):  # noqa: F811
    yield env
    db, practice = env.db, env.practice_id
    leads = [r["id"] for r in db.execute("select id from leads where practice_id=%s", (practice,))]
    db.execute("delete from appointments where practice_id=%s", (practice,))
    db.execute("delete from call_transcripts where lead_id=any(%s)", (leads,))
    db.execute("delete from call_logs where lead_id=any(%s)", (leads,))
    db.execute("delete from clinician_assignments where clinician_id in "
               "(select id from clinicians where practice_id=%s)", (practice,))
    for table in ("clinicians", "case_types", "booking_locations"):
        db.execute(f"delete from {table} where practice_id=%s", (practice,))


def _booking_setup(ctx):
    db, practice = ctx.db, ctx.practice_id
    location = db.execute(
        "insert into booking_locations(practice_id,name,stride_location_id,timezone) "
        "values(%s,'Laguna Niguel',2,'America/New_York') returning id", (practice,)).fetchone()["id"]
    case = db.execute(
        "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes) "
        "values(%s,'Physical Therapy',2,60) returning id", (practice,)).fetchone()["id"]
    clinician = db.execute(
        "insert into clinicians(practice_id,stride_user_id,display_name) values(%s,3,'Joe Smith') "
        "returning id", (practice,)).fetchone()["id"]
    db.execute("insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
               "values(%s,%s,%s)", (clinician, location, case))
    return case


def test_every_kind_of_booking_is_tagged_and_named(booked):
    ctx = booked
    case = _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    now = datetime.now(UTC)
    front_desk = ctx.lead("Sheet Lead", dob="1990-01-01", phone="+15552010001")
    elliot = ctx.lead("Elliot Lead", dob="1991-02-02", phone="+15552010002")
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("101", "Sheet", "Lead", "1990-01-01", mobile="(555) 201-0001"),
        patient("102", "Walk", "In", "1985-05-05", mobile="(555) 201-0099"),
    ]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day(3)),
        appointment("9002", "102", "7002", future_day(4), status="Checked In"),
        appointment("9003", "102", "7002", future_day(5), status="Late Cancel"),
    ]))
    ctx.tick()

    # Elliot booked on a call 84 seconds in; Stride has not exported it yet.
    dialed = now - timedelta(hours=2)
    call = db.execute(
        "insert into call_logs(lead_id,vapi_call_id,dialed_at) values(%s,'call-elliot-1',%s) returning id",
        (elliot, dialed)).fetchone()["id"]
    db.execute("insert into call_transcripts(call_log_id,lead_id,transcript_text) values(%s,%s,%s)",
               (call, elliot, ("AI: Does Tuesday work?\nUser: Mornings are better for me please.\n"
                               "AI: Great, you're all set.")))
    start = now + timedelta(days=6)
    db.execute(
        "insert into appointments(lead_id,practice_id,booking_source,state,start_utc,end_utc,clinician_id,"
        "location_id,case_type_id,call_id,created_at) values(%s,%s,'voice_agent','scheduled',%s,%s,3,2,%s,"
        "'call-elliot-1',%s)",
        (elliot, practice, start, start + timedelta(hours=1), case, dialed + timedelta(seconds=84)))

    records, truncated = av.load_records(db, practice, lower=now - timedelta(days=1),
                                         upper=now + timedelta(days=30))
    assert not truncated
    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {"stride:9001", "stride:9002", "stride:9003", next(
        k for k in by_id if k.startswith("booking:"))}
    sheet = by_id["stride:9001"]
    assert (sheet["tag"], sheet["booked_by"], sheet["state"]) == ("sheet_lead", "stride", "scheduled")
    assert sheet["lead_id"] == front_desk and sheet["location"] == "Laguna Niguel"
    assert by_id["stride:9002"]["tag"] == "stride_only" and by_id["stride:9002"]["state"] == "completed"
    assert by_id["stride:9003"]["state"] == "late_cancel" and by_id["stride:9003"]["lead_id"] is None
    ours = next(r for r in records if r["id"].startswith("booking:"))
    assert (ours["tag"], ours["booked_by"], ours["type"]) == ("elliot", "voice_agent", "Initial Evaluation")
    assert ours["practitioner_name"] == "Joe Smith" and ours["case_title"] == "Physical Therapy"
    assert ours["call_log_id"] == call and ours["call_offset_seconds"] == 84

    card = av.booking_card(db, practice, elliot, now)
    assert card["quote"] == "Mornings are better for me please."
    bookings = av.lead_bookings(db, practice, [front_desk, elliot], now)
    assert bookings[str(front_desk)]["id"] == "stride:9001"
    assert bookings[str(elliot)]["tag"] == "elliot"

    # Upcoming-only window (home page): the past is excluded, open upper end works.
    upcoming, _ = av.load_records(db, practice, lower=now)
    assert {r["id"] for r in upcoming} == set(by_id)


def test_stride_copy_wins_and_a_removed_visit_stays_hidden(booked):
    ctx = booked
    _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    lead = ctx.lead("Elliot Lead", dob="1991-02-02", phone="+15552010002")
    start = datetime.now(UTC) + timedelta(days=5)
    db.execute(
        "insert into appointments(lead_id,practice_id,booking_source,state,start_utc,stride_appointment_id) "
        "values(%s,%s,'voice_agent','scheduled',%s,9100)", (lead, practice, start))
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("201", "Elliot", "Lead", "1991-02-02", mobile="(555) 201-0002")]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9100", "201", "7100", future_day(5), status="Cancel")]))
    ctx.tick()
    records, _ = av.load_records(db, practice, lead_ids=[lead])
    # One visit, Stride's version (cancelled), still credited to Elliot.
    assert [(r["id"], r["state"], r["tag"]) for r in records] == [("stride:9100", "cancelled", "elliot")]

    db.execute("update stride_appointments set deleted_at=now() where practice_id=%s and stride_id=9100",
               (practice,))
    records, _ = av.load_records(db, practice, lead_ids=[lead])
    assert records == []  # removed in Stride: neither copy shows


# --- calendar redesign data (2026-10-26) -------------------------------------------------

NY = ZoneInfo("America/New_York")  # the import fixture's practice timezone


def _at(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(hour, minute), NY)


def _ours(ctx, lead, start, stride_id=None, case=None):
    ctx.db.execute(
        "insert into appointments(lead_id,practice_id,booking_source,state,start_utc,end_utc,clinician_id,"
        "location_id,case_type_id,stride_appointment_id) values(%s,%s,'voice_agent','scheduled',%s,%s,3,2,%s,%s)",
        (lead, ctx.practice_id, start, start + timedelta(hours=1), case, stride_id))


def test_elliot_booking_waits_for_approval_until_the_stride_export_shows_it(booked):
    ctx = booked
    case = _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    db.execute("update clinicians set credentials='PT, DPT' where practice_id=%s", (practice,))
    lead = ctx.lead("Ada Lovelace", dob="1991-02-02", phone="+15552010002")
    day = future_day(5)
    _ours(ctx, lead, _at(day, 10), stride_id=9200, case=case)
    actor = DashboardActor("review", "review@example.test", practice_id=practice)

    (waiting,), _ = av.load_records(db, practice, lead_ids=[lead])
    assert waiting["id"] == "stride:9200"
    assert (waiting["approval"], waiting["stride_status"], waiting["source"]) == ("waiting", None, "elliot")
    assert waiting["patient_name"] == "Lovelace, Ada" and waiting["visit_kind"] == "first_visit"
    assert waiting["clinician"] == {"id": "3", "name": "Joe Smith", "role": "PT, DPT"}
    assert waiting["location_id"] == 2 and waiting["location"] == "Laguna Niguel"
    listed = calendar.appointment_approvals(actor)
    assert [r["id"] for r in listed["appointments"]] == ["stride:9200"] and listed["count"] == 1

    # Stride's export shows the visit (front desk approved it): Stride's copy, Stride's status.
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("201", "Ada", "Lovelace", "1991-02-02", mobile="(555) 201-0002")]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9200", "201", "7200", day, status="U")]))
    ctx.tick()
    (seen,), _ = av.load_records(db, practice, lead_ids=[lead])
    assert seen["id"] == "stride:9200" and seen["record_from"] == "Stride"
    assert (seen["approval"], seen["stride_status"], seen["source"]) == ("in_stride", "unmarked", "elliot")
    assert seen["patient_name"] == "Lovelace, Ada" and seen["visit_type"] == "Initial Evaluation"
    assert calendar.appointment_approvals(actor)["appointments"] == []

    for minute, (letter, status) in enumerate([("O", "confirmed"), ("C", "checked_in"), ("N", "no_show"),
                                               ("L", "late_cancel"), ("A", "cancelled")], start=1):
        ctx.drop(f"appointments_202610011{minute:02d}000.csv", csv_bytes(APPOINTMENT_HEADER, [
            appointment("9200", "201", "7200", day, status=letter,
                        modified=f"2026-10-01 11:{minute:02d}:00")]))
        ctx.tick()
        (seen,), _ = av.load_records(db, practice, lead_ids=[lead])
        assert (seen["approval"], seen["stride_status"]) == ("in_stride", status)


def test_waiting_list_is_upcoming_first_and_skips_what_is_off_the_books(booked):
    ctx = booked
    case = _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    now = datetime.now(UTC)
    late, soon, later, gone = (ctx.lead(f"Lead {n}", phone=f"+1555201000{n}") for n in range(4))
    _ours(ctx, late, now - timedelta(days=2), case=case)   # never approved: still listed, after the upcoming
    _ours(ctx, soon, now + timedelta(days=1), case=case)
    _ours(ctx, later, now + timedelta(days=9), stride_id=9300, case=case)
    _ours(ctx, gone, now + timedelta(days=3), case=case)
    db.execute("update appointments set state='cancelled' where lead_id=%s", (gone,))
    records, truncated = av.waiting_records(db, practice, now)
    assert not truncated
    assert [r["lead_id"] for r in records] == [soon, later, late]
    assert all(r["approval"] == "waiting" for r in records)


def test_day_summary_counts_booked_and_open_slots_and_finds_working_hours(booked):
    ctx = booked
    case = _booking_setup(ctx)
    db, practice = ctx.db, ctx.practice_id
    clinician = db.execute("select id from clinicians where practice_id=%s", (practice,)).fetchone()["id"]
    laguna = db.execute("select id from booking_locations where practice_id=%s", (practice,)).fetchone()["id"]
    # A second clinic on the same Stride location (as on the demo): its slots are the same slots.
    dana = db.execute(
        "insert into booking_locations(practice_id,name,stride_location_id,timezone) "
        "values(%s,'Dana Point',2,'America/New_York') returning id", (practice,)).fetchone()["id"]
    db.execute("insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
               "values(%s,%s,%s)", (clinician, dana, case))
    day, next_day = future_day(3), future_day(4)

    def slot(location, hour, status="open", minutes=60):
        start = _at(day, hour)
        db.execute(
            "insert into availability_slots(practice_id,clinician_id,booking_location_id,duration_minutes,"
            "start_utc,end_utc,local_date,local_time,timezone,status) values(%s,%s,%s,%s,%s,%s,%s,%s,"
            "'America/New_York',%s)",
            (practice, clinician, location, minutes, start, start + timedelta(minutes=minutes),
             start.date(), start.time(), status))

    for hour in (9, 10, 11, 14):
        slot(laguna, hour)
    slot(dana, 9)                      # same 9:00 slot cached for the other clinic: counted once
    slot(laguna, 13, status="booked")  # we booked it: not open, still the schedule
    slot(laguna, 16, status="gone")    # taken since: not open, still the schedule
    slot(laguna, 17, minutes=30)       # another visit length: not counted, still the schedule
    # Only `day` is covered by a successful sync.
    db.execute(
        "insert into availability_sync_state(booking_location_id,duration_minutes,chunk,window_start,"
        "window_end,last_success_at,slot_count) values(%s,60,0,current_date,%s,now(),5)", (laguna, day))

    lead = ctx.lead("Ada Lovelace", dob="1991-02-02", phone="+15552010002")
    _ours(ctx, lead, _at(day, 13), case=case)  # Elliot's, waiting for approval: holds the time
    ctx.drop("patients_20261001100000.csv", csv_bytes(PATIENT_HEADER, [
        patient("301", "Walk", "In", "1985-05-05", mobile="(555) 201-0099")]))
    ctx.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9401", "301", "7401", day, start="12:00 PM", end="1:00 PM"),
        appointment("9402", "301", "7401", day, start="3:00 PM", end="4:00 PM", status="Cancel"),
        appointment("9403", "301", "7401", day, start="4:00 PM", end="5:00 PM", status="L"),
        appointment("9404", "301", "7401", day, start="6:00 PM", end="7:00 PM", status="O"),
        appointment("9405", "301", "7401", next_day, start="10:00 AM", end="11:00 AM"),
    ]))
    ctx.tick()

    result = calendar.appointment_calendar(
        DashboardActor("review", "review@example.test", practice_id=practice),
        date.fromisoformat(day), date.fromisoformat(next_day) + timedelta(days=1))
    summary = {r["date"]: r for r in result["day_summary"] if r["practitioner_id"] == "3"}
    assert summary[day] == {
        "date": day, "practitioner_id": "3", "practitioner_name": "Joe Smith", "role": None,
        # 9:00 first slot; the 6-7 PM visit stretches the day past the last slot (17:30).
        "working_hours": {"start": "09:00", "end": "19:00", "source": "slots"},
        "booked": 3,     # 12:00 + 18:00 Stride, 13:00 ours waiting; cancel and late cancel excluded
        "available": 4,  # 9, 10, 11, 14 at 60 minutes
        "slot_minutes": 60,
    }
    # Outside the synced window: open slots unknown; hours only from the visit.
    assert summary[next_day]["available"] is None and summary[next_day]["booked"] == 1
    assert summary[next_day]["working_hours"] == {"start": "10:00", "end": "11:00", "source": "visits"}
    last = (date.fromisoformat(next_day) + timedelta(days=1)).isoformat()
    assert summary[last]["working_hours"] is None and summary[last]["booked"] == 0
    waiting = [r for r in result["appointments"] if r["approval"] == "waiting"]
    assert [r["lead_id"] for r in waiting] == [lead]
