from datetime import date

import httpx
import pytest

from rpt_agent.config import Settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderClients, ProviderError


def test_mock_adapter_propagates_trace_and_requires_provider_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["trace"] = request.headers["X-Trace-ID"]
        return httpx.Response(201, json={"id": "mock-call-123"})

    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    call_id = clients.create_vapi_call(WorkflowTrace("provider", "test", "trace-provider"), {})
    assert call_id == "mock-call-123"
    assert seen["trace"] == "trace-provider"


def test_missing_provider_id_is_failure():
    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={}))),
    )
    try:
        clients.create_vapi_call(WorkflowTrace("provider", "test", "trace-missing"), {})
    except ProviderError as exc:
        assert exc.code == "missing_id"
        assert exc.ambiguous is True
    else:
        raise AssertionError("missing provider id must fail")


def test_real_vapi_uses_current_call_endpoint_and_bearer_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(201, json={"id": "real-call-123"})

    clients = ProviderClients(
        Settings(
            provider_mode="mock",
            vapi_mode="real",
            vapi_base_url="https://api.vapi.test",
            vapi_api_key="test-key",
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    call_id = clients.create_vapi_call(WorkflowTrace("provider", "test", "trace-real"), {})
    assert call_id == "real-call-123"
    assert seen == {
        "url": "https://api.vapi.test/call",
        "authorization": "Bearer test-key",
    }


def test_provider_modes_can_mix_real_vapi_with_mock_stride():
    settings = Settings(
        provider_mode="mock",
        vapi_mode="real",
        stride_mode="mock",
        mock_base_url="http://mock.test",
    )
    assert settings.provider_url("vapi") == "https://api.vapi.ai"
    assert settings.provider_url("stride") == "http://mock.test/mock/stride"


def test_real_twilio_message_includes_public_status_callback():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(
            item.split("=", 1) for item in request.content.decode().split("&")
        )
        return httpx.Response(201, json={"sid": "SM-test"})

    settings = Settings(
        provider_mode="mock",
        twilio_mode="real",
        twilio_account_sid="AC-test",
        twilio_auth_token="token",
        twilio_from_number="+15005550006",
        public_base_url="https://public.example.test",
    )
    clients = ProviderClients(
        settings,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert clients.send_sms(
        WorkflowTrace("provider", "test", "trace-twilio"),
        "+15555550123",
        "synthetic message",
    ) == "SM-test"
    assert seen["url"].endswith("/Accounts/AC-test/Messages.json")
    assert seen["form"]["StatusCallback"] == (
        "https%3A%2F%2Fpublic.example.test%2Fapi%2Fv1%2Ftwilio%2Fmessage-status"
    )


def test_stride_availability_accepts_live_camel_case_clinician_id():
    response = [{
        "timezone": "US/Eastern",
        "clinicianId": 5981,
        "2026-08-27": ["09:00:00"],
    }]
    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response)
        )),
    )
    slots = clients.stride_availability(
        WorkflowTrace("provider", "test"),
        location=3169,
        duration=60,
        clinician_ids="5981",
        start_date=date(2026, 8, 27),
        end_date=date(2026, 8, 27),
    )
    assert slots[0].clinician_id == 5981


def test_rate_limit_is_retried_when_provider_rejected_request(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="slow down")
        return httpx.Response(201, json={"id": "call-after-retry"})

    monkeypatch.setattr("rpt_agent.services.provider_http.time.sleep", lambda _: None)
    clients = ProviderClients(
        Settings(
            provider_mode="mock",
            mock_base_url="http://mock.test",
            http_retry_attempts=2,
            http_retry_base_seconds=0.1,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert clients.create_vapi_call(WorkflowTrace("provider", "test"), {}) == "call-after-retry"
    assert attempts == 2


def test_post_503_is_ambiguous_and_is_not_blindly_retried():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="unknown provider result")

    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as raised:
        clients.create_vapi_call(WorkflowTrace("provider", "test"), {})
    assert raised.value.ambiguous is True
    assert raised.value.retryable is False
    assert "unknown provider result" not in str(raised.value)
    assert attempts == 1


def test_long_retry_after_is_deferred_to_durable_worker():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")

    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as raised:
        clients.create_vapi_call(WorkflowTrace("provider", "test"), {})
    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds == 120
    assert attempts == 1
