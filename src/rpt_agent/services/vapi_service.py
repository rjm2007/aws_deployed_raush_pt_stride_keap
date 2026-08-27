from typing import Any

from ..observability import WorkflowTrace
from .provider_http import ProviderError, ProviderService


class VapiService(ProviderService):
    def create_call(self, trace: WorkflowTrace, payload: dict[str, Any]) -> str:
        base = self.settings.provider_url("vapi")
        path = "/calls" if self.settings.mode("vapi") == "mock" else "/call"
        headers = (
            {"Authorization": f"Bearer {self.settings.vapi_api_key}"}
            if self.settings.mode("vapi") == "real"
            else {}
        )
        response = self._request(
            trace,
            "vapi",
            "POST",
            base + path,
            operation="create_call",
            json=payload,
            headers=headers,
        )
        if response.status_code not in (200, 201):
            raise self._response_error("vapi", response, idempotent=False)
        call_id = self._json_object(
            "vapi", response, operation="Vapi call", ambiguous=True
        ).get("id")
        if not call_id:
            raise ProviderError(
                "vapi", "missing_id", "Vapi response did not include a call id", ambiguous=True
            )
        return str(call_id)
