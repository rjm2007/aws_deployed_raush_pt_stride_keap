"""Demo-only review groups on a real schema: Stride appointments and the booking agent.

Owner decisions, 2026-10-22 (port of production's "Needs Review" to the demo):
- A booked lead whose Stride visit was cancelled / late-cancelled / a no-show / removed is an
  "Appointment" review: Front desk, Restart cadence (Day 0, restart cooldown) or Text booking link.
  Never "Continue": its cadence is over. A new future visit in Stride still books it by itself.
- A booking the agent could not confirm is a "Booking" review: Front desk only (the patient may
  be booked). Stride's next file showing the visit books the lead and closes the review.
- Calls only (sms_disabled) means no text at all, the booking confirmation text included.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied).
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_stride_import_integration import (  # noqa: F401 - the shared Stride fixture
    APPOINTMENT_HEADER,
    _book_then_change,
    appointment,
    csv_bytes,
    env,
    future_day,
)

from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import slot_booking
from rpt_agent.services.delivery import process_pending_integrations
from rpt_agent.services.lead_status import mark_booked

pytestmark = pytest.mark.integration

HEADERS = {"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "emma01",
           "X-Dashboard-User-Name": "Emma", "X-Dashboard-User-Role": "employee"}
STEPS = [(0, "call"), (0, "sms"), (2, "call"), (5, "sms")]


@pytest.fixture
def denv(env, monkeypatch):  # noqa: F811 - pytest fixture injection
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("TEST_MODE", "false")
    get_settings.cache_clear()
    db = env.db
    # The Stride fixture's leads share one synthetic number; another suite may have left it blocked.
    db.execute("delete from suppressed_numbers where phone_e164='+15552010000'")
    db.execute("update practice_settings set booking_link_url='https://book.example.test' "
               "where practice_id=%s", (env.practice_id,))
    version = db.execute(
        "insert into cadence_versions(practice_id,version_number,name,status,activated_at) "
        "values(%s,1,'Synthetic v1','active',now()) returning id", (env.practice_id,)).fetchone()["id"]
    for order, (day, channel) in enumerate(STEPS):
        db.execute(
            "insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,channel,key,"
            "description,is_active) values(%s,%s,%s,%s,%s,%s,'step',true)",
            (env.practice_id, version, order, day, channel, f"s{order}-{uuid4().hex[:6]}"))
    yield env
    leads = [r["id"] for r in db.execute("select id from leads where practice_id=%s", (env.practice_id,))]
    db.execute("delete from notification_log where lead_id=any(%s)", (leads,))
    db.execute("delete from appointments where lead_id=any(%s)", (leads,))
    db.execute("delete from outreach_events where lead_id=any(%s)", (leads,))
    db.execute("delete from cadence_steps where cadence_version_id=%s", (version,))
    db.execute("delete from cadence_versions where id=%s", (version,))
    db.execute("delete from dashboard_audit_log where practice_id=%s", (env.practice_id,))


def _get(path):
    return TestClient(app).get(f"/api/v1/dashboard/{path}", headers=HEADERS)


def _post(path, body):
    return TestClient(app).post(f"/api/v1/dashboard/{path}", json=body, headers=HEADERS)


def _choices(lead_id):
    review = _get(f"leads/{lead_id}").json()["lead"]["review"]
    return review["group"], [option["choice"] for option in review["options"]]


def _reviews(denv, lead_id):
    return denv.db.execute("select code,resolved_at,resolution,source from lead_reviews where lead_id=%s "
                           "order by id", (lead_id,)).fetchall()


def _cancelled(denv, name="Cancel Test"):
    lead, _ = _book_then_change(denv, name, status="Cancel")
    assert denv.one("select status,needs_review from leads where id=%s", lead)["needs_review"] is True
    return lead


def test_a_stride_cancel_is_an_appointment_review_without_continue(denv):
    lead = _cancelled(denv)
    assert [(r["code"], r["source"]) for r in _reviews(denv, lead)] == [("appointment_cancelled", "stride")]
    group, choices = _choices(lead)
    assert group == "appointment"
    assert choices == ["front_desk", "restart_cadence", "send_booking_link"]
    assert _post(f"review/{lead}/resolve", {"choice": "continue"}).status_code == 409
    # The plain Resume refuses a flagged lead too.
    assert _post(f"leads/{lead}/cadence", {"action": "resume"}).status_code == 409
    preview = _get(f"review/{lead}").json()
    assert {o["choice"]: o["next_step"] for o in preview["options"]}["restart_cadence"].startswith(
        "Next step: the Day 0 call")


def test_front_desk_closes_it_and_a_new_stride_visit_still_books_it(denv):
    lead = _cancelled(denv, "Desk Test")
    assert _post(f"review/{lead}/resolve", {"choice": "front_desk"}).status_code == 200
    row = denv.one("select status,needs_review,cadence_state from leads where id=%s", lead)
    assert (row["status"], row["needs_review"], row["cadence_state"]) == (
        "closed_no_response", False, "terminated")
    # Re-reading the old (cancelled) file books nothing.
    denv.tick()
    assert denv.one("select status from leads where id=%s", lead)["status"] == "closed_no_response"
    # A real rebook in Stride's next file does.
    denv.drop("appointments_20261001130000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9005", "101", "7001", future_day(12), modified="2026-10-01 13:00:00")]))
    denv.db.execute("update stride_appointments set updated_at=now() where practice_id=%s and stride_id=9005",
                    (denv.practice_id,))
    denv.tick()
    denv.db.execute("update stride_appointments set updated_at=now()+interval '1 minute' "
                    "where practice_id=%s and stride_id=9005", (denv.practice_id,))
    denv.tick()
    assert denv.one("select status from leads where id=%s", lead)["status"] == "booked"


def test_restart_cadence_starts_over_from_day_0_with_the_cooldown(denv):
    lead = _cancelled(denv, "Restart Test")
    response = _post(f"review/{lead}/resolve", {"choice": "restart_cadence"})
    assert response.status_code == 200, response.text
    row = denv.one("select status,cadence_state,needs_review from leads where id=%s", lead)
    assert row["needs_review"] is False and row["cadence_state"] == "active"
    planned = denv.db.execute("select day_offset,channel from outreach_events where lead_id=%s "
                              "and status='planned' order by scheduled_for,id", (lead,)).fetchall()
    assert [(p["day_offset"], p["channel"]) for p in planned][:2] == [(0, "call"), (0, "sms")]
    assert _reviews(denv, lead)[0]["resolution"] == "restart_cadence"
    # Flagged again minutes later: a second Day 0 restart is refused (same window as the Sheet).
    denv.db.execute("update leads set needs_review=true,review_reason='Cancelled in Stride',"
                    "status='needs_attention',cadence_state='completed' where id=%s", (lead,))
    denv.db.execute("insert into lead_reviews(lead_id,code,message,source) "
                    "values(%s,'appointment_cancelled','Cancelled in Stride','stride')", (lead,))
    refused = _post(f"review/{lead}/resolve", {"choice": "restart_cadence"})
    assert refused.status_code == 409 and "last 10 minutes" in refused.json()["detail"]


def test_text_booking_link_queues_the_link_and_ends_outreach(denv):
    lead = _cancelled(denv, "Link Test")
    assert _post(f"review/{lead}/resolve", {"choice": "send_booking_link"}).status_code == 200
    note = denv.one("select notification_type,status,payload from notification_log where lead_id=%s", lead)
    assert (note["notification_type"], note["status"]) == ("sms_booking_link", "queued")
    assert note["payload"]["booking_link_url"] == "https://book.example.test"
    row = denv.one("select status,cadence_state,needs_review from leads where id=%s", lead)
    assert (row["status"], row["cadence_state"], row["needs_review"]) == ("booking_link_sent", "completed", False)
    assert not denv.db.execute("select 1 from outreach_events where lead_id=%s and status='planned'",
                               (lead,)).fetchall()


def test_no_booking_link_choice_for_a_calls_only_lead(denv):
    lead = _cancelled(denv, "Quiet Test")
    denv.db.execute("update leads set sms_disabled=true where id=%s", (lead,))
    assert _choices(lead)[1] == ["front_desk", "restart_cadence"]
    refused = _post(f"review/{lead}/resolve", {"choice": "send_booking_link"})
    assert refused.status_code == 409


def test_an_unconfirmed_agent_booking_is_front_desk_only_until_stride_shows_it(denv):
    lead = denv.lead("Unclear Test")
    slot_booking._flag(str(lead), "Stride appointment result unclear during booking; confirm in Stride")
    assert [r["code"] for r in _reviews(denv, lead)] == ["booking_unclear"]
    group, choices = _choices(lead)
    assert (group, choices) == ("booking", ["front_desk"])
    assert _post(f"review/{lead}/resolve", {"choice": "continue"}).status_code == 409
    # Stride's next file shows the visit the agent made (stamped before the flag): booked, closed.
    denv.drop("patients_20261001100000.csv", csv_bytes(
        __import__("test_stride_import_integration").PATIENT_HEADER,
        [__import__("test_stride_import_integration").patient("101", first="Unclear", last="Test")]))
    denv.drop("appointments_20261001100000.csv", csv_bytes(APPOINTMENT_HEADER, [
        appointment("9001", "101", "7001", future_day())]))
    denv.db.execute("update leads set status_changed_at=now()+interval '1 hour' where id=%s", (lead,))
    denv.tick()
    row = denv.one("select status,needs_review from leads where id=%s", lead)
    assert (row["status"], row["needs_review"]) == ("booked", False)
    assert [(r["code"], r["resolution"]) for r in _reviews(denv, lead)] == [("booking_unclear", "booked")]


def test_legacy_rows_with_stride_words_get_the_demo_choices(denv):
    """Migration 048 (production's) calls these "other": the words decide the demo group."""
    lead = denv.lead("Legacy Test")
    denv.db.execute("update leads set needs_review=true,status='needs_attention',"
                    "review_reason='Cancelled in Stride' where id=%s", (lead,))
    assert _reviews(denv, lead)[0]["code"] == "other"  # the 048 fail-safe wrote it
    assert _choices(lead) == ("appointment", ["front_desk", "restart_cadence", "send_booking_link"])


def test_a_confirmed_booking_settles_an_open_review(denv):
    lead = denv.lead("Booked Test")
    denv.db.execute("update leads set needs_review=true,status='needs_attention',"
                    "review_reason='moved to review from the board' where id=%s", (lead,))
    conn = denv.db
    mark_booked(conn, str(lead), "voice_booking")
    row = denv.one("select status,needs_review from leads where id=%s", lead)
    assert (row["status"], row["needs_review"]) == ("booked", False)
    assert _reviews(denv, lead)[0]["resolution"] == "booked"


def test_calls_only_also_stops_the_booking_confirmation_text(denv):
    lead = denv.lead("Silent Test")
    denv.db.execute("update leads set sms_disabled=true where id=%s", (lead,))
    denv.db.execute("insert into notification_log(lead_id,notification_type,channel,status,payload) "
                    "values(%s,'sms_appointment_booked','sms','queued',%s)", (lead, Jsonb({})))

    class NoProviders:
        settings = get_settings()

        def send_sms(self, *args):
            raise AssertionError("no text may go to a calls-only lead")

    process_pending_integrations(WorkflowTrace("worker", "test"), NoProviders())
    note = denv.one("select status,error from notification_log where lead_id=%s", lead)
    assert note["status"] == "skipped" and "calls only" in note["error"]
