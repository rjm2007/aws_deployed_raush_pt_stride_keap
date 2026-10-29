"""A patient who wants a different case, and transfers while a review is open (owner, 2026-10-29).

Demo call: the referral was for the ankle, the patient said "I need an appointment for my spine",
and Elliot booked the ankle anyway. Now Elliot books nothing for another case: he offers the front
desk and reports `case_change` (notes = what the patient asked for). The lead goes to Needs Review
(`case_change_asked`), with three choices: Front desk will handle, Booked by front desk, Continue
cadence. A booked lead keeps its booking and gets a card with Front desk will handle only. A
transfer while any review is open is only noted, never a way back into calls.

End to end on a real schema with the fake Sheet. Needs TEST_DATABASE_URL (a disposable local
database with the migrations applied, never the demo or production database).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_sheet_direct_integration import env  # noqa: F401 - the shared Sheet fixture

from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services.delivery import process_vapi_end_report
from rpt_agent.services.lead_stage import STAGE_SQL, stage_of
from rpt_agent.services.lead_status import (
    TRANSFER_WITH_REVIEW,
    apply_call_outcome,
    mark_booked,
    report_lead_status,
)
from rpt_agent.services.review import flag_lead_for_review
from rpt_agent.services.stride_leads import flag_cancelled_bookings

pytestmark = pytest.mark.integration

HEADERS = {"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "emma01",
           "X-Dashboard-User-Name": "Emma", "X-Dashboard-User-Role": "employee"}
CHOICES = ["front_desk", "booked_by_front_desk", "continue"]
LABELS = ["Front desk will handle", "Booked by front desk", "Continue cadence"]


@pytest.fixture
def cenv(env, monkeypatch):  # noqa: F811 - pytest fixture injection
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("TEST_MODE", "false")
    get_settings.cache_clear()
    yield env
    # Before the shared fixture deletes the leads (appointments block it).
    practices = [r["practice_id"] for r in env.leads()][:1]
    if practices:
        for table in ("appointments", "stride_appointments", "stride_patients"):
            env.db.execute(f"delete from {table} where practice_id=%s", (practices[0],))


def _get(path):
    return TestClient(app).get(f"/api/v1/dashboard/{path}", headers=HEADERS)


def _post(path, body):
    return TestClient(app).post(f"/api/v1/dashboard/{path}", json=body, headers=HEADERS)


def _started(senv, case="Ankle"):
    number = senv.start_row(case=case)
    senv.tick()
    return number, senv.cell(number, "lead_id")


def _on_call(senv, lead_id):
    """The next planned call is on the line now: (event id, call id)."""
    event = senv.db.execute(
        "update outreach_events set status='attempted',executed_at=now(),vapi_call_id=%s "
        "where id=(select id from outreach_events where lead_id=%s and channel='call' "
        "and status='planned' order by day_offset,id limit 1) returning id,vapi_call_id",
        (f"call-{uuid4().hex}", lead_id)).fetchone()
    return event["id"], event["vapi_call_id"]


def _report(lead_id, event_id, call_id, status, notes="spine", tool_call_id=None):
    return report_lead_status(WorkflowTrace("t", "test"), lead_id=lead_id, status=status,
                              call_id=call_id, event_id=event_id, notes=notes,
                              tool_call_id=tool_call_id or f"tool-{uuid4().hex[:8]}")


def _reviews(senv, lead_id):
    return senv.db.execute("select * from lead_reviews where lead_id=%s order by id", (lead_id,)).fetchall()


def _planned(senv, lead_id):
    return senv.db.execute("select count(*) as n from outreach_events where lead_id=%s "
                           "and status='planned'", (lead_id,)).fetchone()["n"]


def _sql_stage(senv, lead_id):
    return senv.db.execute("select " + STAGE_SQL + " as stage from leads l where l.id=%s",
                           (lead_id,)).fetchone()["stage"]


def _book(senv, lead_id, event_id=None):
    """Elliot booked the referral's visit on the call (the booking engine's mark_booked)."""
    lead = senv.lead(lead_id)
    senv.db.execute(
        "insert into appointments(practice_id,lead_id,state,booking_key,start_utc,end_utc) "
        "values(%s,%s,'scheduled',%s,now()+interval '3 days',now()+interval '3 days 1 hour')",
        (lead["practice_id"], lead_id, f"test-{uuid4().hex}"))
    with senv.db.transaction():
        mark_booked(senv.db, lead_id, "voice_booking")
        if event_id:
            senv.db.execute("update outreach_events set status='delivered',settled_at=now(),"
                            "settled_by='tool',outcome='booked' where id=%s", (event_id,))


# --- the tool report ---------------------------------------------------------------------------


def test_case_change_goes_to_needs_review_with_three_choices(cenv):
    senv = cenv
    number, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    assert _report(lead_id, event_id, call_id, "case_change") == "recorded"
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "needs_attention", "paused", True)
    assert _planned(senv, lead_id) == 0
    [review] = _reviews(senv, lead_id)
    assert review["code"] == "case_change_asked"
    assert review["message"] == "Patient asked about a different case: spine (referral is for Ankle)"
    event = senv.db.execute("select status,outcome from outreach_events where id=%s", (event_id,)).fetchone()
    assert (event["status"], event["outcome"]) == ("delivered", "manual")
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["title"] == "Patient wants a different case"
    assert card["review"]["group"] == "call"
    assert "“spine”" in card["review"]["reason"] and "Ankle" in card["review"]["reason"]
    assert {"label": "Patient asked for", "value": "spine"} in card["review"]["proof"]
    assert [o["choice"] for o in card["options"]] == CHOICES
    assert [o["label"] for o in card["options"]] == LABELS
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: patient wants a different case"
    assert senv.cell(number, "lead_status") == "Needs Review"
    assert stage_of({**lead, "cadence_total": 1}) == _sql_stage(senv, lead_id) == "attention"


def test_the_same_report_twice_makes_one_review(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    assert _report(lead_id, event_id, call_id, "case_change", tool_call_id="tc-1") == "recorded"
    assert _report(lead_id, event_id, call_id, "case_change", tool_call_id="tc-1") == "already recorded"
    # A second tool call with the same words (the agent repeated itself) refreshes the same card.
    assert _report(lead_id, event_id, call_id, "case_change", tool_call_id="tc-2") == "recorded"
    assert len(_reviews(senv, lead_id)) == 1


def test_a_transfer_after_the_case_change_keeps_the_review(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "case_change")
    _report(lead_id, event_id, call_id, "transferred_human", notes="connected to the front desk")
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "needs_attention", "paused", True)
    assert _reviews(senv, lead_id)[0]["resolved_at"] is None
    assert _planned(senv, lead_id) == 0
    history = senv.db.execute("select from_status,to_status,reason from lead_status_history "
                              "where lead_id=%s and reason like %s", (lead_id, f"{TRANSFER_WITH_REVIEW}%")).fetchone()
    assert history == {"from_status": "needs_attention", "to_status": "needs_attention",
                       "reason": f"{TRANSFER_WITH_REVIEW}. connected to the front desk"}


def test_a_transfer_before_the_case_change_still_ends_in_review(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "transferred_human")
    assert senv.lead(lead_id)["status"] == "transferred_human"
    _report(lead_id, event_id, call_id, "case_change")
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "needs_attention", "paused", True)


# --- the three choices ---------------------------------------------------------------------------


def test_booked_by_front_desk_books_the_lead_like_the_sheet(cenv):
    senv = cenv
    number, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "case_change")
    senv.tick()
    option = next(o for o in _get(f"review/{lead_id}").json()["options"]
                  if o["choice"] == "booked_by_front_desk")
    assert option["effect"].startswith("The lead moves to Booked (Booked by front desk)")
    assert "Stride's next file" in option["next_step"]
    assert option["done"] == "Lead marked Booked: booked by front desk."
    response = _post(f"review/{lead_id}/resolve", {"choice": "booked_by_front_desk"})
    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Booked by front desk"
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"], lead["status_reason"]) == (
        "booked", "completed", False, "Booked by front desk")
    assert _planned(senv, lead_id) == 0
    assert not senv.db.execute("select 1 from outreach_events where lead_id=%s and status='skipped' "
                               "and failure_reason='paused_for_review'", (lead_id,)).fetchone()
    [review] = _reviews(senv, lead_id)
    assert (review["resolution"], review["resolved_by_name"]) == ("booked_by_front_desk", "Emma")
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Booked by front desk"
    assert senv.cell(number, "lead_status") == "Booked"
    detail = _get(f"leads/{lead_id}").json()
    assert any(e["title"] == "Resolved by Emma: Booked by front desk." for e in detail["activity"])
    history = senv.db.execute("select to_status,reason from lead_status_history where lead_id=%s "
                              "and source='dashboard'", (lead_id,)).fetchone()
    assert history == {"to_status": "booked", "reason": "Review resolved by Emma: Booked by front desk"}
    # Booked is final: the card cannot be used again, and the board card names how it was booked.
    assert _post(f"review/{lead_id}/resolve", {"choice": "continue"}).status_code == 409
    snapshot = _get("snapshot").json()
    card = next(lead for lead in snapshot["leads"] if lead["id"] == lead_id)
    assert (card["stage"], card["next_step"]) == ("booked", "Booked by front desk")


def test_front_desk_closes_the_lead(cenv):
    senv = cenv
    number, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "case_change")
    assert _post(f"review/{lead_id}/resolve", {"choice": "front_desk"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "closed_no_response", "terminated", False)
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Closed: handled by front desk"


def test_continue_cadence_puts_the_steps_back(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "case_change")
    response = _post(f"review/{lead_id}/resolve", {"choice": "continue"})
    assert response.status_code == 200 and response.json()["label"] == "Continue cadence"
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("in_progress", "active", False)
    assert _planned(senv, lead_id) > 0


# --- already booked, then "also my spine" ---------------------------------------------------------


def test_booked_lead_keeps_its_booking_and_gets_a_front_desk_only_card(cenv):
    senv = cenv
    number, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _book(senv, lead_id, event_id)
    assert _report(lead_id, event_id, call_id, "case_change", notes="back") == "recorded"
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("booked", "completed", True)
    event = senv.db.execute("select outcome from outreach_events where id=%s", (event_id,)).fetchone()
    assert event["outcome"] == "booked"  # the booking settled the call; the card does not undo it
    # Booked column, and in the Needs Review page's list (every needs_review lead is in the snapshot).
    assert stage_of({**lead, "cadence_total": 1}) == _sql_stage(senv, lead_id) == "booked"
    snapshot = _get("snapshot").json()
    card = next(row for row in snapshot["leads"] if row["id"] == lead_id)
    assert (card["stage"], card["needs_review"]) == ("booked", True)
    assert card["review"]["code"] == "case_change_asked"
    assert [o["choice"] for o in card["review"]["options"]] == ["front_desk"]
    preview = _get(f"review/{lead_id}").json()
    assert "booked for Ankle" in preview["review"]["reason"] and "“back”" in preview["review"]["reason"]
    [option] = preview["options"]
    assert option["effect"] == "The review closes and the lead stays Booked"
    assert option["done"] == "Review closed. The lead stays Booked."
    for refused in ("continue", "booked_by_front_desk", "restart_cadence"):
        assert _post(f"review/{lead_id}/resolve", {"choice": refused}).status_code == 409
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Booked; needs review: patient wants a different case"
    assert senv.cell(number, "lead_status") == "Booked"
    # A transfer on a booked lead changes nothing (booked is final).
    assert _report(lead_id, event_id, call_id, "transferred_human") == "already booked"

    assert _post(f"review/{lead_id}/resolve", {"choice": "front_desk"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("booked", "completed", False)
    assert _reviews(senv, lead_id)[0]["resolution"] == "front_desk"
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Booked"
    assert senv.cell(number, "lead_status") == "Booked"


def test_a_booking_after_the_case_change_keeps_the_card_open(cenv):
    """mark_booked closes every other review, never the case change one: the booking is for the
    referral's case, not the one the patient asked about."""
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "case_change")
    with senv.db.transaction():
        flag_lead_for_review(senv.db, lead_id, "Stride booking failed (test)", code="booking_failed",
                             source="booking")
    _book(senv, lead_id)
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("booked", True)
    assert lead["review_reason"].startswith("Patient asked about a different case")
    reviews = {r["code"]: r for r in _reviews(senv, lead_id)}
    assert reviews["booking_failed"]["resolution"] == "booked"
    assert reviews["case_change_asked"]["resolved_at"] is None


