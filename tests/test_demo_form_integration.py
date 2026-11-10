"""Public demo form end to end on a real schema (2026-11-07).

A form lead runs like a Sheet lead: the active cadence is planned, a test lead in test mode, one
active lead per phone. It never goes to the Sheet. Needs TEST_DATABASE_URL (a disposable local
database with the migrations applied).
"""

import pytest
from fastapi.testclient import TestClient
from test_sheet_direct_integration import STEPS, _phone, cases, env  # noqa: F401 - shared fixtures

from rpt_agent.api import app
from rpt_agent.config import get_settings

pytestmark = pytest.mark.integration
TOKEN = "t" * 32


@pytest.fixture
def form(env, monkeypatch):  # noqa: F811 - pytest fixture injection
    monkeypatch.setenv("DEMO_FORM_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_API_TOKEN", TOKEN)
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    return env


def _submit(phone, key=None, first="Jane", **extra):
    return TestClient(app).post(
        "/api/v1/demo-form/leads",
        json={"idempotency_key": key or f"demo-{phone}", "first_name": first, "last_name": "Smith",
              "country_code": "+1", "phone": phone, "date_of_birth": "1990-01-16",
              "contact_consent": True, **extra},
        headers={"X-Dashboard-Token": TOKEN})


def test_a_form_lead_starts_the_cadence_like_a_sheet_lead(form):
    phone = _phone()
    response = _submit(phone)
    assert response.status_code == 201
    lead = form.lead(response.json()["lead_id"])
    assert lead["source_system"] == "demo_form" and lead["phone_e164"] == f"+1{phone}"
    assert lead["full_name"] == "Jane Smith" and str(lead["date_of_birth"]) == "1990-01-16"
    assert lead["status"] == "in_progress" and lead["cadence_state"] == "active"
    assert lead["is_test"] is True and lead["consent_captured_at"] is not None
    assert lead["lead_type"] == "Physical Therapy" and lead["location"] == "Laguna Niguel"
    events = form.db.execute(
        "select channel,day_offset,status from outreach_events where lead_id=%s "
        "order by day_offset,id", (lead["id"],)).fetchall()
    assert [(e["day_offset"], e["channel"]) for e in events] == STEPS
    assert {e["status"] for e in events} == {"planned"}
    # Only Sheet leads are written to the Sheet.
    assert form.db.execute(
        "select count(*) as n from integration_outbox where aggregate_id=%s",
        (str(lead["id"]),)).fetchone()["n"] == 0


def test_the_chosen_case_is_stored_under_its_booking_name(form, cases):  # noqa: F811
    pelvic = _submit(_phone(), case_type="physical therapy - pelvic")
    assert pelvic.status_code == 201
    assert form.lead(pelvic.json()["lead_id"])["lead_type"] == "Physical Therapy - Pelvic"
    # Only a case the booking engine can book; nothing is created for an unknown one.
    before = len(form.leads())
    assert _submit(_phone(), case_type="Massage").status_code == 422
    assert len(form.leads()) == before


def test_a_double_tap_makes_one_lead(form):
    phone = _phone()
    first, second = _submit(phone, key="tap-0001"), _submit(phone, key="tap-0001")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["lead_id"] == second.json()["lead_id"]
    assert len(form.leads()) == 1


def test_the_same_phone_again_takes_the_number(form):
    phone = _phone()
    older = _submit(phone, key="again-0001").json()["lead_id"]
    newer = _submit(phone, key="again-0002", first="Janet").json()["lead_id"]
    assert form.lead(older)["cadence_state"] == "terminated"
    assert form.lead(newer)["cadence_state"] == "active"
    assert form.db.execute(
        "select count(*) as n from outreach_events where lead_id=%s and status='planned'",
        (older,)).fetchone()["n"] == 0


def test_the_hourly_cap_stops_a_flood(form, monkeypatch):
    monkeypatch.setenv("DEMO_FORM_MAX_PER_HOUR", "2")
    get_settings.cache_clear()
    assert _submit(_phone()).status_code == 201
    assert _submit(_phone()).status_code == 201
    assert _submit(_phone()).status_code == 429
    assert len(form.leads()) == 2


def test_outside_test_mode_a_form_lead_is_a_real_lead(form, monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    get_settings.cache_clear()
    lead = form.lead(_submit(_phone()).json()["lead_id"])
    assert lead["is_test"] is False and lead["cadence_state"] == "active"
