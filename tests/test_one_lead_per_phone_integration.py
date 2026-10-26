"""One phone = one active lead, on every path that starts a lead again (owner, 2026-10-26).

Demo 2026-10-26: three Sheet rows on one phone; staff dragged the OLDEST to New on the board. It
restarted, and the two newer leads were closed "replaced by a newer referral on this number" (wrong:
an older lead was restarted). A move to In cadence did not take the number at all, so two leads on
one phone could both be called.

Needs TEST_DATABASE_URL (a disposable local database with the migrations applied).
"""

import pytest
from fastapi.testclient import TestClient
from test_sheet_direct_integration import _phone, env  # noqa: F401 - the shared Sheet fixture

from rpt_agent.api import app
from rpt_agent.config import get_settings

pytestmark = pytest.mark.integration

HEADERS = {"X-Dashboard-Token": "x" * 32, "X-Dashboard-User-ID": "emma01",
           "X-Dashboard-User-Name": "Emma", "X-Dashboard-User-Role": "employee"}
REPLACED_REASON = "replaced by a newer referral on this number"
RESTARTED_REASON = "replaced: another lead with this phone was restarted"
NEWER_ROW = "Closed: a newer row has this phone number"
RESTARTED_ROW = "Closed: another row with this phone number was restarted"


@pytest.fixture
def phone_env(env, monkeypatch):  # noqa: F811 - pytest fixture injection
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("TEST_MODE", "false")
    get_settings.cache_clear()
    return env


def _post(path, body):
    return TestClient(app).post(f"/api/v1/dashboard/{path}", json=body, headers=HEADERS)


def _get(path):
    return TestClient(app).get(f"/api/v1/dashboard/{path}", headers=HEADERS)


def _rows(senv, phone, count):
    """`count` Sheet rows on one phone, oldest first; the newest takes the number."""
    numbers = []
    for n in range(count):
        numbers.append(senv.start_row(phone=phone, name=f"Ana Row{n}"))
        senv.tick()
    senv.tick()
    return numbers, [senv.cell(number, "lead_id") for number in numbers]


def _active(senv, phone):
    """Leads on the phone still in outreach with steps left to run (really going to be called)."""
    return [row["id"] for row in senv.db.execute(
        "select id::text as id from leads l where practice_id=%s and phone_e164 like %s "
        "and cadence_state in ('pending','active','paused') and exists(select 1 from "
        "outreach_events e where e.lead_id=l.id and e.status='planned')",
        (senv.practice_id, f"%{phone}")).fetchall()]


def _planned(senv, lead_id):
    return [row["id"] for row in senv.db.execute(
        "select id from outreach_events where lead_id=%s and status='planned' order by id",
        (lead_id,)).fetchall()]


def test_board_new_on_the_oldest_row_closes_the_newer_lead_as_restarted(phone_env):
    senv = phone_env
    phone = _phone()
    (old, mid, new), (old_id, mid_id, new_id) = _rows(senv, phone, 3)
    assert senv.lead(old_id)["status_reason"] == REPLACED_REASON
    assert _active(senv, phone) == [new_id]
    # The newest lead has an open review: it closes as "replaced", not plain "closed".
    assert _post(f"leads/{new_id}/stage", {"stage": "attention"}).status_code == 200

    assert _post(f"leads/{old_id}/stage", {"stage": "new"}).status_code == 200
    senv.tick()

    assert _active(senv, phone) == [old_id]
    newest = senv.lead(new_id)
    assert (newest["cadence_state"], newest["status_reason"]) == ("terminated", RESTARTED_REASON)
    assert newest["needs_review"] is False
    # A replaced lead shows as Closed (owner, 2026-10-26), on the board and in the Sheet.
    assert newest["status"] == "closed_no_response"
    assert _get(f"leads/{new_id}").json()["lead"]["stage"] == "closed"
    assert senv.cell(new, "lead_status") == "Closed"
    assert senv.db.execute("select resolution from lead_reviews where lead_id=%s",
                           (new_id,)).fetchone()["resolution"] == "replaced"
    assert senv.cell(new, "outcome_status") == RESTARTED_ROW
    assert _get(f"leads/{new_id}").json()["lead"]["next_step"] == RESTARTED_ROW
    titles = [entry["title"] for entry in _get(f"leads/{new_id}").json()["activity"]]
    assert "Resolved by System: Replaced by another lead with this phone number." in titles
    assert not any("newer referral" in title for title in titles)
    # Its close names why (tester 2026-10-26), not "Closed, no response".
    assert "Replaced: another lead with this phone was restarted" in titles
    assert "Closed, no response" not in titles
    # The middle lead was already closed by the newest row: it keeps the newer-row words.
    assert senv.lead(mid_id)["status_reason"] == REPLACED_REASON
    assert senv.cell(mid, "outcome_status") == NEWER_ROW
    assert _get(f"leads/{mid_id}").json()["lead"]["next_step"] == (
        "Replaced: a newer Sheet row has this phone number")
    assert senv.cell(old, "lead_status") != "Closed"


