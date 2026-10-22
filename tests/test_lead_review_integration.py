"""Review reasons and resolve-with-a-choice, end to end on a real schema (fake Sheet).

Incident 2026-10-21: a lead flagged for a Name edit and one flagged for a Day 0 text that failed
with Twilio 30006 were "resumed"; the review stayed open, the worker sent nothing, and nobody could
see what had changed or who decided what. Every flag now has a review row with a code, staff pick
a choice (or the lead stays paused), and both are recorded.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_sheet_direct_integration import _phone, env  # noqa: F401 - the shared Sheet fixture

from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services.delivery import apply_twilio_message_status
from rpt_agent.services.lead_status import report_lead_status
from rpt_agent.worker import CLAIM_SQL, run_safety_checks

pytestmark = pytest.mark.integration

HEADERS = {"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "emma01",
           "X-Dashboard-User-Name": "Emma", "X-Dashboard-User-Role": "employee"}


@pytest.fixture
def review_env(env, monkeypatch):  # noqa: F811 - pytest fixture injection
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    # Real cadence days: a local .env with TEST_MODE=true would squeeze them into minutes.
    monkeypatch.setenv("TEST_MODE", "false")
    get_settings.cache_clear()
    return env


def _client():
    return TestClient(app)


def _get(path):
    return _client().get(f"/api/v1/dashboard/{path}", headers=HEADERS)


def _post(path, body):
    return _client().post(f"/api/v1/dashboard/{path}", json=body, headers=HEADERS)


def _started(senv):
    number = senv.start_row()
    senv.tick()
    return number, senv.cell(number, "lead_id")


def _reviews(senv, lead_id):
    return senv.db.execute(
        "select * from lead_reviews where lead_id=%s order by id", (lead_id,)).fetchall()


def _events(senv, lead_id):
    return senv.db.execute(
        "select id,channel,day_offset,status,failure_reason,scheduled_for from outreach_events "
        "where lead_id=%s order by scheduled_for,id", (lead_id,)).fetchall()


def _fail_day0_text(senv, lead_id, error_code):
    """The Day 0 text went out and Twilio reported it undelivered (as on 2026-10-21)."""
    event = senv.db.execute(
        "update outreach_events set status='attempted',executed_at=now(),provider='twilio',"
        "provider_ref=%s where id=(select id from outreach_events where lead_id=%s and channel='sms' "
        "order by day_offset,id limit 1) returning id,provider_ref",
        (f"SM{uuid4().hex}", lead_id)).fetchone()
    senv.db.execute(
        "insert into sms_messages(lead_id,outreach_event_id,direction,body,occurred_at,"
        "delivery_status,provider_message_id) values(%s,%s,'outbound','Hi',now(),'queued',%s)",
        (lead_id, event["id"], event["provider_ref"]))
    with senv.db.transaction():
        apply_twilio_message_status(senv.db, {"MessageSid": event["provider_ref"],
                                             "MessageStatus": "undelivered",
                                             "ErrorCode": error_code})
    return event["id"]


# --- name changed -----------------------------------------------------------------------------


def test_name_change_card_shows_old_and_new_and_use_new_name_resumes_fully(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert lead["needs_review"] is True and lead["status"] == "needs_attention"
    assert senv.cell(number, "outcome_status") == "Needs review: name changed"
    [review] = _reviews(senv, lead_id)
    assert review["code"] == "name_changed" and review["source"] == "sheet"
    assert review["details"] == {"old_name": "Asha Rao", "new_name": "Asha Kumar"}

    card = _get(f"review/{lead_id}").json()
    assert card["review"]["group"] == "name"
    assert {"label": "Old name", "value": "Asha Rao"} in card["review"]["proof"]
    assert {"label": "New name", "value": "Asha Kumar"} in card["review"]["proof"]
    choices = [option["choice"] for option in card["options"]]
    assert choices == ["use_new_name", "keep_old_name", "different_person", "front_desk"]
    use_new = next(option for option in card["options"] if option["choice"] == "use_new_name")
    assert "Name becomes Asha Kumar" in use_new["confirm"] and "Next step: Day" in use_new["confirm"]

    response = _post(f"review/{lead_id}/resolve", {"choice": "use_new_name", "note": "Called her"})
    assert response.status_code == 200, response.text
    lead = senv.lead(lead_id)
    assert (lead["full_name"], lead["first_name"], lead["last_name"]) == ("Asha Kumar", "Asha", "Kumar")
    assert (lead["needs_review"], lead["status"], lead["cadence_state"]) == (False, "in_progress", "active")
    assert all(event["status"] == "planned" for event in _events(senv, lead_id))
    [review] = _reviews(senv, lead_id)
    assert (review["resolution"], review["resolved_by"], review["resolved_by_name"], review["note"]) == (
        "use_new_name", "emma01", "Emma", "Called her")
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Resolved: new name used"
    assert senv.cell(number, "action") == "Start cadence"
    assert senv.cell(number, "lead_status") == "In cadence"
    # Another edit later: the name now matches, so nothing is flagged again.
    senv.sheet.set(number, "email", "kumar@example.test")
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is False and len(_reviews(senv, lead_id)) == 1


def test_keep_old_name_is_remembered_so_the_next_sync_does_not_flag_again(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha  Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    lead = senv.lead(lead_id)
    assert lead["full_name"] == "Asha Rao" and lead["sheet_name_accepted"] == "Asha Kumar"
    assert lead["needs_review"] is False
    senv.sheet.set(number, "email", "kept@example.test")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert lead["needs_review"] is False and lead["email"] == "kept@example.test"
    assert lead["full_name"] == "Asha Rao"  # the accepted Sheet name is never applied silently
    assert len(_reviews(senv, lead_id)) == 1


def test_different_person_closes_for_good(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Someone Else")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "different_person"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("invalid_phone", "terminated", False)
    assert not any(event["status"] == "planned" for event in _events(senv, lead_id))
    # No way back on this lead: resume, In cadence and New all refuse.
    resume = _post(f"leads/{lead_id}/cadence", {"action": "resume"})
    assert resume.status_code == 409 and "new row" in resume.json()["detail"]
    assert _post(f"leads/{lead_id}/stage", {"stage": "cadence"}).status_code == 409
    assert _post(f"leads/{lead_id}/stage", {"stage": "new"}).status_code == 409
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Closed: different person, add a new row"
    assert senv.cell(number, "lead_status") == "Closed"
    snapshot = _get("snapshot").json()
    card = next(lead for lead in snapshot["leads"] if lead["id"] == lead_id)
    assert card["stage"] == "closed" and card["next_step"] == "Different person: add a new row"


# --- phone changed ------------------------------------------------------------------------------


def test_phone_change_closes_the_lead_at_once_and_never_resumes(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    old_phone = senv.lead(lead_id)["phone_e164"]
    new_phone = _phone()
    senv.sheet.set(number, "phone", new_phone)
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("invalid_phone", "terminated", False)
    assert lead["phone_e164"] == old_phone
    assert lead["status_reason"].startswith(f"Phone number changed in the Sheet from …{old_phone[-4:]} "
                                            f"to …{new_phone[-4:]} on ")
    assert lead["status_reason"].endswith("Add the new number as a new row.")
    assert not any(event["status"] == "planned" for event in _events(senv, lead_id))
    [review] = _reviews(senv, lead_id)
    assert review["code"] == "phone_changed" and review["resolution"] == "close_phone_changed"
    assert review["resolved_at"] is not None
    assert new_phone not in str(review["details"])  # only the last four digits are kept
    assert senv.cell(number, "outcome_status") == "Phone changed: add a new row"
    assert senv.cell(number, "lead_status") == "Closed"
    assert senv.cell(number, "action") == "Start cadence"
    assert _post(f"leads/{lead_id}/cadence", {"action": "resume"}).status_code == 409
    assert _post(f"leads/{lead_id}/stage", {"stage": "new"}).status_code == 409
    # Later polls (and another edit) neither duplicate the record nor reopen the lead.
    senv.sheet.set(number, "email", "again@example.test")
    senv.tick()
    senv.tick()
    assert len(_reviews(senv, lead_id)) == 1 and senv.lead(lead_id)["status"] == "invalid_phone"
    # The Sheet's own Restart is refused too.
    senv.sheet.set(number, "action", "Restart cadence")
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    assert senv.lead(lead_id)["cadence_state"] == "terminated"


# --- text failed --------------------------------------------------------------------------------


def test_landline_text_failure_offers_calls_only_and_texts_never_go_out_again(review_env, monkeypatch):
    senv = review_env
    number, lead_id = _started(senv)
    event_id = _fail_day0_text(senv, lead_id, "30006")
    lead = senv.lead(lead_id)
    assert lead["needs_review"] is True and lead["status"] == "needs_attention"
    # The 048 fail-safe and the application describe the same failed step: one review row.
    [review] = _reviews(senv, lead_id)
    assert (review["code"], review["outreach_event_id"]) == ("sms_failed", event_id)
    assert review["details"]["error_code"] == "30006"
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: text failed (landline)"
    assert senv.cell(number, "lead_status") == "Needs Review"

    card = _get(f"review/{lead_id}").json()
    assert card["review"]["landline"] is True
    assert "can't receive texts (likely a landline)" in card["review"]["reason"]
    choices = [option["choice"] for option in card["options"]]
    assert choices == ["calls_only", "front_desk"]  # trying again cannot work on a landline
    calls_only = card["options"][0]
    assert calls_only["confirm"].startswith("Continue with calls only for Asha Rao. Texts off. Next step: Day")
    assert "call" in calls_only["next_step"]
    refused = _post(f"review/{lead_id}/resolve", {"choice": "retry_texts"})
    assert refused.status_code == 409

    assert _post(f"review/{lead_id}/resolve", {"choice": "calls_only"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["status"], lead["cadence_state"], lead["sms_disabled"]) == (
        False, "in_progress", "active", True)
    events = _events(senv, lead_id)
    assert all(e["status"] == "skipped" and e["failure_reason"] == "texts off (calls only)"
               for e in events if e["channel"] == "sms" and e["id"] != event_id)
    assert any(e["status"] == "planned" and e["channel"] == "call" for e in events)

    # Everything due now: the claim returns calls only, never a text for this lead.
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    senv.db.execute("update leads set is_test=true where id=%s", (lead_id,))
    senv.db.execute("update outreach_events set scheduled_for=now()-interval '1 minute',status='planned',"
                   "failure_reason=null where lead_id=%s and id<>%s", (lead_id, event_id))
    with senv.db.transaction():
        senv.db.execute("select set_config('rpt.worker_claim_protocol','v1',true)")
        claimed = senv.db.execute(CLAIM_SQL, {"limit": 500, "test_mode": True}).fetchall()
        mine = [row for row in claimed if str(row["lead_id"]) == lead_id]
        assert mine and all(row["channel"] == "call" for row in mine)
        sms_due = senv.db.execute(
            "select count(*) as n from outreach_events where lead_id=%s and channel='sms' "
            "and status='planned'", (lead_id,)).fetchone()["n"]
        assert sms_due > 0  # texts were due and still not claimed
        raise psycopg.Rollback()  # leave the claims undone

    senv.tick()
    assert senv.cell(number, "outcome_status") == "Resolved: calls only"
    assert senv.cell(number, "message_outcome") == "Calls only (texts off)"


def test_restart_keeps_texts_off(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30006")
    _post(f"review/{lead_id}/resolve", {"choice": "calls_only"})
    senv.db.execute("update outreach_events set status='delivered' where lead_id=%s "
                   "and status in ('attempted','in_flight')", (lead_id,))
    senv.sheet.set(number, "action", "Restart cadence")
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Cadence restarted"
    fresh = [e for e in _events(senv, lead_id) if e["failure_reason"] != "paused_for_review"
             and e["status"] in ("planned", "skipped")]
    assert all(e["status"] == "skipped" for e in fresh if e["channel"] == "sms")
    assert any(e["status"] == "planned" for e in fresh if e["channel"] == "call")


def test_temporary_text_failure_can_be_tried_again(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    failed_id = _fail_day0_text(senv, lead_id, "30003")
    card = _get(f"review/{lead_id}").json()
    assert [o["choice"] for o in card["options"]] == ["calls_only", "retry_texts", "front_desk"]
    assert "switched off" in card["review"]["reason"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "retry_texts"}).status_code == 200
    retry = senv.db.execute(
        "select * from outreach_events where lead_id=%s and channel='sms' and status='planned' "
        "and id<>%s order by scheduled_for limit 1", (lead_id, failed_id)).fetchone()
    assert retry["scheduled_for"] <= datetime.now(UTC) + timedelta(seconds=5)
    assert senv.lead(lead_id)["sms_disabled"] is False


# --- call problems, technical, front desk -------------------------------------------------------


def _wrong_person(senv, lead_id):
    event = senv.db.execute(
        "update outreach_events set status='attempted',executed_at=now(),vapi_call_id=%s "
        "where id=(select id from outreach_events where lead_id=%s and channel='call' "
        "order by day_offset,id limit 1) returning id,vapi_call_id",
        (f"call-{uuid4().hex}", lead_id)).fetchone()
    report_lead_status(WorkflowTrace("t", "test"), lead_id=lead_id, status="wrong_person",
                       call_id=event["vapi_call_id"], event_id=event["id"],
                       notes="Said this is Bob's number")
    return event


def test_wrong_person_number_is_right_continues(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    _wrong_person(senv, lead_id)
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("invalid_phone", True)
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["code"] == "wrong_person"
    assert "Bob's number" in card["review"]["reason"]
    assert [o["choice"] for o in card["options"]] == ["number_ok", "wrong_number", "front_desk"]
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: wrong number"
    assert _post(f"review/{lead_id}/resolve", {"choice": "number_ok"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("in_progress", "active", False)
    assert any(e["status"] == "planned" for e in _events(senv, lead_id))


def test_wrong_number_close(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _wrong_person(senv, lead_id)
    assert _post(f"review/{lead_id}/resolve", {"choice": "wrong_number"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"]) == ("invalid_phone", "terminated")
    assert _post(f"leads/{lead_id}/stage", {"stage": "cadence"}).status_code == 409


def test_unconfirmed_dispatch_continue_skips_that_step_only(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    with senv.db.transaction():  # migration 025: only a worker using the claim protocol may claim
        senv.db.execute("select set_config('rpt.worker_claim_protocol','v1',true)")
        stuck = senv.db.execute(
            "update outreach_events set status='in_flight' where id=(select id from outreach_events "
            "where lead_id=%s order by scheduled_for,id limit 1) returning id", (lead_id,)).fetchone()
    senv.db.execute("alter table outreach_events disable trigger trg_outreach_updated")
    try:  # the worker died 20 minutes ago
        senv.db.execute("update outreach_events set updated_at=now()-interval '20 minutes' where id=%s",
                       (stuck["id"],))
    finally:
        senv.db.execute("alter table outreach_events enable trigger trg_outreach_updated")
    run_safety_checks(WorkflowTrace("t", "test"))
    [review] = _reviews(senv, lead_id)
    assert (review["code"], review["outreach_event_id"]) == ("dispatch_unconfirmed", stuck["id"])
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["group"] == "technical"
    assert [o["choice"] for o in card["options"]] == ["continue", "front_desk"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "continue"}).status_code == 200
    events = {e["id"]: e for e in _events(senv, lead_id)}
    assert events[stuck["id"]]["status"] == "unknown"  # never sent twice
    assert sum(e["status"] == "planned" for e in events.values()) == len(events) - 1


def test_front_desk_closes_with_the_reason(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30007")
    assert _post(f"review/{lead_id}/resolve", {"choice": "front_desk"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["status_reason"]) == (
        "closed_no_response", "terminated", "Handled by front desk")
    assert not any(e["status"] == "planned" for e in _events(senv, lead_id))
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Closed: handled by front desk"
    assert senv.cell(number, "lead_status") == "Closed"
    detail = _get(f"leads/{lead_id}").json()["lead"]
    assert detail["stage"] == "closed" and detail["next_step"] == "Handled by front desk"


# --- no shortcuts around a review ---------------------------------------------------------------


def test_resume_and_in_cadence_refuse_a_flagged_lead(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30006")
    resume = _post(f"leads/{lead_id}/cadence", {"action": "resume"})
    assert resume.status_code == 409 and "Resolve review" in resume.json()["detail"]
    board = _post(f"leads/{lead_id}/stage", {"stage": "cadence"})
    assert board.status_code == 409
    assert senv.lead(lead_id)["needs_review"] is True
    # A free-text "resolution" (the old review queue) is no longer a way through.
    assert _post(f"review/{lead_id}/resolve", {"resolution": "Reviewed by dashboard operator"}).status_code == 422
    assert senv.lead(lead_id)["needs_review"] is True


def test_board_close_on_a_flagged_lead_records_who(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30006")
    assert _post(f"leads/{lead_id}/stage", {"stage": "closed"}).status_code == 200
    [review] = _reviews(senv, lead_id)
    assert (review["resolution"], review["resolved_by_name"]) == ("board_closed", "Emma")


def test_history_and_activity_show_flag_and_resolution(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30006")
    _post(f"review/{lead_id}/resolve", {"choice": "calls_only", "note": "Front desk confirmed landline"})
    history = [row["reason"] for row in senv.db.execute(
        "select reason from lead_status_history where lead_id=%s order by id", (lead_id,))]
    assert any(reason.startswith("Flagged for review: cadence SMS was not delivered") for reason in history)
    assert "Review resolved by Emma: Continue with calls only. Note: Front desk confirmed landline" in history
    audit = senv.db.execute("select actor_name,metadata from dashboard_audit_log where lead_id=%s "
                           "and action='review.resolved'", (lead_id,)).fetchone()
    assert audit["actor_name"] == "Emma" and audit["metadata"]["choice"] == "calls_only"
    titles = [entry["title"] for entry in _get(f"leads/{lead_id}").json()["activity"]]
    assert "Flagged: Text not delivered" in titles
    assert ("Resolved by Emma: Continue with calls only. Note: Front desk confirmed landline"
            in titles)


def test_resume_brings_at_most_one_overdue_step_per_channel(review_env):
    """A lead left in review for days must not fire every overdue step at once."""
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30003")
    # Back-date the whole schedule and the flag: everything was due long ago.
    senv.db.execute("update outreach_events set scheduled_for=scheduled_for-interval '10 days' "
                   "where lead_id=%s", (lead_id,))
    senv.db.execute("update lead_reviews set flagged_at=now()-interval '2 days' where lead_id=%s", (lead_id,))
    assert _post(f"review/{lead_id}/resolve", {"choice": "calls_only"}).status_code == 200
    soon = datetime.now(UTC) + timedelta(minutes=10)
    due = [e for e in _events(senv, lead_id) if e["status"] == "planned" and e["scheduled_for"] <= soon]
    assert len(due) == 1 and due[0]["channel"] == "call"
    planned = [e["scheduled_for"] for e in _events(senv, lead_id) if e["status"] == "planned"]
    assert planned == sorted(planned) and len(planned) >= 2
    assert planned[1] - planned[0] >= timedelta(days=1)  # the cadence keeps its spacing


# --- fail-safe and existing flagged leads ---------------------------------------------------------


def test_a_flag_written_by_sql_alone_still_gets_a_review_row(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    senv.db.execute("update leads set needs_review=true,review_reason='cadence SMS was not delivered',"
                   "status='needs_attention',cadence_state='paused' where id=%s", (lead_id,))
    [review] = _reviews(senv, lead_id)
    assert (review["code"], review["source"]) == ("sms_failed", "db")
    senv.db.execute("update leads set needs_review=false where id=%s", (lead_id,))
    [review] = _reviews(senv, lead_id)
    assert review["resolved_at"] is not None and review["resolution"] == "cleared"


def test_migration_is_rerunnable_and_backfills_old_flags_with_a_code(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30006")
    senv.db.execute("delete from lead_reviews where lead_id=%s", (lead_id,))  # as before 048
    senv.db.execute(Path("supabase/migrations/048_lead_reviews.sql").read_text(encoding="utf-8"))
    [review] = _reviews(senv, lead_id)
    assert (review["code"], review["source"], review["details"]["error_code"]) == ("sms_failed", "legacy", "30006")
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["landline"] is True
    assert [o["choice"] for o in card["options"]] == ["calls_only", "front_desk"]


def test_an_old_free_text_reason_falls_back_to_continue_or_front_desk(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    senv.db.execute("update leads set needs_review=true,review_reason='Patient asked a question',"
                   "status='needs_attention',cadence_state='paused' where id=%s", (lead_id,))
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["group"] == "other"
    assert [o["choice"] for o in card["options"]] == ["continue", "front_desk"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "continue"}).status_code == 200
    assert senv.lead(lead_id)["status"] == "in_progress"


def test_snapshot_card_carries_the_review(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    senv.db.execute("update lead_reviews set details=%s where lead_id=%s", (Jsonb({}), lead_id))
    _fail_day0_text(senv, lead_id, "30006")
    card = next(lead for lead in _get("snapshot").json()["leads"] if lead["id"] == lead_id)
    assert card["stage"] == "attention"
    assert card["review"]["code"] == "sms_failed" and card["review"]["landline"] is True
    assert card["review"]["group_label"] == "Text failed"
    assert card["texts_off"] is False


# --- QA fixes, 2026-10-22 ---------------------------------------------------------------------------


def test_legacy_name_review_keep_old_name_accepts_the_sheet_name_once(review_env):
    """The D. S. lead: flagged before 048 with only "Name changed; needs review". Keep the old name,
    then an unrelated edit must not flag again; a later, genuinely different name still does."""
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Dana Smyth")
    senv.tick()
    senv.tick()
    senv.db.execute("update lead_reviews set details='{}'::jsonb,source='legacy' where lead_id=%s", (lead_id,))
    card = _get(f"review/{lead_id}").json()
    assert {"label": "Old name", "value": "Asha Rao"} in card["review"]["proof"]
    assert "use_new_name" not in [o["choice"] for o in card["options"]]
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 409
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    senv.sheet.set(number, "email", "dana@example.test")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["full_name"], lead["sheet_name_accepted"]) == (
        False, "Asha Rao", "Dana Smyth")
    senv.sheet.set(number, "name", "Someone Else")
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is True


def test_booked_lead_whose_phone_changed_can_never_restart_on_the_old_number(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    old = senv.cell(number, "phone")
    senv.sheet.set(number, "action", "Booked")
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    senv.sheet.set(number, "phone", _phone())
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["status"] == "booked"  # still Booked for staff
    for move in ("new", "cadence"):
        refused = _post(f"leads/{lead_id}/stage", {"stage": move})
        assert refused.status_code == 409 and "phone number changed" in refused.json()["detail"]
    # The old number back in the row and Restart picked: refused too.
    senv.sheet.set(number, "phone", old)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    assert senv.cell(number, "outcome_status").startswith("Error: The phone number changed")
    assert not any(e["status"] == "planned" for e in _events(senv, lead_id))


def test_a_typed_command_is_never_overwritten_by_a_review_text(review_env, monkeypatch):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    monkeypatch.setenv("SHEET_SETTLE_SECONDS", "60")
    get_settings.cache_clear()
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    senv.sheet.set(number, "action", "Booked")  # typed before the next poll, still settling
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    assert (senv.cell(number, "action"), senv.cell(number, "outcome_status")) == ("Booked", "")


def test_reviews_are_settled_one_at_a_time_so_a_landline_stays_texts_off(review_env):
    """QA 2026-10-22: a 30006 text failure and a name change were open together; "use new name"
    settled both and texts resumed to a landline."""
    senv = review_env
    number, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30006")
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["code"] == "sms_failed"  # the oldest first
    assert [item["title"] for item in card["review"]["also"]] == ["Name changed in the Sheet"]
    assert "next open review" in card["options"][0]["next_step"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 409
    first = _post(f"review/{lead_id}/resolve", {"choice": "calls_only"})
    assert first.status_code == 200 and first.json()["remaining_reviews"] == 1
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["sms_disabled"], lead["cadence_state"]) == (True, True, "paused")
    assert not any(e["status"] == "planned" for e in _events(senv, lead_id))
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: name changed"
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["sms_disabled"], lead["full_name"]) == (False, True, "Asha Kumar")
    assert not any(e["status"] == "planned" and e["channel"] == "sms" for e in _events(senv, lead_id))
    assert any(e["status"] == "planned" and e["channel"] == "call" for e in _events(senv, lead_id))


def test_a_front_desk_lead_cannot_be_resumed(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30007")
    _post(f"review/{lead_id}/resolve", {"choice": "front_desk"})
    resume = _post(f"leads/{lead_id}/cadence", {"action": "resume"})
    assert resume.status_code == 409 and "handled by front desk" in resume.json()["detail"]
    assert senv.lead(lead_id)["cadence_state"] == "terminated"


# --- QA round 2 -------------------------------------------------------------------------------------


def test_legacy_keep_old_name_never_accepts_a_different_name_typed_later(review_env):
    """The keep-old-name marker accepted whatever name came next; a different person typed over
    the row passed silently. Only the Name the worker last read is accepted now."""
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Dana Smyth")
    senv.tick()
    senv.tick()
    senv.db.execute("update lead_reviews set details='{}'::jsonb,source='legacy' where lead_id=%s", (lead_id,))
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    assert senv.lead(lead_id)["sheet_name_accepted"] == "Dana Smyth"
    senv.sheet.set(number, "name", "Completely Other Person")
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is True


def test_the_next_sheet_read_fills_a_legacy_name_review(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Dana Smyth")
    senv.tick()
    senv.tick()
    # As before 048: no new name on the review, and no Sheet name recorded yet.
    senv.db.execute("update lead_reviews set details='{}'::jsonb,source='legacy' where lead_id=%s", (lead_id,))
    senv.db.execute("update sheet_row_state set sheet_name=null where lead_id=%s", (lead_id,))
    refused = _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"})
    assert refused.status_code == 409 and "not recorded yet" in refused.json()["detail"]
    senv.tick()  # a full read records the row's Name and fills the review, without re-flagging
    [review] = _reviews(senv, lead_id)
    assert review["details"] == {"old_name": "Asha Rao", "new_name": "Dana Smyth"} and not review["resolved_at"]
    card = _get(f"review/{lead_id}").json()
    assert card["options"][0]["choice"] == "use_new_name"
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 200
    assert senv.lead(lead_id)["full_name"] == "Dana Smyth"


def test_an_old_phone_review_closed_by_front_desk_still_blocks_the_old_number(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    senv.db.execute("update leads set needs_review=true,review_reason='Phone number changed; needs review',"
                    "status='needs_attention',cadence_state='paused' where id=%s", (lead_id,))
    assert _post(f"review/{lead_id}/resolve", {"choice": "front_desk"}).status_code == 200
    refused = _post(f"leads/{lead_id}/stage", {"stage": "new"})
    assert refused.status_code == 409 and "phone number changed" in refused.json()["detail"]
    assert not any(e["status"] == "planned" for e in _events(senv, lead_id))


def test_try_texting_again_is_kept_when_another_review_is_open(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    failed = _fail_day0_text(senv, lead_id, "30003")
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "retry_texts"}).status_code == 200
    assert not any(e["status"] == "planned" for e in _events(senv, lead_id))  # still paused
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    retry = [e for e in _events(senv, lead_id) if e["channel"] == "sms" and e["id"] != failed
             and e["day_offset"] == 0 and e["status"] == "planned"]
    assert len(retry) == 1


def test_pause_is_refused_on_a_closed_lead(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30007")
    _post(f"review/{lead_id}/resolve", {"choice": "front_desk"})
    pause = _post(f"leads/{lead_id}/cadence", {"action": "pause"})
    assert pause.status_code == 409 and "handled by front desk" in pause.json()["detail"]
    assert senv.lead(lead_id)["cadence_state"] == "terminated"
