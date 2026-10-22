"""The automatic second look at an unclear booking (services/booking_check.py), with a FAKE Stride.

Owner, 2026-10-22: a Stride timeout during book_slot left the appointment 'unknown' and the lead in
review until staff acted. Now the booking-worker checks: Stride's own files (found -> booked), then
Stride's openings (time still open -> the visit was not made), with backoff for about 30 minutes.
It never creates anything in Stride and is safe to run twice.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied).
"""

# ruff: noqa: F811 - `env` is the shared booking fixture, injected by name

from datetime import timedelta

import pytest
from test_slot_booking_integration import (  # noqa: F401 - the shared booking fixture
    DAY1,
    booked_setup,
    env,
    open_days,
    stride_error,
)

from rpt_agent.services.booking_check import run_booking_checks

pytestmark = pytest.mark.integration


def _unclear(env):
    lead = booked_setup(env)
    env.stride.script["appointments"] = [stride_error("timeout", "stride timed out", ambiguous=True)]
    assert env.book(lead, date=DAY1, time="9:00 AM").startswith("UNCONFIRMED")
    appt = env.one("select id,state,updated_at,slot_id,start_utc,clinician_id from appointments "
                   "where lead_id=%s", lead)
    assert appt["state"] == "unknown"
    return lead, appt


def _check(env, appt, minutes):
    return run_booking_checks(env.trace, env.stride, now=appt["updated_at"] + timedelta(minutes=minutes),
                              practice_id=env.practice_id)


def _review(env, lead):
    return env.one("select code,resolved_at,resolution,details from lead_reviews where lead_id=%s "
                   "order by id desc limit 1", lead)


def test_not_checked_before_a_minute_has_passed(env):
    _, appt = _unclear(env)
    calls = env.stride.count("availability")
    early = appt["updated_at"] + timedelta(seconds=20)
    assert run_booking_checks(env.trace, env.stride, now=early, practice_id=env.practice_id)["checked"] == 0
    assert env.stride.count("availability") == calls


def test_time_still_open_in_stride_means_the_visit_was_not_made(env):
    lead, appt = _unclear(env)
    open_days(env, c5981={DAY1: ["09:00:00", "10:00:00"]})  # Stride still offers 9:00
    creates = sum(env.stride.count(kind) for kind in ("patients", "cases", "appointments"))
    assert _check(env, appt, 2)["not_made"] == 1
    row = env.one("select state,needs_staff_review from appointments where id=%s", appt["id"])
    assert (row["state"], row["needs_staff_review"]) == ("failed", False)
    assert env.one("select status from availability_slots where id=%s", appt["slot_id"])["status"] == "open"
    lead_row = env.one("select needs_review,review_reason from leads where id=%s", lead)
    assert lead_row["needs_review"] and lead_row["review_reason"] == "Booking did not go through in Stride"
    review = _review(env, lead)
    assert review["code"] == "booking_failed" and review["resolved_at"] is None
    # Read-only: nothing new was created in Stride. Running again changes nothing.
    assert sum(env.stride.count(kind) for kind in ("patients", "cases", "appointments")) == creates
    assert _check(env, appt, 4)["not_made"] == 0


def test_found_in_strides_file_books_the_lead_and_closes_the_review(env):
    lead, appt = _unclear(env)
    patient = env.one("select stride_patient_id from leads where id=%s", lead)["stride_patient_id"]
    assert patient  # the patient was created before the appointment call timed out
    env.db.execute(
        "insert into stride_appointments(practice_id,stride_id,patient_stride_id,status,start_utc,"
        "clinician_id) values(%s,%s,%s,'Unmarked',%s,%s)",
        (env.practice_id, patient + 7, patient, appt["start_utc"], appt["clinician_id"]))
    for _ in range(2):  # run twice: the second pass finds nothing to do
        result = _check(env, appt, 2)
    assert result["confirmed"] == 0
    row = env.one("select state,stride_appointment_id from appointments where id=%s", appt["id"])
    assert (row["state"], row["stride_appointment_id"]) == ("scheduled", patient + 7)
    lead_row = env.one("select status,cadence_state,needs_review from leads where id=%s", lead)
    assert (lead_row["status"], lead_row["cadence_state"], lead_row["needs_review"]) == (
        "booked", "completed", False)
    review = _review(env, lead)
    assert review["resolution"] == "confirmed_in_stride"
    assert env.one("select count(*) n from notification_log where lead_id=%s "
                   "and notification_type='sms_appointment_booked'", lead)["n"] == 1
    env.db.execute("delete from stride_appointments where practice_id=%s", (env.practice_id,))


def test_time_gone_or_stride_failing_waits_with_backoff_then_stops_asking(env):
    lead, appt = _unclear(env)
    open_days(env, c5981={DAY1: ["10:00:00"]})  # 9:00 is gone: probably ours, not proven
    assert _check(env, appt, 2)["waiting"] == 1
    assert _review(env, lead)["details"]["stride_check"]["attempts"] == 1
    calls = env.stride.count("availability")
    _check(env, appt, 2)  # attempt 2 is not due until 3 minutes
    assert env.stride.count("availability") == calls
    env.stride.availability_error = stride_error("503", "stride returned HTTP 503")
    assert _check(env, appt, 4)["errors"] == 1
    assert _review(env, lead)["details"]["stride_check"]["attempts"] == 2
    # After about 30 minutes Stride is not asked again: Stride's file and the front desk take over.
    env.stride.availability_error = None
    calls = env.stride.count("availability")
    _check(env, appt, 45)
    assert env.stride.count("availability") == calls
    assert env.one("select state from appointments where id=%s", appt["id"])["state"] == "unknown"
    assert env.one("select needs_review from leads where id=%s", lead)["needs_review"] is True
