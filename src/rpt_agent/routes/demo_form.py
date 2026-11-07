"""Public demo form (demo.aibolt.ai/try, conference demo 2026-11-07).

The page is public, so the browser never calls this route: the dashboard server
(`rpt_frontend/app/api/try/route.ts`) forwards the form with the dashboard service token. Off unless
`DEMO_FORM_ENABLED`; never set it in production.
"""

from __future__ import annotations

import hmac
import re
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..parsing import parse_flexible_date
from ..services.demo_form import DemoFormError, start_demo_lead

router = APIRouter(prefix="/api/v1/demo-form", tags=["demo"])

# Country code -> digits a mobile number has after it. The demo calls US numbers from the US number
# and Indian numbers (our testers) from the other one (config.vapi_phone_number_for).
NATIONAL_DIGITS = {"+1": 10, "+91": 10}


def require_demo_form(request: Request) -> None:
    """Switched on, and called by the dashboard server (service token), not a browser."""
    settings = get_settings()
    if not settings.demo_form_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    expected = settings.dashboard_api_token
    supplied = request.headers.get("x-dashboard-token", "")
    if not expected:
        raise HTTPException(status_code=503, detail="dashboard API is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid dashboard credentials")


def national_number(country_code: str, phone: str) -> str | None:
    """The digits after the country code, or None when it is not a mobile number of that country."""
    digits = re.sub(r"\D", "", phone)
    code = country_code.removeprefix("+")
    # "+1 949..." or "1949..." typed into the number box: drop the repeated country code.
    if len(digits) == NATIONAL_DIGITS[country_code] + len(code) and digits.startswith(code):
        digits = digits[len(code):]
    # Indian numbers are often written with a trunk 0 in front.
    if country_code == "+91" and len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == NATIONAL_DIGITS[country_code] else None


class DemoLead(BaseModel):
    # One per page load: a double tap or a retry makes one lead.
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    country_code: Literal["+1", "+91"]
    phone: str = Field(min_length=7, max_length=32)
    date_of_birth: date
    contact_consent: Literal[True]

    @field_validator("first_name", "last_name")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def parse_date_of_birth(cls, value: date | datetime | str) -> date:
        parsed = parse_flexible_date(value)
        if parsed is None:
            raise ValueError("date_of_birth is required")
        return parsed

    @field_validator("date_of_birth")
    @classmethod
    def valid_date_of_birth(cls, value: date) -> date:
        # The booking engine refuses a birthday less than a year back (SMS agent rule, 2026-11-03).
        today = datetime.now(UTC).date()
        if value > today.replace(year=today.year - 1, day=min(today.day, 28)) or value.year < 1900:
            raise ValueError("date_of_birth is not a real birth date")
        return value


@router.post("/leads", status_code=201, dependencies=[Depends(require_demo_form)])
async def create_demo_lead(payload: DemoLead):
    national = national_number(payload.country_code, payload.phone)
    if not national:
        raise HTTPException(status_code=422, detail="phone is not a valid mobile number")
    try:
        lead_id = await run_in_threadpool(
            start_demo_lead,
            idempotency_key=payload.idempotency_key,
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone=f"{payload.country_code}{national}",
            phone_original=f"{payload.country_code} {payload.phone.strip()}",
            date_of_birth=payload.date_of_birth,
        )
    except DemoFormError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"lead_id": lead_id, "first_name": payload.first_name}
