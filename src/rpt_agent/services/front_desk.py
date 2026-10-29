"""Is the clinic's front desk open right now? For Elliot's live transfer offer (owner, 2026-10-29).

Elliot offers to connect the patient to the front desk (a different case, a booking he could not
make, ...) only while someone is there to answer: the worker sends `front_desk_open` ("yes"/"no")
with every call. These are the front desk's hours per clinic, not the calling hours in
practice_settings.business_hours; clinic holidays (practice_settings.holidays) close it too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .lead_status import _window_for

CLINIC_TZ = ZoneInfo("America/Los_Angeles")
# Weekday (ISO, 1 = Monday) -> open / close, in the same shape as practice_settings.business_hours
# so lead_status._window_for applies the holidays exactly as for calls. Sunday is never listed.
_LONG_HOURS = {
    **{str(day): {"open": "07:00", "close": "19:00"} for day in range(1, 6)},
    "6": {"open": "07:00", "close": "13:30"},
}
FRONT_DESK_HOURS = {
    "laguna niguel": _LONG_HOURS,
    "dana point": _LONG_HOURS,
    "mission viejo": {str(day): {"open": "07:00", "close": "17:00"} for day in range(1, 6)},
}
# An unknown or empty location gets Laguna Niguel's hours (owner, 2026-10-29).
DEFAULT_LOCATION = "laguna niguel"
# "No" in the last minutes before closing: a transfer then may ring an empty desk.
CLOSING_MARGIN = timedelta(minutes=10)


def _clinic_key(location: str | None) -> str:
    words = " ".join("".join(ch if ch.isalpha() else " " for ch in str(location or "").lower()).split())
    return next((key for key in FRONT_DESK_HOURS if key in words), DEFAULT_LOCATION)


def front_desk_open(location: str | None, now: datetime | None = None, holidays: list | None = None) -> bool:
    """Whether the front desk of the lead's clinic is open at `now` (clinic time), with the margin."""
    local = (now or datetime.now(UTC)).astimezone(CLINIC_TZ)
    hours = FRONT_DESK_HOURS[_clinic_key(location)]
    regular = _window_for(hours, [], local.date())
    window = _window_for(hours, holidays or [], local.date())
    if not regular or not window:
        return False
    # A holiday's early close shortens the day, never lengthens it (13:30 on a Saturday stays 13:30).
    open_t, close_t = regular[0], min(regular[1], window[1])
    closing = datetime.combine(local.date(), close_t, tzinfo=CLINIC_TZ) - CLOSING_MARGIN
    return open_t <= local.time() and local < closing


def front_desk_open_word(location: str | None, now: datetime | None = None,
                         holidays: list | None = None) -> str:
    """The call variable's value: "yes" or "no"."""
    return "yes" if front_desk_open(location, now, holidays) else "no"
