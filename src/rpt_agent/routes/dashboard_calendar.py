"""Staff calendar over booking records and the Stride import mirror."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..db import transaction
from ..observability import WorkflowTrace
from ..services import appointment_views, slot_booking
from .dashboard import Actor, _audit

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
MAX_RECORDS = 2000
CLINIC_TIMEZONE = "America/Los_Angeles"  # the dashboard's fallback (frontend display.ts)


def calendar_window(start: date, end: date, timezone: str):
    if end < start or (end - start).days > 62:
        raise HTTPException(400, "Choose a calendar range of at most 63 days.")
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(start, time.min, zone).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def clinic_timezone(conn, practice_id) -> str:
    """The zone the calendar shows: the clinic's own (practices.timezone), like the lead page,
    the home page and the rest of the dashboard. Not stride_location_timezone: the demo's Stride
    location is US/Eastern, so the calendar showed a 2:00 PM visit at 5:00 PM (2026-10-28).
    Stride's times are still read in the Stride zone by the import and the booking engine; only
    how the calendar displays and buckets the same instants changes."""
    row = conn.execute("select timezone from practices where id=%s", (practice_id,)).fetchone()
    return (row or {}).get("timezone") or CLINIC_TIMEZONE


def calendar_record(row, *, imported=False):
    return appointment_views.record(row, imported=imported)


@router.get("/appointment-calendar")
def appointment_calendar(actor: Actor, start: date, end: date, lead_id: UUID | None = None):
    with transaction() as conn:
        settings = conn.execute(
            "select ps.stride_booking_enabled,ps.business_hours from practice_settings ps "
            "where ps.practice_id=%s", (actor.practice_id,),
        ).fetchone() or {}
        timezone = clinic_timezone(conn, actor.practice_id)
        lower, upper = calendar_window(start, end, timezone)
        tables = appointment_views.optional_tables(conn)
        records, truncated = appointment_views.load_records(
            conn, actor.practice_id, lower=lower, upper=upper,
            lead_ids=[lead_id] if lead_id else None, limit=MAX_RECORDS,
        )
        practitioners = []
        if tables["booking"]:
            # Role (credentials) for the column headers: ours, else Stride's users export.
            role = ("coalesce(nullif(trim(c.credentials),''),(select nullif(trim(su.credentials),'') "
                    "from stride_users su where su.practice_id=c.practice_id and su.stride_id=c.stride_user_id "
                    "and su.deleted_at is null))" if tables["imports"] else "nullif(trim(c.credentials),'')")
            practitioners = conn.execute(
                "select distinct c.stride_user_id as id,c.display_name as name,bl.name as location,"
                "bl.stride_location_id," + role + " as role "
                "from clinicians c join clinician_assignments ca on ca.clinician_id=c.id "
                "join booking_locations bl on bl.id=ca.booking_location_id "
                "where c.practice_id=%s and bl.practice_id=%s "
                "and c.is_active and ca.is_active and bl.is_active order by c.display_name,bl.name",
                (actor.practice_id, actor.practice_id),
            ).fetchall()
        resources = {(str(p["id"]), p["location"]): {
            "id": str(p["id"]), "name": p["name"], "location": p["location"], "role": p["role"],
        } for p in practitioners}
        for r in records:
            resources.setdefault((r["practitioner_id"], r["location"]), {
                "id": r["practitioner_id"], "name": r["practitioner_name"],
                "location": r["location"], "role": r["clinician"]["role"],
            })
        # Open slots from Stride's availability cache (whole clinic, never lead-filtered).
        slots = appointment_views.open_slots(conn, actor.practice_id, lower, upper, timezone,
                                             datetime.now(UTC))
        last_import = None
        if tables["imports"]:
            last_import = conn.execute(
                "select max(processed_at) as at from stride_import_files "
                "where practice_id=%s and status='processed'", (actor.practice_id,),
            ).fetchone()["at"]
    return {
        "appointments": records, "practitioners": list(resources.values()),
        "timezone": timezone, "business_hours": settings.get("business_hours") or {},
        "booking_enabled": bool(settings.get("stride_booking_enabled") and tables["booking"]),
        "stride_import_enabled": tables["imports"], "last_import_at": last_import,
        "truncated": truncated, "generated_at": datetime.now(UTC), "open_slots": slots,
    }


@router.get("/appointment-approvals")
def appointment_approvals(actor: Actor):
    """Visits we booked that Stride's export has not shown yet: waiting for the front desk
    to approve them in Stride (Elliot books as pending). Upcoming first, then past ones."""
    with transaction() as conn:
        records, truncated = appointment_views.waiting_records(conn, actor.practice_id, datetime.now(UTC))
        last_import = None
        if appointment_views.optional_tables(conn)["imports"]:
            last_import = conn.execute(
                "select max(processed_at) as at from stride_import_files "
                "where practice_id=%s and status='processed'", (actor.practice_id,),
            ).fetchone()["at"]
    return {"appointments": records, "count": len(records), "truncated": truncated,
            "last_import_at": last_import, "generated_at": datetime.now(UTC)}


def _staff_lead(conn, actor, lead_id):
    lead = conn.execute(
        "select id,first_name,last_name,date_of_birth,email,status,call_opt_out,sms_opt_out,"
        "needs_review,phone_e164 from leads where id=%s and practice_id=%s",
        (lead_id, actor.practice_id),
    ).fetchone()
    if not lead:
        raise HTTPException(404, "Lead not found.")
    return lead


@router.get("/appointment-slots")
def appointment_slots(
    actor: Actor, lead_id: UUID, start: date, days: int = Query(7, ge=1, le=14),
    practitioner_id: int | None = Query(None, ge=1),
):
    with transaction() as conn:
        _staff_lead(conn, actor, lead_id)
        try:
            context = slot_booking.load_context(conn, str(lead_id))
        except (ValueError, slot_booking.BookingToolError):
            raise HTTPException(409, "This lead's location and case need booking configuration.") from None
        if not context.booking_enabled:
            raise HTTPException(409, "New bookings are currently switched off for this practice.")
        clinicians = [c["id"] for c in context.clinicians
                      if practitioner_id is None or c["stride_user_id"] == practitioner_id]
        if not clinicians:
            raise HTTPException(409, "This practitioner is not assigned to the lead's location and case.")
        slots = slot_booking.search_slots(
            conn, context, start=start, end=start + timedelta(days=days - 1),
            clinician_ids=clinicians, limit=60,
        )
    return {
        "slots": slots, "location": context.location["name"],
        "case_title": context.case_type["name"], "timezone": context.timezone,
        "duration_minutes": context.case_type["duration_minutes"],
        "cache_fresh": slot_booking.cache_is_fresh(context),
    }


class CalendarBooking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: UUID
    slot_id: int = Field(ge=1)


@router.post("/appointments", status_code=201)
def create_calendar_appointment(payload: CalendarBooking, actor: Actor):
    with transaction() as conn:
        lead = _staff_lead(conn, actor, payload.lead_id)
        # Opted out / Do Not Contact / blocked number do not stop a staff booking: the patient asked
        # for it (e.g. phoned the clinic). Booking contacts nobody, the opt-outs and the number block
        # stay, and the confirmation text is skipped by the notifier for them (user decision 2026-10-14).
        if lead["needs_review"]:
            raise HTTPException(409, "Resolve the lead's review before booking.")
        if lead["status"] == "booked":
            # The booking engine refuses too (it also sees Stride visits); answer early and plainly.
            raise HTTPException(409, _STAFF_MESSAGES["ALREADY_BOOKED"])
        if not lead["first_name"] or not lead["last_name"] or not lead["date_of_birth"]:
            raise HTTPException(400, "The lead needs a first name, last name and date of birth to book.")
        slot = conn.execute(
            "select s.local_date,s.local_time,s.start_utc,c.display_name from availability_slots s "
            "join clinicians c on c.id=s.clinician_id where s.id=%s and s.practice_id=%s "
            "and s.status='open'", (payload.slot_id, actor.practice_id),
        ).fetchone()
        if not slot:
            raise HTTPException(409, "That appointment time is no longer available.")
        timezone = clinic_timezone(conn, actor.practice_id)
        _audit(conn, actor, actor.practice_id, "appointment.booking_requested", "lead",
               str(payload.lead_id), {"slot_id": payload.slot_id})
    try:
        result = slot_booking.book_slot(
            WorkflowTrace("dashboard_booking", "api"), lead_id=str(payload.lead_id), call_id=None,
            source="staff",
            arguments={
                "date": slot["local_date"].isoformat(), "time": slot["local_time"].isoformat(),
                "clinician_name": slot["display_name"], "first_name": lead["first_name"],
                "last_name": lead["last_name"], "date_of_birth": lead["date_of_birth"].isoformat(),
                "email": lead["email"], "slot_id": payload.slot_id,
            },
        )
    except ValueError as exc:
        # Validation of the lead's own details (name, birthday, email): tell staff what to fix.
        _booking_outcome(actor, payload, "rejected", str(exc).split(":", 1)[0])
        raise HTTPException(400, _staff_message(str(exc))) from None
    except slot_booking.BookingToolError:
        _booking_outcome(actor, payload, "rejected", "BOOKING_UNAVAILABLE")
        raise HTTPException(409, "New bookings are currently switched off for this practice.") from None
    code = result.split(":", 1)[0]
    _booking_outcome(actor, payload, "booked" if code == "BOOKED" else "refused", code)
    if code != "BOOKED":
        raise HTTPException(409, _staff_message(result))
    with transaction() as conn:
        saved = conn.execute(
            "select id,state,start_utc,stride_appointment_id from appointments "
            "where lead_id=%s and practice_id=%s "
            "and state in ('scheduled','booking','unknown') order by id desc limit 1",
            (payload.lead_id, actor.practice_id),
        ).fetchone()
    return {
        "status": "scheduled" if saved and saved["state"] == "scheduled" else "confirmed_sync_pending",
        "id": calendar_record(saved)["id"] if saved else None,
        "lead_id": payload.lead_id,
        # The calendar day to jump to, in the calendar's (clinic) zone.
        "start_day": ((saved["start_utc"] if saved and saved["start_utc"] else slot["start_utc"])
                      .astimezone(ZoneInfo(timezone)).date()),
    }


# What staff see when a booking does not go through. The booking engine's replies are written for
# the voice agent ("tell the caller ..."), so the dashboard needs its own words.
_STAFF_MESSAGES = {
    "ALREADY_BOOKED": "This patient is already booked, so no new appointment was made.",
    "IN_PROGRESS": "A booking for this patient is already being made. Wait a minute and refresh.",
    "UNCONFIRMED": "Stride did not answer clearly. The lead is flagged; check Stride before booking again.",
    "PATIENT_EXISTS": "This patient already exists in Stride, so book them in Stride directly. "
                      "The lead is flagged for review.",
    "TAKEN": "That time was just taken in Stride. Pick another time.",
    "NOT_AVAILABLE": "That time is no longer open. Pick another time.",
    "FOLLOW_UP": "The booking could not be finished in time. Check Stride before trying again.",
    "BAD_SLOT": "That appointment time is not valid. Refresh and pick a time again.",
    "MISSING_NAME": "The lead needs a first and last name to book.",
    "BAD_NAME": "The lead's name is too long for Stride. Shorten it on the lead first.",
    "BAD_DATE_OF_BIRTH": "The lead's date of birth is not valid. Fix it on the lead first.",
    "BAD_EMAIL": "The lead's email address is not valid. Fix or remove it on the lead first.",
}


def _staff_message(result: str) -> str:
    code = result.split(":", 1)[0].strip()
    return _STAFF_MESSAGES.get(code, "That time could not be booked. Check availability or ask staff to review.")


def _booking_outcome(actor, payload: "CalendarBooking", outcome: str, code: str) -> None:
    """Audit what happened, not only that a booking was asked for (no patient data)."""
    with transaction() as conn:
        _audit(conn, actor, actor.practice_id, f"appointment.booking_{outcome}", "lead",
               str(payload.lead_id), {"slot_id": payload.slot_id, "result": code})