def test_board_in_cadence_takes_the_number_when_the_lead_has_steps_left(phone_env):
    senv = phone_env
    phone = _phone()
    (_old, new), (old_id, new_id) = _rows(senv, phone, 2)
    # Two leads with steps on one phone (data from before 2026-10-26): In cadence takes the number.
    senv.db.execute("update outreach_events set status='planned',failure_reason=null "
                    "where lead_id=%s and status='skipped'", (old_id,))
    senv.db.execute("update leads set cadence_state='paused',status_reason=null where id=%s", (old_id,))

    assert _post(f"leads/{old_id}/stage", {"stage": "cadence"}).status_code == 200
    senv.tick()

    assert _active(senv, phone) == [old_id]
    assert senv.lead(new_id)["status_reason"] == RESTARTED_REASON
    assert senv.cell(new, "outcome_status") == RESTARTED_ROW
    assert senv.lead(new_id)["status"] == "closed_no_response"
    assert senv.cell(new, "lead_status") == "Closed"


def test_board_in_cadence_on_an_ended_lead_leaves_the_live_lead_alone(phone_env):
    # Tester 2026-10-26: In cadence on the replaced lead closed the live one and started nothing.
    senv = phone_env
    phone = _phone()
    (old, new), (old_id, new_id) = _rows(senv, phone, 2)
    steps = _planned(senv, new_id)
    assert steps and _planned(senv, old_id) == []

    assert _post(f"leads/{old_id}/stage", {"stage": "cadence"}).status_code == 200
    senv.tick()

    assert _active(senv, phone) == [new_id]
    assert _planned(senv, new_id) == steps  # no call or text lost
    assert senv.lead(new_id)["cadence_state"] == "active"
    assert senv.lead(new_id)["status_reason"] is None
    assert senv.cell(new, "outcome_status") != RESTARTED_ROW
    # The moved lead still says why it stopped: the newer row has the number.
    senv.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                    "where lead_id=%s", (old_id,))
    senv.sheet.set(old, "action", "Restart cadence")
    senv.tick()
    assert _active(senv, phone) == [new_id]


def test_sheet_restart_of_an_older_row_closes_the_newer_lead_as_restarted(phone_env):
    senv = phone_env
    phone = _phone()
    old = senv.start_row(phone=phone, name="Ana Old")
    senv.tick()
    old_id = senv.cell(old, "lead_id")
    # Its cadence ran out before the patient came back with a new referral.
    senv.db.execute("update leads set status='closed_no_response',cadence_state='completed' "
                    "where id=%s", (old_id,))
    senv.db.execute("update outreach_events set status='skipped' where lead_id=%s "
                    "and status='planned'", (old_id,))
    senv.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                    "where lead_id=%s", (old_id,))  # not the restart cooldown
    new = senv.start_row(phone=phone, name="Ana New")
    senv.tick()
    new_id = senv.cell(new, "lead_id")
    assert _active(senv, phone) == [new_id]

    senv.sheet.set(old, "action", "Restart cadence")
    senv.tick()
    senv.tick()

    assert _active(senv, phone) == [old_id]
    assert senv.lead(new_id)["status_reason"] == RESTARTED_REASON
    assert senv.cell(new, "outcome_status") == RESTARTED_ROW
    # The closed newer row cannot be restarted from the Sheet: the dashboard decides.
    senv.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                    "where lead_id=%s", (new_id,))
    senv.sheet.set(new, "action", "Restart cadence")
    senv.tick()
    assert senv.cell(new, "outcome_status") == (
        "Error: this lead is closed (another row with this phone number was restarted). "
        "Use the dashboard.")
    assert _active(senv, phone) == [old_id]


