from __future__ import annotations

import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..db import record_integration_event
from ..observability import WorkflowTrace


class ProviderError(RuntimeError):
    def __init__(
        self,
        provider: str,
        code: str,
        message: str,
        *,
        ambiguous: bool = False,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.ambiguous = ambiguous
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class ProviderService:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        audit_enabled: bool = True,
    ):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=self.settings.request_timeout_seconds)
        self.audit_enabled = audit_enabled

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("retry-after", "").strip()
        if not value:
            return None
        try:
            return max(1, int(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return max(1, int((parsed - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                return None

    def _request(
        self,
        trace: WorkflowTrace,
        provider: str,
        method: str,
        url: str,
        *,
        idempotent: bool | None = None,
        operation: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        trace.log("provider_request_started", provider=provider, method=method, url=url)
        headers = dict(kwargs.pop("headers", {}))
        headers["X-Trace-ID"] = trace.trace_id
        if self.settings.mode(provider) == "mock":
            headers["X-Mock-Scenario"] = self.settings.mock_scenario
        safe_to_retry = method.upper() in {"GET", "HEAD", "OPTIONS"} if idempotent is None else idempotent
        transient_statuses = {408, 425, 500, 502, 503, 504}
        attempts = self.settings.http_retry_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                if self.audit_enabled:
                    record_integration_event(
                        trace.trace_id,
                        "outbound",
                        provider,
                        operation or f"{method.upper()} request",
                        "failed",
                        error_category=type(exc).__name__,
                    )
                if safe_to_retry and attempt < attempts:
                    delay = min(
                        self.settings.http_retry_base_seconds * (2 ** (attempt - 1)), 5.0
                    ) * random.uniform(1.0, 1.25)
                    trace.log(
                        "provider_retry_scheduled",
                        provider=provider,
                        attempt=attempt,
                        retry_in_seconds=round(delay, 2),
                        error_category=type(exc).__name__,
                    )
                    time.sleep(delay)
                    continue
                event = (
                    "provider_timeout"
                    if isinstance(exc, httpx.TimeoutException)
                    else "provider_transport_error"
                )
                trace.log(event, provider=provider, error_category=type(exc).__name__)
                raise ProviderError(
                    provider,
                    "timeout" if isinstance(exc, httpx.TimeoutException) else "transport",
                    f"{provider} request "
                    f"{'timed out' if isinstance(exc, httpx.TimeoutException) else 'failed'}",
                    ambiguous=not safe_to_retry,
                    retryable=safe_to_retry,
                ) from exc
            trace.log(
                "provider_response_received",
                provider=provider,
                status_code=response.status_code,
                request_id=response.headers.get("x-request-id", ""),
            )
            if self.audit_enabled:
                record_integration_event(
                    trace.trace_id,
                    "outbound",
                    provider,
                    operation or f"{method.upper()} request",
                    "accepted" if response.is_success else "rejected",
                    http_status=response.status_code,
                )
            retryable_response = response.status_code == 429 or (
                safe_to_retry and response.status_code in transient_statuses
            )
            if retryable_response and attempt < attempts:
                retry_after = self._retry_after(response)
                if retry_after and retry_after > 5:
                    return response
                delay = retry_after or min(
                    self.settings.http_retry_base_seconds * (2 ** (attempt - 1)), 5.0
                )
                delay *= random.uniform(1.0, 1.25)
                trace.log(
                    "provider_retry_scheduled",
                    provider=provider,
                    attempt=attempt,
                    status_code=response.status_code,
                    retry_in_seconds=round(delay, 2),
                )
                time.sleep(delay)
                continue
            return response
        raise RuntimeError("provider retry loop exited unexpectedly")

    def _response_error(
        self, provider: str, response: httpx.Response, *, idempotent: bool
    ) -> ProviderError:
        transient = response.status_code in {408, 425, 500, 502, 503, 504}
        return ProviderError(
            provider,
            str(response.status_code),
            f"{provider} returned HTTP {response.status_code}",
            ambiguous=transient and not idempotent,
            retryable=response.status_code == 429 or (transient and idempotent),
            retry_after_seconds=self._retry_after(response),
        )

    @staticmethod
    def _json_object(
        provider: str, response: httpx.Response, *, operation: str, ambiguous: bool
    ) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise ProviderError(
                provider,
                "malformed_response",
                f"{operation} response was not valid JSON",
                ambiguous=ambiguous,
            ) from exc
        if not isinstance(value, dict):
            raise ProviderError(
                provider,
                "malformed_response",
                f"{operation} response was not an object",
                ambiguous=ambiguous,
            )
        return value
