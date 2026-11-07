"""Public demo form route (demo.aibolt.ai/try, 2026-11-07): switch, service token, input checks.

No database: the service is replaced, so these only cover what the route accepts.
"""

import pytest
from fastapi.testclient import TestClient

from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.routes import demo_form
from rpt_agent.routes.demo_form import national_number

TOKEN = "t" * 32
BODY = {"idempotency_key": "demo-key-0001", "first_name": " Jane ", "last_name": "Smith",
        "country_code": "+1", "phone": "(949) 555-0123", "date_of_birth": "1990-01-16",
        "contact_consent": True}


@pytest.fixture
def started(monkeypatch):
    monkeypatch.setenv("DEMO_FORM_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    calls = []

    def fake_start(**kwargs):
        calls.append(kwargs)
        return "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr(demo_form, "start_demo_lead", fake_start)
    yield calls
    get_settings.cache_clear()


def _post(body=None, token=TOKEN):
    return TestClient(app).post("/api/v1/demo-form/leads", json=body or BODY,
                                headers={"X-Dashboard-Token": token})


def test_the_form_is_off_unless_switched_on(monkeypatch):
    monkeypatch.setenv("DEMO_FORM_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    try:
        assert _post().status_code == 404
    finally:
        get_settings.cache_clear()


def test_only_the_dashboard_server_may_call_it(started):
    assert _post(token="wrong").status_code == 401
    assert TestClient(app).post("/api/v1/demo-form/leads", json=BODY).status_code == 401
    assert started == []


def test_a_valid_form_starts_a_lead_with_the_full_number(started):
    response = _post()
    assert response.status_code == 201
    assert response.json() == {"lead_id": "11111111-1111-1111-1111-111111111111",
                               "first_name": "Jane"}
    (call,) = started
    assert call["phone"] == "+19495550123"
    assert call["first_name"] == "Jane" and call["last_name"] == "Smith"
    assert str(call["date_of_birth"]) == "1990-01-16"


def test_an_indian_number_is_kept_with_its_code(started):
    assert _post({**BODY, "country_code": "+91", "phone": "098765 43210"}).status_code == 201
    assert started[0]["phone"] == "+919876543210"


@pytest.mark.parametrize("change", [
    {"phone": "555-0123"},                      # too short
    {"country_code": "+44"},                    # not offered
    {"contact_consent": False},
    {"first_name": "   "},
    {"date_of_birth": "2099-01-01"},
    {"date_of_birth": "not a date"},
])
def test_bad_input_starts_nothing(started, change):
    assert _post({**BODY, **change}).status_code == 422
    assert started == []


def test_a_birthday_under_a_year_back_is_refused(started):
    # The booking engine refuses it later anyway (SMS agent rule, 2026-11-03).
    from datetime import UTC, datetime
    today = datetime.now(UTC).date().isoformat()
    assert _post({**BODY, "date_of_birth": today}).status_code == 422


@pytest.mark.parametrize(("code", "typed", "expected"), [
    ("+1", "9495550123", "9495550123"),
    ("+1", "+1 (949) 555-0123", "9495550123"),
    ("+1", "1-949-555-0123", "9495550123"),
    ("+91", "98765 43210", "9876543210"),
    ("+91", "+91 98765 43210", "9876543210"),
    ("+91", "098765 43210", "9876543210"),
    ("+1", "94955501", None),
    ("+91", "123", None),
])
def test_national_number(code, typed, expected):
    assert national_number(code, typed) == expected