def test_review_restart_of_an_older_lead_closes_the_newer_lead_as_restarted(phone_env):
    senv = phone_env
    phone = _phone()
    (_old, new), (old_id, new_id) = _rows(senv, phone, 2)
    # Staff move the replaced lead to review, then choose Restart on its card.
    assert _post(f"leads/{old_id}/stage", {"stage": "attention"}).status_code == 200
    assert _post(f"review/{old_id}/resolve", {"choice": "restart_cadence"}).status_code == 200
    senv.tick()

    assert _active(senv, phone) == [old_id]
    assert senv.lead(new_id)["status_reason"] == RESTARTED_REASON
    assert senv.cell(new, "outcome_status") == RESTARTED_ROW
    closed = _get(f"review/{new_id}").json()["closed"]
    assert closed["message"] == (
        "This lead was closed: another row with this phone number was restarted. "
        "Its history is kept.")


def test_a_new_row_still_says_a_newer_row(phone_env):
    senv = phone_env
    phone = _phone()
    (old, _new), (old_id, new_id) = _rows(senv, phone, 2)
    assert _active(senv, phone) == [new_id]
    assert senv.lead(old_id)["status_reason"] == REPLACED_REASON
    assert senv.cell(old, "outcome_status") == NEWER_ROW
    # It used to keep "In cadence" after its calls had ended.
    assert senv.lead(old_id)["status"] == "closed_no_response"
    assert senv.cell(old, "lead_status") == "Closed"
    lead = _get(f"leads/{old_id}").json()["lead"]
    assert (lead["stage"], lead["next_step"]) == (
        "closed", "Replaced: a newer Sheet row has this phone number")
    titles = [entry["title"] for entry in _get(f"leads/{old_id}").json()["activity"]]
    assert "Replaced by a newer referral on this number" in titles


def test_a_replaced_lead_in_callback_or_review_closes(phone_env):
    senv = phone_env
    phone = _phone()
    first = senv.start_row(phone=phone, name="Ana One")
    senv.tick()
    first_id = senv.cell(first, "lead_id")
    senv.db.execute("update leads set status='callback_scheduled' where id=%s", (first_id,))
    second = senv.start_row(phone=phone, name="Ana Two")
    senv.tick()
    senv.tick()
    second_id = senv.cell(second, "lead_id")
    assert senv.lead(first_id)["status"] == "closed_no_response"
    history = senv.db.execute(
        "select from_status,to_status from lead_status_history where lead_id=%s and reason=%s",
        (first_id, REPLACED_REASON)).fetchone()
    assert (history["from_status"], history["to_status"]) == ("callback_scheduled",
                                                              "closed_no_response")
    # A lead in Needs Review (status needs_attention) leaves that column too.
    senv.db.execute("update leads set status='needs_attention',needs_review=true where id=%s",
                    (second_id,))
    assert _post(f"leads/{first_id}/stage", {"stage": "new"}).status_code == 200
    senv.tick()
    assert senv.lead(second_id)["status"] == "closed_no_response"
    assert _get(f"leads/{second_id}").json()["lead"]["stage"] == "closed"
    assert senv.cell(second, "lead_status") == "Closed"


def test_a_booked_lead_keeps_booked_when_a_new_row_takes_the_number(phone_env):
    senv = phone_env
    phone = _phone()
    old = senv.start_row(phone=phone, name="Ana Booked")
    senv.tick()
    old_id = senv.cell(old, "lead_id")
    assert _post(f"leads/{old_id}/stage", {"stage": "booked"}).status_code == 200
    senv.start_row(phone=phone, name="Ana Again")
    senv.tick()
    lead = senv.lead(old_id)
    assert (lead["status"], lead["cadence_state"]) == ("booked", "completed")
    assert lead["status_reason"] != REPLACED_REASON


def test_review_continue_with_no_steps_left_leaves_the_live_lead_alone(phone_env):
    # Tester 2026-10-26: Continue on the replaced lead ("none left, moves to Closed") closed the
    # live lead too, leaving no lead on the phone.
    senv = phone_env
    phone = _phone()
    (_old, new), (old_id, new_id) = _rows(senv, phone, 2)
    steps = _planned(senv, new_id)
    assert _post(f"leads/{old_id}/stage", {"stage": "attention"}).status_code == 200
    assert _post(f"review/{old_id}/resolve", {"choice": "continue"}).status_code == 200
    senv.tick()

    assert _active(senv, phone) == [new_id]
    assert _planned(senv, new_id) == steps
    assert senv.lead(new_id)["status_reason"] is None
    assert senv.cell(new, "outcome_status") != RESTARTED_ROW
