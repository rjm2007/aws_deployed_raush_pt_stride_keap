"""Test chat setup extras (services/sms_agent_sandbox_setup.py): the booked visit and the open review
map to the LeadView fields live reads (2026-11-02 eval: bk-02, ls-01). Pure; no database."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from rpt_agent.services.sms_agent_context import LeadView, context_block
from rpt_agent.services.sms_agent_sandbox_setup import (
    FRONT_DESK_REASON,
    FULL_REVIEW_REASON,
    apply_setup_extras,
    booked_visit_text,
)


@dataclass
class Setup:
    booked_visit: str = ""
    open_review: str = "auto"


def _lead(status="in_progress", cadence="active", new=False, **changes):
    values = {
        "is_new_person": new, "first_name": "Sam", "last_name": "Test", "date_of_birth": None,
        "clinic": "Laguna Niguel", "service": "Physical Therapy", "referral_word": "knee",
        "status": status, "cadence_state": cadence, "needs_review": status == "needs_attention",
        "review_reason": "made-up review (test chat)" if status == "needs_attention" else "",
        "booked_visit": "booked (test chat: no visit details set)" if status == "booked" else "",
    }
    return LeadView(**{**values, **changes})


def test_booked_visit_text_matches_the_live_shape():
    at = datetime(2026, 11, 3, 9, 0)  # noqa: DTZ001 - clinic wall time
    assert booked_visit_text(at, "Alan", "Laguna Niguel") == (
        "Tue Nov 3 at 9:00 AM with Alan at Laguna Niguel")
    assert booked_visit_text(datetime(2026, 11, 12, 14, 30), " ", "") == "Thu Nov 12 at 2:30 PM"  # noqa: DTZ001


def test_booked_visit_replaces_the_placeholder_on_a_booked_lead():
    lead = apply_setup_extras(_lead("booked", "completed"), Setup(booked_visit="Tue Nov 3 at 9:00 AM"))
    assert lead.booked_visit == "Tue Nov 3 at 9:00 AM"
    assert lead.status == "booked" and not lead.needs_review


def test_booked_visit_is_ignored_on_a_lead_that_is_not_booked():
    lead = apply_setup_extras(_lead(), Setup(booked_visit="Tue Nov 3 at 9:00 AM"))
    assert lead.booked_visit == ""


def test_auto_and_a_setup_without_the_fields_change_nothing():
    lead = _lead("needs_attention", "paused")
    assert apply_setup_extras(lead, Setup()) is lead
    assert apply_setup_extras(lead, object()) is lead


def test_front_desk_only_pauses_a_lead_in_cadence_but_booking_goes_on():
    lead = apply_setup_extras(_lead(), Setup(open_review="front_desk"))
    assert (lead.status, lead.cadence_state) == ("needs_attention", "paused")
    assert lead.needs_review and lead.front_desk_only and not lead.booking_paused
    assert lead.review_reason == FRONT_DESK_REASON
    block = context_block(lead, now=datetime(2026, 11, 2, 17, 0, tzinfo=UTC), front_desk_open=True,
                          booking_link="", front_desk_phone="(949) 555-0100",
                          timezone="America/Los_Angeles")
    assert "front desk will reach out about a question" in block
    assert "front desk only (" in block and "booking by text still works" in block


def test_front_desk_only_on_needs_attention_is_not_a_full_review():
    lead = apply_setup_extras(_lead("needs_attention", "paused"), Setup(open_review="front_desk"))
    assert lead.front_desk_only and not lead.booking_paused


def test_full_review_stops_booking():
    lead = apply_setup_extras(_lead(), Setup(open_review="full"))
    assert (lead.status, lead.cadence_state) == ("needs_attention", "paused")
    assert lead.booking_paused and lead.review_reason == FULL_REVIEW_REASON


@pytest.mark.parametrize("status", ["booked", "declined", "closed_no_response", "invalid_phone"])
def test_a_card_keeps_a_final_status(status):
    lead = apply_setup_extras(_lead(status, "completed"), Setup(open_review="front_desk"))
    assert lead.status == status and lead.cadence_state == "completed"
    assert lead.needs_review and lead.front_desk_only


def test_none_clears_the_review_of_needs_attention():
    lead = apply_setup_extras(_lead("needs_attention", "paused"), Setup(open_review="none"))
    assert not lead.needs_review and not lead.booking_paused and lead.review_reason == ""


def test_a_new_person_is_left_alone():
    lead = _lead("new", "pending", new=True)
    assert apply_setup_extras(lead, Setup(open_review="full", booked_visit="x")) is lead
