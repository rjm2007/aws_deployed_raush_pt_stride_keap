import json
from typing import Any

from ..observability import WorkflowTrace
from ..security import sign_handoff
from .provider_http import ProviderService


class KeapService(ProviderService):
    def deliver_handoff(self, trace: WorkflowTrace, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        response = self._request(
            trace,
            "keap",
            "POST",
            self.settings.keap_handoff_url,
            operation="deliver_appointment_handoff",
            content=body,
            headers={"Content-Type": "application/json", "X-RPT-Signature": sign_handoff(body)},
            idempotent=True,
        )
        if response.status_code not in (200, 201, 202, 204):
            raise self._response_error("keap_handoff", response, idempotent=True)