def test_a_stride_cancel_is_still_seen_behind_a_case_change_card(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _book(senv, lead_id, event_id)
    _report(lead_id, event_id, call_id, "case_change")
    practice = senv.lead(lead_id)["practice_id"]
    stride_id = uuid4().int % 1_000_000_000
    senv.db.execute("insert into stride_patients(practice_id,stride_id,lead_id) values(%s,%s,%s)",
                    (practice, stride_id, lead_id))
    senv.db.execute(
        "insert into stride_appointments(practice_id,stride_id,patient_stride_id,status,start_utc,"
        "updated_at) values(%s,%s,%s,'Cancelled',%s,now()+interval '1 second')",
        (practice, stride_id, stride_id, datetime.now(UTC) + timedelta(days=3)))
    with senv.db.transaction():
        assert flag_cancelled_bookings(senv.db, practice) == 1
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("needs_attention", True)
    assert {r["code"] for r in _reviews(senv, lead_id) if r["resolved_at"] is None} == {
        "case_change_asked", "appointment_cancelled"}


# --- a transfer never clears another review --------------------------------------------------------


def _booking_review(senv, lead_id):
    """A booking the agent could not make on the call (FOLLOW_UP, PATIENT_EXISTS, ...)."""
    with senv.db.transaction():
        flag_lead_for_review(senv.db, lead_id, "Stride says this patient already exists; staff must "
                             "book manually", code="booking_patient_exists", source="booking")


@pytest.mark.parametrize("status", ["transferred_human", "booking_link_and_transfer"])
def test_a_transfer_after_a_booking_review_keeps_it(cenv, status):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _booking_review(senv, lead_id)
    senv.db.execute("update practice_settings set booking_link_url='https://book.example.test' "
                    "where practice_id=%s", (senv.lead(lead_id)["practice_id"],))
    assert _report(lead_id, event_id, call_id, status) == "recorded"
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "needs_attention", "paused", True)
    assert _reviews(senv, lead_id)[0]["resolved_at"] is None
    assert _planned(senv, lead_id) == 0


