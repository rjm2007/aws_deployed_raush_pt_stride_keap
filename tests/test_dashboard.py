from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import rpt_agent.routes.dashboard as dashboard_routes
from rpt_agent.api import app
from rpt_agent.config import get_settings
from rpt_agent.services.delivery import _call_text_artifacts


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class SnapshotConnection:
    def execute(self, sql, params=None):
        del params
        if "from leads l left join lateral" in sql:
            return Result([{
                "id": uuid4(), "full_name": "Synthetic Lead", "phone_e164": "+15550000001",
                "email": None, "source_system": "synthetic_test", "status": "in_progress",
                "cadence_state": "active", "needs_review": False, "review_reason": None,
                "created_at": datetime.now(UTC), "last_contacted_at": None,
                "next_event_id": 1, "next_step": "Day 3 scheduling call", "next_channel": "call",
                "next_scheduled_for": datetime.now(UTC),
            }])
        if "from appointments a" in sql or "from cadence_steps cs" in sql or "from message_templates mt" in sql:
            return Result([])
        return Result([{"provider_queue": 0, "handoff_queue": 0, "unknown_events": 0, "review_queue": 0}])


class CreateLeadConnection:
    def __init__(self):
        self.lead_id = uuid4()
        self.audit_written = False
        self.created_is_test = False

    def execute(self, sql, params=None):
        if "from practices where slug='rausch-pt'" in sql:
            return Result([{"id": 1, "timezone": "America/Los_Angeles"}])
        if "source_system='dashboard'" in sql:
            return Result([])
        if "insert into leads" in sql:
            self.created_is_test = bool(params[-2])
            return Result([{"id": self.lead_id}])
        if "from leads l left join lateral" in sql:
            return Result([{
                "id": self.lead_id,
                "full_name": "Synthetic Lead",
                "phone_e164": "+15550000001",
                "email": "synthetic@example.test",
                "source_system": "dashboard",
                "status": "in_progress",
                "cadence_state": "active",
                "needs_review": False,
                "review_reason": None,
                "created_at": datetime.now(UTC),
                "last_contacted_at": None,
                "date_of_birth": date(1990, 1, 1),
                "referred_by": "Community partner",
                "lead_type": "Wellness",
                "location": "Dana Point",
                "owner": "Sarah Johnson",
                "is_test": self.created_is_test,
                "next_event_id": 1,
                "next_step": "Initial call",
                "next_channel": "call",
                "next_scheduled_for": datetime.now(UTC),
            }])
        if "insert into dashboard_audit_log" in sql:
            self.audit_written = True
            return Result([])
        raise AssertionError(sql)


class DetailConnection:
    lead_id = uuid4()

    def execute(self, sql, params=None):
        del params
        if "from leads where id=" in sql:
            return Result([{
                "id": self.lead_id,
                "practice_id": 1,
                "full_name": "Synthetic Lead",
                "first_name": "Synthetic",
                "last_name": "Lead",
                "phone_e164": "+15550000001",
                "email": None,
                "date_of_birth": date(1990, 1, 1),
                "source_system": "dashboard",
                "status": "in_progress",
                "status_reason": None,
                "cadence_state": "active",
                "call_opt_out": False,
                "sms_opt_out": False,
                "needs_review": False,
                "review_reason": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "last_contacted_at": datetime.now(UTC),
                "callback_requested_at": None,
                "referred_by": None,
                "lead_type": "Physical Therapy",
                "location": "Dana Point",
                "owner": "Test Owner",
                "is_test": True,
            }])
        if "from outreach_events oe" in sql:
            return Result([
                {"id": 1, "cadence_step_id": 1, "channel": "sms", "day_offset": 0,
                 "status": "delivered", "scheduled_for": datetime.now(UTC),
                 "executed_at": datetime.now(UTC), "outcome": None,
                 "description": "Initial SMS"},
                {"id": 2, "cadence_step_id": 2, "channel": "call", "day_offset": 3,
                 "status": "attempted", "scheduled_for": datetime.now(UTC),
                 "executed_at": datetime.now(UTC), "outcome": None,
                 "description": "Follow-up Call"},
            ])
        if any(table in sql for table in (
            "from sms_messages", "from call_logs", "from appointments",
            "from lead_status_history", "from lead_message_overrides",
        )):
            return Result([])
        raise AssertionError(sql)


