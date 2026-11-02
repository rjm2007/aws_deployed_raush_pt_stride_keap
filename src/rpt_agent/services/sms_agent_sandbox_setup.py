"""Test chat setup extras: a booked visit and the open review (docs/SMS_AGENT.md "Test chat tab").

The 2026-11-02 scenario run could not set up two things live leads have: a Booked lead's visit (the
sandbox only said "no visit details set") and Elliot's own front-desk card (status Needs attention
always meant a full review that stops booking). These helpers turn the tab's choices into the same
LeadView fields `sms_agent_context.load_live` reads for a real lead. Pure: no database, no clock.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from .lead_review import SMS_FRONT_DESK_REASONS
from .sms_agent_context import LeadView

# 'auto': as before, status Needs attention = a full review, any other status = none.
OPEN_REVIEW_CHOICES = ("auto", "none", "front_desk", "full")
# The reason the made-up front-desk card shows (live: the card's reason words, joined).
FRONT_DESK_REASON = SMS_FRONT_DESK_REASONS["unknown_question"]
FULL_REVIEW_REASON = "made-up review (test chat)"
# Statuses an open card leaves alone (live: open_front_desk_card / flag_for_review pause only a lead
# still in the cadence).
_KEEPS_STATUS = {"booked", "declined", "do_not_contact", "closed_no_response", "invalid_phone"}
_CADENCE_DONE = {"completed", "terminated"}


def booked_visit_text(at: datetime, therapist: str = "", clinic: str = "") -> str:
    """'Tue Nov 3 at 9:00 AM with Alan at Laguna Niguel', the shape live's booked_visit() writes.
    `at` is the clinic's wall time."""
    text = f"{at:%a} {at:%b} {at.day} at {at:%I:%M %p}".replace(" 0", " ")
    if therapist.strip():
        text += f" with {therapist.strip()}"
    if clinic.strip():
        text += f" at {clinic.strip()}"
    return text


def apply_setup_extras(lead: LeadView, setup: Any) -> LeadView:
    """The made-up lead with the setup's booked visit and open review. `setup` is a SandboxSetup;
    a setup without these fields (or with the defaults) leaves the lead as it is."""
    if lead.is_new_person:
        return lead
    updates: dict[str, Any] = {}
    visit = str(getattr(setup, "booked_visit", "") or "").strip()
    if visit and lead.status == "booked":
        updates["booked_visit"] = visit
    choice = str(getattr(setup, "open_review", "auto") or "auto")
    if choice == "none":
        updates.update(needs_review=False, review_reason="", front_desk_only=False)
    elif choice in {"front_desk", "full"}:
        front_desk = choice == "front_desk"
        updates.update(needs_review=True, front_desk_only=front_desk,
                       review_reason=FRONT_DESK_REASON if front_desk else FULL_REVIEW_REASON)
        if lead.status not in _KEEPS_STATUS and lead.cadence_state not in _CADENCE_DONE:
            # Any open card pauses a lead in the cadence (status needs_attention), like live.
            updates.update(status="needs_attention", cadence_state="paused")
    return replace(lead, **updates) if updates else lead