def test_a_transfer_outcome_from_the_call_report_keeps_the_review(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, _ = _on_call(senv, lead_id)
    _booking_review(senv, lead_id)
    assert apply_call_outcome(WorkflowTrace("t", "test"), lead_id=lead_id, event_id=event_id,
                              outcome="transferred", source="webhook") == "recorded"
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "needs_attention", "paused", True)


def _end_report(lead_id, event_id, call_id, *, ended, result, patient_lines):
    messages = [{"role": "bot", "message": "Hi, am I speaking with Asha Rao?"},
                {"role": "user", "message": "Yes."},
                {"role": "bot", "message": "It's Elliot from Rausch Physical Therapy. Do you have a moment?"}]
    messages += [{"role": "user", "message": line} for line in patient_lines]
    return {"message": {
        "type": "end-of-call-report", "endedReason": ended,
        "call": {"id": call_id, "assistantOverrides": {"variableValues": {
            "lead_id": lead_id, "outreach_event_id": str(event_id)}}},
        "artifact": {"messages": messages,
                     "structuredOutputs": {"so-1": {"name": "Call outcome", "result": result}}},
    }}


def test_the_end_of_call_transfer_keeps_an_open_review(cenv):
    """Vapi ended the call as forwarded and the summary says transferred: noted only."""
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _booking_review(senv, lead_id)
    process_vapi_end_report(WorkflowTrace("t", "test"), _end_report(
        lead_id, event_id, call_id, ended="assistant-forwarded-call",
        result={"status": "transferred_human", "summary": "Connected to the front desk."},
        patient_lines=["Can I talk to someone?"]))
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == (
        "needs_attention", "paused", True)
    event = senv.db.execute("select outcome from outreach_events where id=%s", (event_id,)).fetchone()
    assert event["outcome"] == "transferred"