def test_dashboard_requires_server_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    try:
        response = TestClient(app).get("/api/v1/dashboard/snapshot")
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_dashboard_snapshot_uses_authenticated_actor(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()

    @contextmanager
    def fake_transaction():
        yield SnapshotConnection()

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    try:
        response = TestClient(app).get(
            "/api/v1/dashboard/snapshot",
            headers={
                "X-Dashboard-Token": "x" * 32,
                "X-Dashboard-User-ID": "staff-1",
                "X-Dashboard-User-Email": "staff@example.test",
            },
        )
        assert response.status_code == 200
        assert response.json()["counts"]["cadence"] == 1
    finally:
        get_settings.cache_clear()


def test_dashboard_create_lead_persists_and_materializes(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TEST_MODE", "true")
    get_settings.cache_clear()
    connection = CreateLeadConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    monkeypatch.setattr(dashboard_routes, "materialize_cadence", lambda *args: 8)
    payload = {
        "idempotency_key": "lead-create-1",
        "first_name": "Synthetic",
        "last_name": "Lead",
        "phone": "+15550000001",
        "email": "synthetic@example.test",
        "date_of_birth": "1990-01-01",
        "referred_by": "Community partner",
        "lead_type": "Wellness",
        "location": "Dana Point",
        "owner": "Sarah Johnson",
        "contact_consent": True,
    }
    headers = {
        "X-Dashboard-Token": "x" * 32,
        "X-Dashboard-User-ID": "staff-1",
        "X-Dashboard-User-Email": "staff@example.test",
    }
    try:
        denied = TestClient(app).post(
            "/api/v1/dashboard/leads",
            headers=headers,
            json={**payload, "contact_consent": False},
        )
        assert denied.status_code == 422
        response = TestClient(app).post(
            "/api/v1/dashboard/leads",
            headers=headers,
            json=payload,
        )
        assert response.status_code == 201
        assert response.json()["lead_type"] == "Wellness"
        assert response.json()["cadence_state"] == "active"
        assert response.json()["is_test"] is True
        assert connection.audit_written
    finally:
        get_settings.cache_clear()


def test_dashboard_lead_detail_uses_database_phone_and_event_progress(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    connection = DetailConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(dashboard_routes, "transaction", fake_transaction)
    try:
        response = TestClient(app).get(
            f"/api/v1/dashboard/leads/{connection.lead_id}",
            headers={
                "X-Dashboard-Token": "x" * 32,
                "X-Dashboard-User-ID": "staff-1",
                "X-Dashboard-User-Email": "staff@example.test",
            },
        )
        assert response.status_code == 200
        lead = response.json()["lead"]
        assert lead["phone"] == "+15550000001"
        assert lead["cadence_progress"] == 2
        assert lead["cadence_total"] == 2
        assert lead["next_event_status"] == "attempted"
        assert lead["next_step"] == "Awaiting result: Follow-up Call"
    finally:
        get_settings.cache_clear()


def test_dashboard_migration_and_text_only_call_artifacts():
    sql = Path("supabase/migrations/016_dashboard_security_and_transcripts.sql").read_text(
        encoding="utf-8"
    )
    assert "transcript_text" in sql and "dashboard_audit_log" in sql
    intake_sql = Path("supabase/migrations/017_dashboard_lead_intake.sql").read_text(
        encoding="utf-8"
    )
    assert "lead_type" in intake_sql and "referred_by" in intake_sql
    consent_sql = Path("supabase/migrations/018_dashboard_staff_attestation.sql").read_text(
        encoding="utf-8"
    )
    assert "dashboard_staff_attestation" in consent_sql
    transcript, summary = _call_text_artifacts({
        "artifact": {"transcript": "Assistant: Hello\nPatient: Hi"},
        "analysis": {"summary": "Requested a callback."},
    })
    assert transcript == "Assistant: Hello\nPatient: Hi"
    assert summary == "Requested a callback."
