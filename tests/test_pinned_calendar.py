"""Demo calendar pinned by BOOKING_TODAY_OVERRIDE: a day the agent names lands on the pinned calendar."""
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.services.slot_booking import (
    WHEN_VALUES,
    date_window,
    pinned_calendar_date,
    pinned_calendar_month,
)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))  # pinned day, a Monday
PINNED = date(2026, 6, 15)
REAL_TODAY = date(2026, 10, 21)  # a Wednesday


@pytest.mark.parametrize(("asked", "expected"), [
    (date(2026, 10, 22), date(2026, 6, 18)),  # "this Thursday" -> first Thursday of the pinned calendar
    (date(2026, 10, 27), date(2026, 6, 23)),  # "next Tuesday" (6 days on) -> Tuesday 23 June
    (date(2026, 10, 29), date(2026, 6, 25)),  # "Thursday next week" -> a week after the first one
    (date(2026, 6, 19), date(2026, 6, 19)),   # already a pinned-calendar date (from a tool reply): kept
    (None, None),
])
def test_a_named_day_moves_onto_the_pinned_calendar(asked, expected):
    moved = pinned_calendar_date(asked, NOW, PINNED, 31, real_today=REAL_TODAY)
    assert moved == expected
    if asked and moved != asked:
        assert moved.weekday() == asked.weekday()


def test_without_a_pinned_day_the_date_is_unchanged():
    assert pinned_calendar_date(date(2026, 10, 22), NOW, None, 31, real_today=REAL_TODAY) == date(2026, 10, 22)


def test_a_real_past_day_is_past_even_after_the_pinned_day():
    """Oct 1 (real past, after the pinned June 15) was called "too far ahead" (QA 2026-10-22)."""
    with pytest.raises(ValueError, match="PAST_DATE: Thursday, October 1 has already passed"):
        pinned_calendar_date(date(2026, 10, 1), NOW, PINNED, 31, real_today=REAL_TODAY)


# A named month (when=specific_month, owner 2026-10-23), same rule as a named day.
@pytest.mark.parametrize(("asked", "expected"), [
    (date(2026, 10, 1), date(2026, 6, 1)),   # this (real) month -> the pinned month
    (date(2026, 12, 1), date(2026, 8, 1)),   # "December" said in October -> August on the pinned calendar
    (date(2026, 8, 1), date(2026, 8, 1)),    # already a pinned-calendar month (inside 90 days): kept
    (None, None),
])
def test_a_named_month_moves_onto_the_pinned_calendar(asked, expected):
    assert pinned_calendar_month(asked, NOW, PINNED, 90, real_today=REAL_TODAY) == expected


def test_a_real_past_month_is_past():
    with pytest.raises(ValueError, match="PAST_DATE: May 2026 has already passed"):
        pinned_calendar_month(date(2026, 5, 1), NOW, PINNED, 90, real_today=REAL_TODAY)
    assert pinned_calendar_month(date(2026, 12, 1), NOW, None, 90) == date(2026, 12, 1)


def test_month_windows():
    assert date_window("next_month", NOW, 31) == (date(2026, 7, 1), date(2026, 7, 31))
    assert date_window("next_month", datetime(2026, 12, 20, tzinfo=NOW.tzinfo), 31) == (
        date(2027, 1, 1), date(2027, 1, 31))
    # This month starts today, not on the 1st.
    assert date_window("specific_month", NOW, 31, month=date(2026, 6, 1)) == (
        date(2026, 6, 15), date(2026, 6, 30))
    assert date_window("specific_month", NOW, 31, month=date(2027, 2, 1)) == (
        date(2027, 2, 1), date(2027, 2, 28))
    with pytest.raises(ValueError, match="PAST_DATE: May 2026"):
        date_window("specific_month", NOW, 31, month=date(2026, 5, 1))
    # Default windows are unchanged by the 90-day cache.
    assert date_window("earliest", NOW, 31) == (date(2026, 6, 15), date(2026, 7, 15))


def test_the_vapi_tool_schema_lists_every_when_and_the_month():
    tools = json.loads((Path(__file__).parents[1] / "config" / "vapi_booking_tools.json").read_text())
    find = next(t for t in tools["tools"] if t["function"]["name"] == "find_slots")["function"]
    properties = find["parameters"]["properties"]
    assert tuple(properties["when"]["enum"]) == WHEN_VALUES
    assert "YYYY-MM" in properties["month"]["description"]
