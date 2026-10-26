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
    return [row["id"] for row in senv.db.execute(
        "select id::text as id from leads where practice_id=%s and phone_e164 like %s "
        "and cadence_state in ('pending','active','paused')",
        (senv.practice_id, f"%{phone}")).fetchall()]


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
    assert senv.db.execute("select resolution from lead_reviews where lead_id=%s",
                           (new_id,)).fetchone()["resolution"] == "replaced"
    assert senv.cell(new, "outcome_status") == RESTARTED_ROW
    assert _get(f"leads/{new_id}").json()["lead"]["next_step"] == RESTARTED_ROW
    titles = [entry["title"] for entry in _get(f"leads/{new_id}").json()["activity"]]
    assert "Resolved by System: Replaced by another lead with this phone number." in titles
    assert not any("newer referral" in title for title in titles)
    # The middle lead was already closed by the newest row: it keeps the newer-row words.
    assert senv.lead(mid_id)["status_reason"] == REPLACED_REASON
    assert senv.cell(mid, "outcome_status") == NEWER_ROW
    assert _get(f"leads/{mid_id}").json()["lead"]["next_step"] == (
        "Replaced: a newer Sheet row has this phone number")
    assert senv.cell(old, "lead_status") != "Closed"


def test_board_in_cadence_takes_the_number_too(phone_env):
    senv = phone_env
    phone = _phone()
    (_old, new), (old_id, new_id) = _rows(senv, phone, 2)
    assert _active(senv, phone) == [new_id]

    assert _post(f"leads/{old_id}/stage", {"stage": "cadence"}).status_code == 200
    senv.tick()

    assert _active(senv, phone) == [old_id]
    assert senv.lead(new_id)["status_reason"] == RESTARTED_REASON
    assert senv.cell(new, "outcome_status") == RESTARTED_ROW


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
