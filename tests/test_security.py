import hashlib
import hmac
import json
import time

import pytest

from rpt_agent.config import get_settings
from rpt_agent.security import sign_slot, verify_slot


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
