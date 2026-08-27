"""Documentation-only request models.

The endpoints parse the raw body themselves so that one URL accepts both the Vapi
tool-call envelope and a flat JSON object. Binding these models as real body
parameters would reject the envelope, so they are attached to the OpenAPI schema
only - they give Swagger an editable example without touching runtime parsing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CheckAvailabilityBody(BaseModel):
    lead_id: str = Field(examples=["dfd7ec12-8ac4-4dc8-a177-a32e106cb842"])
    date: str | None = Field(
        default=None,
        description="YYYY-MM-DD. Omit to get the next openings from today.",
        examples=["2026-07-28"],
    )
    time: str | None = Field(
        default=None,
        description="Optional preferred time: 9 AM, 2:30 PM, or 14:30.",
        examples=["9:00 AM"],
    )


class CreateAppointmentBody(BaseModel):
    lead_id: str = Field(examples=["dfd7ec12-8ac4-4dc8-a177-a32e106cb842"])
    date: str = Field(description="YYYY-MM-DD.", examples=["2026-07-28"])
    time: str = Field(description="Must be a slot returned by check-availability.", examples=["9:00 AM"])
    first_name: str | None = Field(default=None, description="Only used when the lead record lacks it.")
    last_name: str | None = Field(default=None, description="Only used when the lead record lacks it.")
    date_of_birth: str | None = Field(
        default=None, description="YYYY-MM-DD. Only used when the lead record lacks it."
    )


class UpdateLeadStatusBody(BaseModel):
    lead_id: str = Field(examples=["dfd7ec12-8ac4-4dc8-a177-a32e106cb842"])
    status: str = Field(
        description=(
            "booked, declined, callback_scheduled, booking_link, transferred_human, "
            "no_answer, wrong_person, call_opt_out, or do_not_contact."
        ),
        examples=["no_answer"],
    )
    call_id: str | None = Field(
        default=None,
        description="Vapi call id. Required unless outreach_event_id is supplied.",
        examples=["manual-test-1"],
    )
    notes: str | None = Field(default=None, description="Short operational note, no medical detail.")
    callback_requested_at: str | None = Field(
        default=None,
        description="Required for callback_scheduled: timezone-aware ISO 8601 within 30 days.",
        examples=["2026-07-28T18:00:00+00:00"],
    )


def documented_body(model: type[BaseModel]) -> dict[str, Any]:
    """Attach a model to a route's OpenAPI entry without binding it at runtime."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }
