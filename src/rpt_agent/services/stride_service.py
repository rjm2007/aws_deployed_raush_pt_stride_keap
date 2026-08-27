import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..observability import WorkflowTrace
from .provider_http import ProviderError, ProviderService


@dataclass(frozen=True)
class Slot:
    clinician_id: int
    timezone: str
    local_date: str
    local_time: str


class StrideService(ProviderService):
    def availability(
        self,
        trace: WorkflowTrace,
        *,
        location: int,
        duration: int,
        clinician_ids: str,
        start_date: date,
        end_date: date,
    ) -> list[Slot]:
        response = self._request(
            trace,
            "stride",
            "GET",
            self.settings.provider_url("stride") + "/v1/scheduling/availabilities/",
            operation="get_availability",
            params={
                "location": location,
                "duration": duration,
                "clinician_ids": clinician_ids,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            headers=(
                {"Authorization": f"Token {self.settings.stride_api_token}"}
                if self.settings.mode("stride") == "real"
                else {}
            ),
        )
        if response.status_code != 200:
            raise self._response_error("stride", response, idempotent=True)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "stride", "malformed_response", "Stride availability was not valid JSON"
            ) from exc
        if not isinstance(data, list):
            raise ProviderError(
                "stride", "malformed_response", "Stride availability was not a list"
            )
        slots: list[Slot] = []
        try:
            for clinician in data:
                clinician_id = clinician.get("clinician_id", clinician.get("clinicianId"))
                timezone = clinician["timezone"]
                for day, times in clinician.items():
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                        continue
                    if not isinstance(times, list) or not all(
                        isinstance(value, str) for value in times
                    ):
                        raise TypeError("availability date values must be string lists")
                    slots.extend(
                        Slot(int(clinician_id), timezone, day, value)
                        for value in times
                    )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                "stride", "malformed_response", "Stride availability had an unexpected structure"
            ) from exc
        return slots

    def create(self, trace: WorkflowTrace, resource: str, payload: dict[str, Any]) -> int:
        response = self._request(
            trace,
            "stride",
            "POST",
            self.settings.provider_url("stride") + f"/v1/{resource}/",
            operation=f"create_{resource.rstrip('s')}",
            json=payload,
            headers=(
                {"Authorization": f"Token {self.settings.stride_api_token}"}
                if self.settings.mode("stride") == "real"
                else {}
            ),
        )
        if response.status_code != 200:
            try:
                data = response.json() if response.content else {}
                raw_detail = (
                    data.get("detail", response.text[:200])
                    if isinstance(data, dict)
                    else response.text[:200]
                )
            except ValueError:
                raw_detail = response.text[:200]
            normalized_detail = str(raw_detail).lower()
            if "overlap" in normalized_detail:
                detail = "overlapping appointment"
            elif "already exists" in normalized_detail or "duplicate" in normalized_detail:
                detail = "already exists"
            else:
                detail = f"Stride rejected {resource} with HTTP {response.status_code}"
            classified = self._response_error("stride", response, idempotent=False)
            raise ProviderError(
                "stride",
                classified.code,
                str(detail),
                ambiguous=classified.ambiguous,
                retryable=classified.retryable,
                retry_after_seconds=classified.retry_after_seconds,
            )
        resource_id = self._json_object(
            "stride", response, operation=f"Stride {resource}", ambiguous=True
        ).get("id")
        if not resource_id:
            raise ProviderError(
                "stride", "missing_id", f"Stride {resource} response omitted id", ambiguous=True
            )
        try:
            return int(resource_id)
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                "stride", "malformed_response", f"Stride {resource} id was invalid", ambiguous=True
            ) from exc
