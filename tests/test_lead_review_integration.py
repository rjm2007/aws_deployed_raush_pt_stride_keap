"""Review reasons and resolve-with-a-choice, end to end on a real schema (fake Sheet).

Incident 2026-10-21: a lead flagged for a Name edit and one flagged for a Day 0 text that failed
with Twilio 30006 were "resumed"; the review stayed open, the worker sent nothing, and nobody could
see what had changed or who decided what. Every flag now has a review row with a code, staff pick
a choice (or the lead stays paused), and both are recorded.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied).
"""

from datetime import UTC, date, datetime, timedelta
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
from rpt_agent.services import sheet_direct
from rpt_agent.services.delivery import apply_twilio_message_status
from rpt_agent.services.google_sheet import parse_grid
from rpt_agent.services.lead_status import report_lead_status
from rpt_agent.services.sheet_direct import profile_hash
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


# --- a stale page: the review closed while it was open ----------------------------------------


def test_closed_review_says_who_resolved_it_or_that_the_lead_was_replaced(review_env):
    """2026-10-23: a page opened before the review closed said only "no longer available"."""
    from rpt_agent.services.review import hand_over_number

    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 200
    message = "This review was already resolved by Emma: Same person, use new name & continue."
    card = _get(f"review/{lead_id}").json()
    assert card["review"] is None and card["options"] == []
    assert card["closed"]["reason"] == "resolved" and card["closed"]["message"] == message
    again = _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"})
    assert again.status_code == 409 and again.json()["detail"] == message

    # Flag it again, then a newer Sheet row on the same number takes over (one lead per phone).
    senv.sheet.set(number, "name", "Asha Rao")
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is True
    lead = senv.lead(lead_id)
    with senv.db.transaction():
        hand_over_number(senv.db, practice_id=lead["practice_id"], phone=lead["phone_e164"],
                         lead_id=str(uuid4()))
    replaced = "This lead was closed: a newer Sheet row uses the same phone number. Its history is kept."
    card = _get(f"review/{lead_id}").json()
    assert card["review"] is None and card["closed"] == {"reason": "replaced", "message": replaced}
    refused = _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"})
    assert refused.status_code == 409 and refused.json()["detail"] == replaced


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
    assert choices == ["use_new_name", "keep_old_name", "use_new_name_restart", "keep_old_name_restart",
                       "different_person", "front_desk"]
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
    assert senv.cell(number, "action") == ""  # emptied once the Start ran (2026-10-25), never refilled
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


def test_keep_old_name_writes_the_old_name_back_into_the_sheet_without_a_new_review(review_env,
                                                                                    monkeypatch):
    """Owner, demo test 2026-10-23: "QA Nameone" -> "QA Namekeep", keep old name: calls resumed but
    the row still showed "QA Namekeep". The Name cell now goes back, and nothing flags or re-syncs."""
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    senv.tick(full=False)  # a narrow poll writes the result
    assert senv.cell(number, "name") == "Asha Rao"
    assert senv.cell(number, "outcome_status") == "Resolved: old name kept"
    state = senv.db.execute("select profile_hash,sheet_name from sheet_row_state where lead_id=%s",
                            (lead_id,)).fetchone()
    grid = parse_grid(senv.sheet.read_values(), 2)
    assert state == {"profile_hash": profile_hash(grid.rows[0]), "sheet_name": "Asha Rao"}
    calls = []
    real = sheet_direct.sync_sheet_lead
    monkeypatch.setattr(sheet_direct, "sync_sheet_lead", lambda **kw: calls.append(kw) or real(**kw))
    for _ in range(3):
        senv.tick()
    assert calls == []  # in sync: no profile sync, so no loop
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["full_name"], lead["cadence_state"]) == (False, "Asha Rao", "active")
    assert len(_reviews(senv, lead_id)) == 1
    assert senv.cell(number, "name") == "Asha Rao"


def test_the_same_new_name_retyped_after_the_write_back_opens_a_review_again(review_env):
    """Tester case 5, 2026-10-23: keep old name, the Sheet went back, then staff typed the same new
    name again: nothing was flagged and the Sheet and the lead disagreed silently."""
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    senv.tick()
    assert senv.cell(number, "name") == "Asha Rao"
    assert senv.lead(lead_id)["sheet_name_accepted"] is None
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is True
    assert senv.cell(number, "outcome_status") == "Needs review: name changed"
    assert len(_reviews(senv, lead_id)) == 2


