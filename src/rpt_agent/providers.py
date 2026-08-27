"""Small compatibility facade over the provider-specific service files."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from .config import Settings, get_settings
from .observability import WorkflowTrace
from .services.keap_service import KeapService
from .services.provider_http import ProviderError
from .services.stride_service import Slot, StrideService
from .services.twilio_service import TwilioService
from .services.vapi_service import VapiService


class ProviderClients:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        shared_client = client or httpx.Client(timeout=self.settings.request_timeout_seconds)
        options = {"audit_enabled": client is None}
        self.vapi = VapiService(self.settings, shared_client, **options)
        self.twilio = TwilioService(self.settings, shared_client, **options)
        self.stride = StrideService(self.settings, shared_client, **options)
        self.keap = KeapService(self.settings, shared_client, **options)

    def create_vapi_call(self, trace: WorkflowTrace, payload: dict[str, Any]) -> str:
        return self.vapi.create_call(trace, payload)

    def send_sms(self, trace: WorkflowTrace, to: str, body: str) -> str:
        return self.twilio.send_sms(trace, to, body)

    def stride_availability(
        self,
        trace: WorkflowTrace,
        *,
        location: int,
        duration: int,
        clinician_ids: str,
        start_date: date,
        end_date: date,
    ) -> list[Slot]:
        return self.stride.availability(
            trace,
            location=location,
            duration=duration,
            clinician_ids=clinician_ids,
            start_date=start_date,
            end_date=end_date,
        )

    def stride_create(self, trace: WorkflowTrace, resource: str, payload: dict[str, Any]) -> int:
        return self.stride.create(trace, resource, payload)

    def deliver_handoff(self, trace: WorkflowTrace, payload: dict[str, Any]) -> None:
        self.keap.deliver_handoff(trace, payload)


__all__ = ["ProviderClients", "ProviderError", "Slot"]
