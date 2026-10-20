"""Demo calendar pinned by BOOKING_TODAY_OVERRIDE: a day the agent names lands on the pinned calendar."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.services.slot_booking import pinned_calendar_date

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))  # pinned day, a Monday
PINNED = date(2026, 6, 15)
REAL_TODAY = date(2026, 10, 21)  # a Wednesday


@pytest.mark.parametrize(("asked", "expected"), [
    (date(2026, 10, 22), date(2026, 6, 18)),  # "this Thursday" -> first Thursday of the pinned calendar
    (date(2026, 10, 27), date(2026, 6, 23)),  # "next Tuesday" (6 days on) -> Tuesday 23 June
    (date(2026, 10, 29), date(2026, 6, 25)),  # "Thursday next week" -> a week after the first one
    (date(2026, 6, 19), date(2026, 6, 19)),   # already a pinned-calendar date (from a tool reply): kept
    (date(2026, 10, 1), date(2026, 10, 1)),   # a real past day stays past (the tool says PAST_DATE)
    (None, None),
])
def test_a_named_day_moves_onto_the_pinned_calendar(asked, expected):
    moved = pinned_calendar_date(asked, NOW, PINNED, 31, real_today=REAL_TODAY)
    assert moved == expected
    if asked and moved != asked:
        assert moved.weekday() == asked.weekday()


def test_without_a_pinned_day_the_date_is_unchanged():
    assert pinned_calendar_date(date(2026, 10, 22), NOW, None, 31, real_today=REAL_TODAY) == date(2026, 10, 22)