def test_keep_old_name_leaves_a_third_name_staff_typed_to_the_normal_review(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    senv.sheet.set(number, "name", "Someone Else")  # typed before the write lands
    senv.tick()
    assert senv.cell(number, "name") == "Someone Else"
    senv.tick()
    assert senv.cell(number, "name") == "Someone Else"
    assert senv.lead(lead_id)["needs_review"] is True
    assert [r["details"]["new_name"] for r in _reviews(senv, lead_id)] == ["Asha Kumar", "Someone Else"]


def test_keep_old_name_skips_a_third_name_typed_between_the_read_and_the_write(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_name"}).status_code == 200
    # The poll's only row read before its write is the pre-write check: staff type then.
    senv.sheet.before_write = lambda: senv.sheet.set(number, "name", "Someone Else")
    senv.tick(full=False)
    assert senv.cell(number, "name") == "Someone Else"
    assert senv.cell(number, "outcome_status") == "Resolved: old name kept"  # the rest landed
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is True and senv.cell(number, "name") == "Someone Else"


def test_use_new_name_leaves_the_sheet_name_as_it_is(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 200
    senv.tick()
    senv.tick()
    assert senv.cell(number, "name") == "Asha Kumar" and senv.lead(lead_id)["needs_review"] is False


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
    assert lead["status_reason"].startswith("Phone number changed in the Sheet on ")
    assert "…" not in lead["status_reason"]
    assert lead["status_reason"].endswith("Add the new number as a new row.")
    assert not any(event["status"] == "planned" for event in _events(senv, lead_id))
    [review] = _reviews(senv, lead_id)
    assert review["code"] == "phone_changed" and review["resolution"] == "close_phone_changed"
    assert review["resolved_at"] is not None
    assert new_phone not in str(review["details"])  # only the last four digits are kept
    assert senv.cell(number, "outcome_status") == "Phone changed: add a new row"
    assert senv.cell(number, "lead_status") == "Closed"
    assert senv.cell(number, "action") == ""
    assert _post(f"leads/{lead_id}/cadence", {"action": "resume"}).status_code == 409
    assert _post(f"leads/{lead_id}/stage", {"stage": "new"}).status_code == 409
    # Later polls (and another edit) neither duplicate the record nor reopen the lead.
    senv.sheet.set(number, "email", "again@example.test")
    senv.tick()
    senv.tick()
    assert len(_reviews(senv, lead_id)) == 1 and senv.lead(lead_id)["status"] == "invalid_phone"
    # The Sheet's own Restart is refused too, with the reason; Action empties.
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    assert senv.lead(lead_id)["cadence_state"] == "terminated"
    assert senv.cell(number, "outcome_status").startswith("Error: ")
    assert senv.cell(number, "action") == ""


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
    # Trying again cannot work on a landline.
    assert choices == ["calls_only", "restart_cadence", "front_desk"]
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
    assert [o["choice"] for o in card["options"]] == ["calls_only", "retry_texts", "restart_cadence",
                                                      "front_desk"]
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
    assert [o["choice"] for o in card["options"]] == ["number_ok", "wrong_number", "restart_cadence",
                                                      "front_desk"]
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: wrong number"
    assert _post(f"review/{lead_id}/resolve", {"choice": "number_ok"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("in_progress", "active", False)
    assert any(e["status"] == "planned" for e in _events(senv, lead_id))


def test_booked_typed_on_a_flagged_row_books_and_closes_the_review(review_env):
    # QA 2026-10-22 (row "QA Actionkeep"): Booked on a "Needs review: wrong number" row was ignored.
    senv = review_env
    number, lead_id = _started(senv)
    _wrong_person(senv, lead_id)
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: wrong number"
    senv.sheet.set(number, "action", "Booked")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("booked", False)
    [review] = _reviews(senv, lead_id)
    assert review["resolved_at"] is not None and review["resolution"] == "booked"
    assert review["resolved_by_name"] == "Google Sheet"
    assert senv.cell(number, "outcome_status") == "Booked"
    assert (senv.cell(number, "action"), senv.cell(number, "lead_status")) == ("", "Booked")
    assert not any(e["status"] == "planned" for e in _events(senv, lead_id))
    assert _post(f"review/{lead_id}/resolve", {"choice": "number_ok"}).status_code == 409


def test_do_not_contact_typed_on_a_flagged_row_blocks_the_number_and_closes_the_review(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    _wrong_person(senv, lead_id)
    senv.tick()
    senv.sheet.set(number, "action", "Do Not Contact")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("do_not_contact", False)
    [review] = _reviews(senv, lead_id)
    assert review["resolution"] == "do_not_contact"
    blocked = senv.db.execute("select 1 from suppressed_numbers where phone_e164=%s",
                              (lead["phone_e164"],)).fetchone()
    assert blocked
    assert senv.cell(number, "outcome_status") == "Do Not Contact applied"
    assert senv.cell(number, "action") == ""


REVIEW_REFUSED = ("Error: this lead needs review. Resolve it on the dashboard; you can restart it "
                  "from there.")


def _refusals(senv, lead_id, code):
    return senv.db.execute(
        "select count(*) as n from lead_action_requests where lead_id::text=%s "
        "and action='restart_cadence' and status='failed' "
        "and response_body#>>'{body,code}'=%s", (lead_id, code)).fetchone()["n"]


def test_restart_typed_on_a_flagged_row_is_refused_at_once_and_emptied(review_env):
    # Owner 2026-10-25: no more waiting in Action. The Restart is refused with a reason, Action
    # empties, the review stays open, and the restart is chosen on the review card instead.
    senv = review_env
    number, lead_id = _started(senv)
    _wrong_person(senv, lead_id)
    senv.tick()
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    assert senv.cell(number, "outcome_status") == REVIEW_REFUSED
    assert senv.cell(number, "action") == "" and senv.cell(number, "action_request_id") == ""
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("invalid_phone", True)  # no bypass
    assert _refusals(senv, lead_id, "restart_needs_review") == 1
    assert _restarts(senv, lead_id) == 0
    # Resolved on the dashboard: nothing runs by itself (nothing is left in Action).
    assert _post(f"review/{lead_id}/resolve", {"choice": "number_ok"}).status_code == 200
    for _ in range(3):
        senv.tick()
    assert senv.cell(number, "outcome_status") == "Resolved: number confirmed"
    assert _restarts(senv, lead_id) == 0
    # Picked again now the lead is out of review: it runs.
    _no_cooldown(senv, lead_id)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Cadence restarted"
    assert _restarts(senv, lead_id) == 1 and senv.cell(number, "action") == ""


def test_a_booked_typed_before_the_review_is_resolved_still_runs(review_env, monkeypatch):
    # The QA repro: Booked typed, then "Number is right" resolved while the row was still settling.
    # The resolution put "Start cadence" over the Booked; calls restarted and nothing was booked.
    from rpt_agent import sheet_sync_worker

    senv = review_env
    number, lead_id = _started(senv)
    _wrong_person(senv, lead_id)
    senv.tick()
    monkeypatch.setattr(get_settings(), "sheet_settle_seconds", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sheet_sync_worker, "_clock", lambda: clock["t"])
    senv.sheet.set(number, "action", "Booked")
    senv.tick()  # seen, settling
    assert _post(f"review/{lead_id}/resolve", {"choice": "number_ok"}).status_code == 200
    clock["t"] += 30
    senv.tick()  # the resolution's snapshot arrives while the Booked still settles
    assert senv.cell(number, "action") == "Booked"
    clock["t"] += 31
    senv.tick()
    assert senv.lead(lead_id)["status"] == "booked"
    assert senv.cell(number, "outcome_status") == "Booked"
    assert senv.cell(number, "action") == ""


def _no_cooldown(senv, lead_id):
    senv.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                    "where lead_id=%s", (lead_id,))
    senv.db.execute("update lead_reviews set resolved_at=resolved_at-interval '11 minutes' "
                    "where lead_id=%s and resolved_at is not null", (lead_id,))


def _restarts(senv, lead_id):
    return senv.db.execute(
        "select count(*) as n from lead_action_requests where lead_id::text=%s "
        "and action='restart_cadence' and status='completed'", (lead_id,)).fetchone()["n"]


def _name_review(senv):
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: name changed"
    return number, lead_id


def _review_still_open(senv, lead_id):
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["full_name"]) == (True, "Asha Rao")
    [review] = _reviews(senv, lead_id)
    assert review["resolved_at"] is None


def test_a_restart_with_outcome_status_cleared_is_still_refused_on_a_lead_in_review(review_env):
    # Tester 2026-10-23: staff cleared Outcome Status next to a Restart on a row in review; it ran at
    # once, closed the name review as "restarted" by Google Sheet and accepted the new name. The
    # database decides, not the row.
    senv = review_env
    number, lead_id = _name_review(senv)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    assert senv.cell(number, "outcome_status") == REVIEW_REFUSED
    assert senv.cell(number, "action") == ""
    _review_still_open(senv, lead_id)
    assert _restarts(senv, lead_id) == 0


def test_a_restart_left_waiting_by_the_old_rules_is_emptied_never_run(review_env):
    # One-time, first poll after the 2026-10-25 change: a row still holding a parked Restart (the
    # old "...then the restart runs" words) is emptied with the refusal text and never run.
    senv = review_env
    number, lead_id = _name_review(senv)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.sheet.set(number, "outcome_status", "Needs review: name changed; resolve it on the "
                                             "dashboard first, then the restart runs")
    senv.tick(full=False)  # a narrow poll loads it
    assert senv.cell(number, "outcome_status") == REVIEW_REFUSED
    assert senv.cell(number, "action") == ""
    _review_still_open(senv, lead_id)
    for _ in range(2):
        senv.tick()
    assert _restarts(senv, lead_id) == 0 and senv.cell(number, "action") == ""
    titles = [entry["title"] for entry in _get(f"leads/{lead_id}").json()["activity"]]
    assert ("Restart cadence refused: this lead needs review. Resolve it on the dashboard; you can "
            "restart it from there.") in titles


def test_booked_cleared_on_a_row_in_review_still_runs(review_env):
    senv = review_env
    number, lead_id = _name_review(senv)
    senv.sheet.set(number, "action", "Booked")
    senv.sheet.set(number, "outcome_status", "")
    senv.tick()
    assert senv.lead(lead_id)["status"] == "booked"
    assert senv.cell(number, "outcome_status") == "Booked"


# --- restart from the review card (owner, 2026-10-25) ---------------------------------------------


@pytest.mark.parametrize(("choice", "name", "sheet_name", "status"), [
    ("use_new_name_restart", "Asha Kumar", "Asha Kumar", "Resolved: new name used; cadence restarted"),
    ("keep_old_name_restart", "Asha Rao", "Asha Rao", "Resolved: old name kept; cadence restarted"),
])
def test_a_name_review_can_restart_from_day_0(review_env, choice, name, sheet_name, status):
    senv = review_env
    number, lead_id = _name_review(senv)
    old_events = {e["id"] for e in _events(senv, lead_id)}
    card = _get(f"review/{lead_id}").json()
    assert [o["choice"] for o in card["options"]] == [
        "use_new_name", "keep_old_name", "use_new_name_restart", "keep_old_name_restart",
        "different_person", "front_desk"]
    option = next(o for o in card["options"] if o["choice"] == choice)
    assert option["label"].endswith("& restart from Day 0")
    assert "new cadence starts from Day 0" in option["effect"]
    assert option["next_step"] == "Next step: the Day 0 call, within calling hours."
    response = _post(f"review/{lead_id}/resolve", {"choice": choice})
    assert response.status_code == 200, response.text
    lead = senv.lead(lead_id)
    assert (lead["full_name"], lead["needs_review"], lead["cadence_state"]) == (name, False, "active")
    events = _events(senv, lead_id)
    assert events and all(e["status"] == "planned" for e in events)
    assert not old_events & {e["id"] for e in events}  # a fresh Day 0 run
    [review] = _reviews(senv, lead_id)
    assert (review["resolution"], review["resolved_by_name"]) == (choice, "Emma")
    senv.tick(full=False)
    assert senv.cell(number, "outcome_status") == status
    assert senv.cell(number, "name") == sheet_name  # keep old name: written back
    for _ in range(2):
        senv.tick()
    assert senv.lead(lead_id)["needs_review"] is False and len(_reviews(senv, lead_id)) == 1
    titles = [entry["title"] for entry in _get(f"leads/{lead_id}").json()["activity"]]
    assert f"Resolved by Emma: {option['label']}." in titles


@pytest.mark.parametrize("group", ["text", "call", "technical", "other"])
def test_text_call_technical_and_other_reviews_can_restart_from_day_0(review_env, group):
    senv = review_env
    number, lead_id = _started(senv)
    if group == "text":
        _fail_day0_text(senv, lead_id, "30003")
    elif group == "call":
        _wrong_person(senv, lead_id)
    else:
        code = "dispatch_failed" if group == "technical" else "other"
        senv.db.execute("update leads set needs_review=true,review_reason='Patient asked a question',"
                        "status='needs_attention',cadence_state='paused' where id=%s", (lead_id,))
        senv.db.execute("update lead_reviews set code=%s where lead_id=%s and resolved_at is null",
                        (code, lead_id))
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["group"] == group
    choices = [o["choice"] for o in card["options"]]
    assert "restart_cadence" in choices and choices[-1] == "front_desk"
    senv.db.execute("update outreach_events set status='delivered' where lead_id=%s "
                    "and status in ('attempted','in_flight')", (lead_id,))
    response = _post(f"review/{lead_id}/resolve", {"choice": "restart_cadence"})
    assert response.status_code == 200, response.text
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["cadence_state"]) == (False, "active")
    assert any(e["status"] == "planned" and e["day_offset"] == 0 for e in _events(senv, lead_id))
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Cadence restarted"


def test_a_restart_choice_respects_the_cooldown_and_says_why(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    assert _restarts(senv, lead_id) == 1
    _wrong_person(senv, lead_id)
    senv.db.execute("update outreach_events set status='delivered' where lead_id=%s "
                    "and status in ('attempted','in_flight')", (lead_id,))
    refused = _post(f"review/{lead_id}/resolve", {"choice": "restart_cadence"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == ("The cadence was already restarted in the last 10 minutes. "
                                        "Try the restart again later.")
    _review_still_open_any(senv, lead_id)
    _no_cooldown(senv, lead_id)
    assert _post(f"review/{lead_id}/resolve", {"choice": "restart_cadence"}).status_code == 200
    # And the other way round: a Sheet Restart right after the card's restart is refused.
    senv.tick()
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    assert senv.cell(number, "outcome_status").startswith(
        "Error: restarted less than 10 minutes ago. Pick Restart cadence again after ")
    assert _restarts(senv, lead_id) == 1


def _review_still_open_any(senv, lead_id):
    assert senv.lead(lead_id)["needs_review"] is True
    assert any(r["resolved_at"] is None for r in _reviews(senv, lead_id))


def test_no_restart_is_offered_while_another_review_is_open(review_env):
    # A restart settles every open review at once: the second one would never be decided.
    senv = review_env
    number, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30003")
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.tick()
    senv.tick()
    card = _get(f"review/{lead_id}").json()
    assert "restart_cadence" not in [o["choice"] for o in card["options"]]
    refused = _post(f"review/{lead_id}/resolve", {"choice": "restart_cadence"})
    assert refused.status_code == 409 and "another open review" in refused.json()["detail"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "retry_texts"}).status_code == 200
    card = _get(f"review/{lead_id}").json()
    assert "use_new_name_restart" in [o["choice"] for o in card["options"]]  # the last one


# --- Sheet Restart guardrails: booked, closed (owner, 2026-10-25) ----------------------------------


def test_a_sheet_restart_on_a_front_desk_lead_is_refused(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    _fail_day0_text(senv, lead_id, "30007")
    assert _post(f"review/{lead_id}/resolve", {"choice": "front_desk"}).status_code == 200
    senv.tick()
    _no_cooldown(senv, lead_id)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    assert senv.cell(number, "outcome_status") == (
        "Error: this lead is closed (handled by front desk). Use the dashboard.")
    assert senv.cell(number, "action") == ""
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["cadence_state"]) == ("closed_no_response", "terminated")
    assert _restarts(senv, lead_id) == 0
    titles = [entry["title"] for entry in _get(f"leads/{lead_id}").json()["activity"]]
    assert ("Restart cadence refused: this lead is closed (handled by front desk). Use the "
            "dashboard.") in titles


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
    assert [o["choice"] for o in card["options"]] == ["continue", "restart_cadence", "front_desk"]
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


@pytest.mark.parametrize("in_review", [True, False])
def test_board_close_writes_closed_into_the_sheet(review_env, in_review):
    """2026-10-26: a board move to Closed sent nothing, so Outcome Status kept the old text."""
    senv = review_env
    number, lead_id = _started(senv)
    if in_review:
        senv.sheet.set(number, "name", "Asha Kumar")
        senv.tick()
        senv.tick()
        assert senv.cell(number, "outcome_status") == "Needs review: name changed"
    assert _post(f"leads/{lead_id}/stage", {"stage": "closed"}).status_code == 200
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Closed on the dashboard"
    assert senv.cell(number, "lead_status") == "Closed"
    assert senv.cell(number, "action") == ""  # never written by the backend
    assert all(r["resolved_at"] is not None for r in _reviews(senv, lead_id))
    assert len(_reviews(senv, lead_id)) == (1 if in_review else 0)


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
    assert [o["choice"] for o in card["options"]] == ["calls_only", "restart_cadence", "front_desk"]


def test_an_old_free_text_reason_falls_back_to_continue_or_front_desk(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    senv.db.execute("update leads set needs_review=true,review_reason='Patient asked a question',"
                   "status='needs_attention',cadence_state='paused' where id=%s", (lead_id,))
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["group"] == "other"
    assert [o["choice"] for o in card["options"]] == ["continue", "restart_cadence", "front_desk"]
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
    # The row's Name went back to the name on file (2026-10-23), so the kept Sheet name is spent.
    assert senv.cell(number, "name") == "Asha Rao"
    assert (lead["needs_review"], lead["full_name"], lead["sheet_name_accepted"]) == (
        False, "Asha Rao", None)
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


def test_a_lead_replaced_by_a_newer_row_on_the_same_phone_says_so_in_the_sheet(env):  # noqa: F811
    # Demo test 2026-10-22: a name-change test lead was resumed ("keep old name"), then the next test
    # row used the same phone. One lead per number: the old one stopped, but its row still read
    # "Resolved: old name kept" under Lead Status "Closed", which looked like Continue had failed.
    phone = _phone()
    first = env.start_row(phone=phone, name="Ana First")
    env.tick()
    second = env.start_row(phone=phone, name="Ana Second")
    env.tick()
    env.tick()
    assert env.cell(first, "outcome_status") == "Closed: a newer row has this phone number"
    assert env.cell(first, "lead_status") == "Closed"
    assert env.cell(second, "outcome_status") == "Cadence started"


# --- location, case and date of birth edited in the Sheet (owner, 2026-10-25) --------------------

# Sheet column, new cell text, lead column, value on file, value after "use new", review code,
# field words, the old value as the Sheet shows it after "keep old" (dates: MM/DD/YYYY).
DETAIL_EDITS = [
    ("location", "Laguna Niguel", "location", "Dana Point", "Laguna Niguel", "location_changed",
     "location", "Dana Point"),
    ("case", "Pelvic", "lead_type", "Ortho", "Pelvic", "case_changed", "case", "Ortho"),
    ("date_of_birth", "02/03/1991", "date_of_birth", date(1990, 1, 16),
     date(1991, 2, 3), "dob_changed", "date of birth", "01/16/1990"),
]


def _detail_review(senv, column, new):
    number, lead_id = _started(senv)
    senv.sheet.set(number, column, new)
    senv.tick()
    senv.tick()
    return number, lead_id


@pytest.mark.parametrize(("column", "new", "lead_column", "old_value", "new_value", "code", "field",
                          "shown"), DETAIL_EDITS)
def test_a_location_case_or_birthday_edit_goes_to_review_and_use_new_applies_it(
        review_env, column, new, lead_column, old_value, new_value, code, field, shown):
    senv = review_env
    number, lead_id = _detail_review(senv, column, new)
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["status"], lead[lead_column]) == (True, "needs_attention", old_value)
    assert senv.cell(number, "outcome_status") == f"Needs review: {field} changed"
    assert senv.cell(number, "lead_status") == "Needs Review"
    [review] = _reviews(senv, lead_id)
    assert (review["code"], review["source"]) == (code, "sheet")
    assert review["details"]["old_value"] == shown and review["details"]["field"] == field
    card = _get(f"review/{lead_id}").json()
    assert card["review"]["group"] == "details"
    assert {"label": f"Old {field}", "value": shown} in card["review"]["proof"]
    assert [o["label"] for o in card["options"]] == [
        f"Use new {field} & continue", f"Keep old {field} & continue",
        f"Use new {field} & restart from Day 0", f"Keep old {field} & restart from Day 0",
        "Front desk will handle"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_value"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead["cadence_state"], lead[lead_column]) == (False, "active", new_value)
    senv.tick()
    assert senv.cell(number, "outcome_status") == f"Resolved: new {field} used"
    assert senv.cell(number, column) == new  # the Sheet keeps what staff typed
    for _ in range(2):
        senv.tick()
    assert senv.lead(lead_id)["needs_review"] is False and len(_reviews(senv, lead_id)) == 1


@pytest.mark.parametrize(("column", "new", "lead_column", "old_value", "new_value", "code", "field",
                          "shown"), DETAIL_EDITS)
def test_keep_old_writes_the_old_value_back_without_a_new_review(
        review_env, monkeypatch, column, new, lead_column, old_value, new_value, code, field, shown):
    senv = review_env
    number, lead_id = _detail_review(senv, column, new)
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_value"}).status_code == 200
    senv.tick(full=False)
    assert senv.cell(number, column) == shown  # written back, as for "keep old name"
    assert senv.cell(number, "outcome_status") == f"Resolved: old {field} kept"
    state = senv.db.execute("select profile_hash from sheet_row_state where lead_id=%s",
                            (lead_id,)).fetchone()
    assert state["profile_hash"] == profile_hash(parse_grid(senv.sheet.read_values(), 2).rows[0])
    calls = []
    real = sheet_direct.sync_sheet_lead
    monkeypatch.setattr(sheet_direct, "sync_sheet_lead", lambda **kw: calls.append(kw) or real(**kw))
    for _ in range(3):
        senv.tick()
    assert calls == []  # in sync: no loop
    lead = senv.lead(lead_id)
    assert (lead["needs_review"], lead[lead_column]) == (False, old_value)
    # The same new value typed again later is a new change: reviewed again.
    monkeypatch.undo()
    senv.sheet.set(number, column, new)
    senv.tick()
    senv.tick()
    assert senv.lead(lead_id)["needs_review"] is True and len(_reviews(senv, lead_id)) == 2


def test_keep_old_is_remembered_when_the_write_back_is_skipped(review_env):
    # Staff type a third value between our read and our write: theirs stays, and is reviewed;
    # the value staff already turned down is not.
    senv = review_env
    number, lead_id = _detail_review(senv, "location", "Laguna Niguel")
    assert _post(f"review/{lead_id}/resolve", {"choice": "keep_old_value"}).status_code == 200
    senv.sheet.before_write = lambda: senv.sheet.set(number, "location", "Mission Viejo")
    senv.tick(full=False)
    senv.sheet.before_write = None
    assert senv.cell(number, "location") == "Mission Viejo"
    senv.tick()
    senv.tick()
    [_, second] = _reviews(senv, lead_id)
    assert second["details"]["new_value"] == "Mission Viejo" and second["resolved_at"] is None
    assert senv.lead(lead_id)["location"] == "Dana Point"


def test_keep_old_location_and_restart_from_day_0(review_env):
    senv = review_env
    number, lead_id = _detail_review(senv, "location", "Laguna Niguel")
    old_events = {e["id"] for e in _events(senv, lead_id)}
    response = _post(f"review/{lead_id}/resolve", {"choice": "keep_old_value_restart"})
    assert response.status_code == 200, response.text
    lead = senv.lead(lead_id)
    assert (lead["location"], lead["needs_review"], lead["cadence_state"]) == ("Dana Point", False, "active")
    assert not old_events & {e["id"] for e in _events(senv, lead_id) if e["status"] == "planned"}
    senv.tick(full=False)
    assert senv.cell(number, "location") == "Dana Point"
    assert senv.cell(number, "outcome_status") == "Resolved: old location kept; cadence restarted"
    titles = [entry["title"] for entry in _get(f"leads/{lead_id}").json()["activity"]]
    assert "Resolved by Emma: Keep old location & restart from Day 0." in titles
    assert "Flagged: Location changed in the Sheet" in titles


def test_name_and_location_changed_together_are_two_reviews_in_order(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "name", "Asha Kumar")
    senv.sheet.set(number, "location", "Laguna Niguel")
    senv.tick()
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: name changed"
    assert [r["code"] for r in _reviews(senv, lead_id)] == ["name_changed", "location_changed"]
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_name"}).status_code == 200
    senv.tick()
    assert senv.cell(number, "outcome_status") == "Needs review: location changed"
    assert _post(f"review/{lead_id}/resolve", {"choice": "use_new_value"}).status_code == 200
    lead = senv.lead(lead_id)
    assert (lead["full_name"], lead["location"], lead["needs_review"]) == (
        "Asha Kumar", "Laguna Niguel", False)


def test_an_email_edit_applies_at_once_and_shows_in_the_activity(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "email", "asha.new@example.test")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert (lead["email"], lead["needs_review"]) == ("asha.new@example.test", False)
    activity = _get(f"leads/{lead_id}").json()["activity"]
    entry = next(e for e in activity if e["title"] == "Email changed in Google Sheet")
    assert entry["actor_name"] == "Google Sheet"


def test_a_finished_lead_keeps_its_details_and_is_not_reviewed(review_env):
    # Like a name: a booked lead has no outreach left to protect, so nothing is flagged, and the
    # value on file stays.
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "action", "Booked")
    senv.tick()
    senv.sheet.set(number, "case", "Pelvic")
    senv.tick()
    senv.tick()
    lead = senv.lead(lead_id)
    assert (lead["status"], lead["needs_review"], lead["lead_type"]) == ("booked", False, "Ortho")


# --- every Sheet command in the lead's Activity (owner, 2026-10-25) --------------------------------


def test_every_sheet_command_shows_in_the_activity_with_refusals_and_reasons(review_env):
    senv = review_env
    number, lead_id = _started(senv)
    senv.sheet.set(number, "action", "Restart cadence")
    senv.tick()
    senv.sheet.set(number, "action", "Restart cadence")  # within 10 minutes: refused
    senv.tick()
    senv.sheet.set(number, "action", "Start cadence")  # linked row: refused by the Sheet worker
    senv.tick()
    senv.sheet.set(number, "action", "Booked")
    senv.tick()
    senv.sheet.set(number, "action", "Do Not Contact")
    senv.tick()
    activity = _get(f"leads/{lead_id}").json()["activity"]
    sheet = [e for e in activity if e["actor_name"] == "Google Sheet"]
    titles = [e["title"] for e in sheet]
    assert "Start cadence: cadence started" in titles
    assert "Restart cadence: cadence restarted" in titles
    assert any(t.startswith("Restart cadence refused: restarted less than 10 minutes ago. Pick "
                            "Restart cadence again after ") for t in titles)
    assert ("Start cadence refused: this row already has a lead. Pick Restart cadence to start the "
            "calls again.") in titles
    assert "Booked: marked Booked" in titles
    assert "Do Not Contact: applied" in titles
    assert all(e["details"].get("source") == "Google Sheet" for e in sheet)
    # The status lines of these commands are not shown twice.
    assert not any(e["title"] == "Lead status updated" and e["actor_name"] == "Google Sheet"
                   for e in activity)


# --- every activity on a lead, once, with who made it (owner, 2026-10-26) ------------------------


def _call(senv, lead_id, status, notes=None):
    event = senv.db.execute(
        "update outreach_events set status='attempted',executed_at=now(),vapi_call_id=%s "
        "where id=(select id from outreach_events where lead_id=%s and channel='call' "
        "and status='planned' order by day_offset,id limit 1) returning id,vapi_call_id",
        (f"call-{uuid4().hex}", lead_id)).fetchone()
    report_lead_status(WorkflowTrace("t", "test"), lead_id=lead_id, status=status,
                       call_id=event["vapi_call_id"], event_id=event["id"], notes=notes)


def test_a_call_outcome_is_one_line_by_elliot_and_cancelled_steps_are_not_listed(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _call(senv, lead_id, "not_interested")
    assert senv.lead(lead_id)["status"] == "declined"
    activity = _get(f"leads/{lead_id}").json()["activity"]
    titles = [e["title"] for e in activity]
    assert titles.count("Call: not interested") == 1
    assert "Not interested" not in titles  # the status line of that same call
    assert not any("skipped" in t for t in titles)  # the steps the decline cancelled
    assert "Lead status updated" not in titles
    call = next(e for e in activity if e["title"] == "Call: not interested")
    assert (call["actor_name"], call["category"]) == ("Elliot", "calls")
    assert {e["actor_name"] for e in activity} <= {"Google Sheet", "Elliot", "System"}


def test_a_flag_is_one_line_and_names_who_raised_it(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    _call(senv, lead_id, "wrong_person", notes="Said this is Bob's number")
    activity = _get(f"leads/{lead_id}").json()["activity"]
    titles = [e["title"] for e in activity]
    flagged = [e for e in activity if e["action"] == "review.flagged"]
    assert len(flagged) == 1 and flagged[0]["actor_name"] == "Elliot"
    assert not any(t.startswith("Flagged for review:") for t in titles)
    assert "Invalid phone number" not in titles  # the status line of that same flag


def test_a_finished_cadence_shows_in_the_activity(review_env):
    senv = review_env
    _, lead_id = _started(senv)
    senv.db.execute("update outreach_events set status='skipped' where lead_id=%s", (lead_id,))
    run_safety_checks(WorkflowTrace("t", "test"))
    assert senv.lead(lead_id)["status"] == "closed_no_response"
    activity = _get(f"leads/{lead_id}").json()["activity"]
    entry = next(e for e in activity if e["title"] == "Closed, no response")
    assert entry["actor_name"] == "System"
    assert entry["details"]["reason"] == "cadence finished with no response"
    assert entry["details"]["to"] == "closed_no_response"
