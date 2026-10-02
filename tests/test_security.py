import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rpt_agent import security
from rpt_agent.config import get_settings
from rpt_agent.security import sign_slot, verify_slot


def dashboard_request(
    *, token="x" * 32, user_id="alex.1", name="Alex%20Morgan", role="employee"
) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/dashboard/snapshot",
        "headers": [
            (b"x-dashboard-token", token.encode()),
            (b"x-dashboard-user-id", user_id.encode()),
            (b"x-dashboard-user-name", name.encode()),
            (b"x-dashboard-user-role", role.encode()),
        ],
    })


def test_slot_token_round_trip(monkeypatch):
    monkeypatch.setenv("SLOT_TOKEN_SECRET", "test-secret-that-is-long")
    get_settings.cache_clear()
    token = sign_slot('{"lead_id":"lead-1"}', int(time.time()) + 60)
    payload, expires = verify_slot(token)
    assert payload == '{"lead_id":"lead-1"}'
    assert expires > time.time()


def test_slot_token_tamper_and_expiry(monkeypatch):
    monkeypatch.setenv("SLOT_TOKEN_SECRET", "test-secret-that-is-long")
    get_settings.cache_clear()
    token = sign_slot("payload", int(time.time()) - 1)
    with pytest.raises(ValueError, match="invalid or expired"):
        verify_slot(token)
    valid = sign_slot("payload", int(time.time()) + 60)
    tamper_at = len(valid) // 2
    tampered = valid[:tamper_at] + ("A" if valid[tamper_at] != "A" else "B") + valid[tamper_at + 1:]
    with pytest.raises(ValueError, match="invalid or expired"):
        verify_slot(tampered)


def test_vapi_hmac_authentication(monkeypatch):
    from fastapi.testclient import TestClient

    import rpt_agent.api as api_module

    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "unused-bearer")
    monkeypatch.setenv("VAPI_HMAC_SECRET", "hmac-secret")
    get_settings.cache_clear()
    payload = {"message": {"toolCallList": [{"id": "x", "name": "unknown", "arguments": {}}]}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"hmac-secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    response = TestClient(api_module.app).post(
        "/api/v1/vapi/tools", content=body,
        headers={"Content-Type": "application/json", "X-Vapi-Timestamp": timestamp,
                 "X-Vapi-Signature": signature},
    )
    assert response.status_code == 200


def test_dashboard_auth_accepts_proxy_identity(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    actor = security.require_dashboard_auth(dashboard_request())
    assert (actor.user_id, actor.display_name, actor.role) == (
        "alex.1", "Alex Morgan", "employee",
    )


def test_dashboard_auth_rejects_invalid_role(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        security.require_dashboard_auth(dashboard_request(role="owner"))
    assert exc.value.status_code == 401


def test_dashboard_auth_rejects_identity_without_valid_proxy_token(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        security.require_dashboard_auth(dashboard_request(token="wrong"))
    assert exc.value.status_code == 401


def test_employee_gets_403_from_admin_api(monkeypatch):
    from fastapi.testclient import TestClient

    import rpt_agent.api as api_module

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    try:
        response = TestClient(api_module.app).delete(
            "/api/v1/dashboard/leads/00000000-0000-4000-8000-000000000001",
            headers={
                "X-Dashboard-Token": "x" * 32,
                "X-Dashboard-User-ID": "sarah",
                "X-Dashboard-User-Name": "Sarah%20Johnson",
                "X-Dashboard-User-Role": "employee",
            },
        )
        assert response.status_code == 403
    finally:
        get_settings.cache_clear()
