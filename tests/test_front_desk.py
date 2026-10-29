"""Front desk open now? (services/front_desk.py, owner 2026-10-29): Elliot offers a live transfer
only while the lead's clinic's front desk is open, with 10 minutes' margin before closing."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from rpt_agent.services.front_desk import front_desk_open, front_desk_open_word

PT = ZoneInfo("America/Los_Angeles")


def at(text: str) -> datetime:
    """A clinic wall time ("2026-10-30 07:00") as an aware moment, converted to UTC like `now`."""
    return datetime.fromisoformat(text).replace(tzinfo=PT).astimezone(ZoneInfo("UTC"))


@pytest.mark.parametrize("location", ["Laguna Niguel", "Dana Point", "dana point office"])
@pytest.mark.parametrize(("when", "open_"), [
    ("2026-10-30 06:59", False),  # Friday, before opening
    ("2026-10-30 07:00", True),
    ("2026-10-30 18:49", True),
    ("2026-10-30 18:50", False),  # last 10 minutes before 19:00
    ("2026-10-31 07:00", True),   # Saturday
    ("2026-10-31 13:19", True),
    ("2026-10-31 13:20", False),  # Saturday closes 13:30
    ("2026-11-01 10:00", False),  # Sunday
])
def test_laguna_niguel_and_dana_point_hours(location, when, open_):
    assert front_desk_open(location, at(when)) is open_


@pytest.mark.parametrize(("when", "open_"), [
    ("2026-10-30 16:49", True),
    ("2026-10-30 16:50", False),  # Mission Viejo closes 17:00 on weekdays
    ("2026-10-31 09:00", False),  # and on Saturday
])
def test_mission_viejo_hours(when, open_):
    assert front_desk_open("Mission Viejo", at(when)) is open_


@pytest.mark.parametrize("location", [None, "", "Irvine"])
def test_unknown_location_uses_laguna_niguel_hours(location):
    assert front_desk_open(location, at("2026-10-30 18:00")) is True
    assert front_desk_open(location, at("2026-10-31 13:00")) is True
    assert front_desk_open(location, at("2026-10-31 13:25")) is False


def test_daylight_saving_ends_1_november_2026():
    # PDT (UTC-7) before, PST (UTC-8) after: 18:45 Monday 2 Nov is 02:45 UTC on 3 Nov.
    assert front_desk_open("Laguna Niguel", datetime.fromisoformat("2026-11-03T02:45:00+00:00")) is True
    assert front_desk_open("Laguna Niguel", datetime.fromisoformat("2026-11-03T02:55:00+00:00")) is False
    # 07:00 PST = 15:00 UTC; 14:30 UTC is still 06:30 there.
    assert front_desk_open("Laguna Niguel", datetime.fromisoformat("2026-11-02T14:30:00+00:00")) is False
    assert front_desk_open("Laguna Niguel", datetime.fromisoformat("2026-11-02T15:00:00+00:00")) is True
    # Saturday 31 Oct, still PDT: 13:15 local is 20:15 UTC.
    assert front_desk_open("Dana Point", datetime.fromisoformat("2026-10-31T20:15:00+00:00")) is True


def test_clinic_holidays_close_it_and_an_early_close_shortens_the_day():
    holidays = ["2026-11-26", {"date": "2026-12-24", "close": "12:00"},
                {"date": "2026-10-31", "close": "15:00"}]
    assert front_desk_open("Laguna Niguel", at("2026-11-26 10:00"), holidays) is False
    assert front_desk_open("Laguna Niguel", at("2026-12-24 11:49"), holidays) is True
    assert front_desk_open("Laguna Niguel", at("2026-12-24 11:50"), holidays) is False
    # A holiday close later than the desk's own (Saturday 13:30) never lengthens the day.
    assert front_desk_open("Laguna Niguel", at("2026-10-31 14:00"), holidays) is False


def test_the_call_variable_is_yes_or_no():
    assert front_desk_open_word("Laguna Niguel", at("2026-10-30 10:00")) == "yes"
    assert front_desk_open_word("Laguna Niguel", at("2026-11-01 10:00")) == "no"
