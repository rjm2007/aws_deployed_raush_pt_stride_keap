from datetime import date

from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import Slot
from rpt_agent.services.booking import BookingService

CONTEXT = {
    "stride_location_id": 3169,
    "stride_default_duration_mins": 60,
    "stride_clinician_ids": "5981,5982",
    "stride_location_timezone": "America/Los_Angeles",
}


class FakeProviders:
    def stride_availability(self, trace, **kwargs):
        # Two clinicians free at the same wall-clock times.
        return [
            Slot(clinician, "America/Los_Angeles", "2026-08-27", value)
            for clinician in (5981, 5982)
            for value in ("09:00:00", "10:30:00", "13:30:00")
        ]


def _message(**kwargs) -> str:
    service = BookingService(FakeProviders())
    service._booking_context = staticmethod(lambda *a, **k: dict(CONTEXT))
    return service.availability_message(
        WorkflowTrace("test", "test"),
        lead_id="lead-1",
        practice_id=None,
        **kwargs,
    )


def test_shared_clinician_times_are_offered_once():
    message = _message(requested_date=None, requested_time=None)
    assert message.count("9:00 AM") == 1
    assert "10:30 AM" in message


def test_nearest_alternatives_are_spoken_in_clock_order():
    message = _message(requested_date=date(2026, 8, 27), requested_time="11:00")
    assert message.index("9:00 AM") < message.index("10:30 AM")


def test_exact_open_slot_is_confirmed():
    message = _message(requested_date=date(2026, 8, 27), requested_time="9 AM")
    assert "is open" in message