# --- the call was cut before Elliot reported --------------------------------------------------------


def test_a_case_change_from_the_call_summary_opens_the_card(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    process_vapi_end_report(WorkflowTrace("t", "test"), _end_report(
        lead_id, event_id, call_id, ended="customer-ended-call",
        result={"status": "case_change", "requested_case": "spine",
                "summary": "Patient wants a spine appointment."},
        patient_lines=["I need an appointment for my spine."]))
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("needs_attention", True)
    [review] = _reviews(senv, lead_id)
    assert (review["code"], review["details"]["asked"]) == ("case_change_asked", "spine")


def test_a_case_change_summary_without_the_patient_speaking_is_no_answer(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    report = _end_report(lead_id, event_id, call_id, ended="customer-ended-call",
                         result={"status": "case_change", "requested_case": "spine"},
                         patient_lines=[])
    report["message"]["artifact"]["messages"] = report["message"]["artifact"]["messages"][:1]
    process_vapi_end_report(WorkflowTrace("t", "test"), report)
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("in_progress", False)
    assert _reviews(senv, lead_id) == []


def test_a_case_change_tool_report_is_not_overridden_by_the_summary(cenv):
    senv = cenv
    _, lead_id = _started(senv)
    event_id, call_id = _on_call(senv, lead_id)
    _report(lead_id, event_id, call_id, "case_change")
    process_vapi_end_report(WorkflowTrace("t", "test"), _end_report(
        lead_id, event_id, call_id, ended="customer-ended-call",
        result={"status": "declined", "summary": "Not interested."},
        patient_lines=["No thanks, not interested."]))
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("needs_attention", True)
