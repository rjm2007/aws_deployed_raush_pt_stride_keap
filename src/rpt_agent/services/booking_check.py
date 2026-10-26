"""A second look at a booking the agent could not confirm (appointments.state='unknown').

Owner, 2026-10-22: when Stride does not answer clearly during book_slot (a timeout), the patient is
told our front desk team will take care of the appointment (before 2026-10-26: "our team will confirm by
text") and the lead goes to review. Instead of waiting for staff:

1. Our imported Stride data first (stride_appointments, from the CSV files): the patient (by the saved
   Stride patient or case id, or by name + date of birth) has a visit at that start time with that
   clinician -> the booking went through. The appointment becomes scheduled with that Stride id, the
   lead is booked the normal way (status, cadence completed, confirmation text unless texts are off)
   and the review closes as "confirmed in Stride".
2. Otherwise, ask Stride read-only (the availability endpoint, the only Stride read the public API
   has; the API cannot list a patient's appointments). That time still open for that clinician ->
   the visit was not made. The appointment is marked failed, the slot is offerable again, and the
   lead stays in review as "booking did not go through" (front desk only).
   That time gone -> probably ours, not proven: wait for Stride's next file (step 1).
   Stride failing -> try again later.
3. Stride is asked about one minute after the unclear result, then with backoff, for about 30
   minutes. After that only step 1 runs (Stride's files), and the front desk settles the rest.

Runs in the booking-worker (it already talks to Stride), so calls and texts are never delayed.
Never writes to Stride. Safe to run twice: every change re-locks the appointment and requires it to
still be 'unknown'.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from ..db import transaction
from ..observability import WorkflowTrace
from .lead_review import close_open_reviews
from .lead_status import mark_booked, record_status
from .sheet_sync import enqueue_sheet_update
from .stride_import import CANCELLED_STATUSES, NO_SHOW_STATUSES

# Minutes after the unclear result at which Stride is asked (attempt n is due at STRIDE_ASK[n]).
STRIDE_ASK_MINUTES = (1, 3, 7, 15, 29)
STRIDE_ASK_WINDOW = timedelta(minutes=30)
# How long Stride's files are still compared with an unclear booking (cheap: our own tables).
FILE_MATCH_WINDOW = timedelta(days=14)
CONFIRMED = "confirmed_in_stride"
NOT_MADE_CODE = "booking_failed"
NOT_MADE_MESSAGE = "Booking did not go through in Stride"
_MISSED = sorted(CANCELLED_STATUSES | NO_SHOW_STATUSES)
_BOOKING_CODES = ("booking_unclear", "booking_not_recorded", NOT_MADE_CODE)

_CANDIDATES_SQL = (
    "select a.id,a.lead_id,a.practice_id,a.start_utc,a.clinician_id,a.location_id,a.slot_id,"
    "a.slot_hold_id,a.stride_case_id,a.updated_at as unclear_at,s.local_date,s.local_time,"
    "s.duration_minutes,l.stride_patient_id,l.is_test,r.id as review_id,"
    "coalesce((r.details->'stride_check'->>'attempts')::int,0) as attempts "
    "from appointments a join leads l on l.id=a.lead_id "
    "left join availability_slots s on s.id=a.slot_id "
    "left join lateral (select id,details from lead_reviews where lead_id=a.lead_id "
    " and resolved_at is null and code=any(%s) order by id desc limit 1) r on true "
    "where a.state='unknown' and a.slot_id is not null and a.start_utc is not null "
    "and a.updated_at<=%s and a.updated_at>%s and (%s::bigint is null or a.practice_id=%s) "
    "order by a.updated_at limit 50"
)

# The visit in Stride's files that is this booking: same start, same clinician, and the patient is
# the one we booked (saved patient or case id, else name + date of birth).
_FILE_MATCH_SQL = (
    "select sa.stride_id from appointments a join leads l on l.id=a.lead_id "
    "join stride_appointments sa on sa.practice_id=a.practice_id and sa.deleted_at is null "
    " and sa.start_utc=a.start_utc and (a.clinician_id is null or sa.clinician_id=a.clinician_id) "
    " and lower(coalesce(sa.status,'')) <> all(%s) "
    "left join stride_patients p on p.practice_id=sa.practice_id and p.stride_id=sa.patient_stride_id "
    " and p.deleted_at is null "
    "where a.id=%s and ("
    " (coalesce(a.stride_patient_id,l.stride_patient_id) is not null"
    "  and sa.patient_stride_id=coalesce(a.stride_patient_id,l.stride_patient_id))"
    " or (a.stride_case_id is not null and sa.case_stride_id=a.stride_case_id)"
    " or (p.stride_id is not null and l.date_of_birth is not null and p.date_of_birth=l.date_of_birth"
    "  and lower(trim(coalesce(p.first_name,'')))=lower(trim(coalesce(l.first_name,'')))"
    "  and lower(trim(coalesce(p.last_name,'')))=lower(trim(coalesce(l.last_name,''))))"
    ") order by sa.stride_modified_at desc nulls last limit 1"
)


def _stride_due(row: dict, now: datetime) -> bool:
    attempts = int(row["attempts"] or 0)
    if row["review_id"] is None or attempts >= len(STRIDE_ASK_MINUTES):
        return False  # no review to count on (staff settled it), or Stride was asked enough
    if now - row["unclear_at"] > STRIDE_ASK_WINDOW:
        return False
    return now >= row["unclear_at"] + timedelta(minutes=STRIDE_ASK_MINUTES[attempts])


def run_booking_checks(
    trace: WorkflowTrace, providers: Any, now: datetime | None = None,
    practice_id: int | None = None,
) -> dict[str, int]:
    """One pass over unclear bookings. Returns counts (confirmed, not_made, waiting, errors)."""
    now = now or datetime.now(UTC)
    counts = {"checked": 0, "confirmed": 0, "not_made": 0, "waiting": 0, "errors": 0}
    with transaction() as conn:
        rows = conn.execute(
            _CANDIDATES_SQL,
            (list(_BOOKING_CODES), now - timedelta(minutes=STRIDE_ASK_MINUTES[0]),
             now - FILE_MATCH_WINDOW, practice_id, practice_id),
        ).fetchall()
    for row in rows:
        counts["checked"] += 1
        with transaction() as conn:
            found = conn.execute(_FILE_MATCH_SQL, (_MISSED, row["id"])).fetchone()
            if found:
                if confirm_booking(conn, row["id"], found["stride_id"]):
                    counts["confirmed"] += 1
                    trace.log("booking_check_confirmed", appointment_id=row["id"], via="stride_file")
                continue
        if not _stride_due(row, now) or row["local_date"] is None:
            counts["waiting"] += 1
            continue
        try:
            live = providers.stride_availability(
                trace, location=row["location_id"], duration=row["duration_minutes"],
                clinician_ids=str(row["clinician_id"]), start_date=row["local_date"],
                end_date=row["local_date"],
            )
        except Exception as exc:  # noqa: BLE001 - Stride not answering is "ask again later"
            _count_attempt(row, now, f"stride error: {type(exc).__name__}")
            counts["errors"] += 1
            trace.log("booking_check_stride_error", appointment_id=row["id"],
                      error_category=type(exc).__name__)
            continue
        wanted = row["local_time"].strftime("%H:%M:%S")
        still_open = any(item.local_date == row["local_date"].isoformat() and item.local_time == wanted
                         for item in live)
        if still_open:
            with transaction() as conn:
                if mark_not_made(conn, row["id"], now):
                    counts["not_made"] += 1
                    trace.log("booking_check_not_made", appointment_id=row["id"])
        else:
            # Gone from Stride's openings: most likely our visit, but the API cannot prove it.
            _count_attempt(row, now, "time no longer open in Stride (waiting for Stride's file)")
            counts["waiting"] += 1
    return counts


def _count_attempt(row: dict, now: datetime, outcome: str) -> None:
    with transaction() as conn:
        conn.execute(
            "update lead_reviews set details=details||jsonb_build_object('stride_check',"
            "jsonb_build_object('attempts',coalesce((details->'stride_check'->>'attempts')::int,0)+1,"
            "'last_at',%s::text,'outcome',%s::text)),updated_at=now() where id=%s and resolved_at is null",
            (now.isoformat(), outcome, row["review_id"]),
        )


def _locked_unknown(conn, appointment_id: int) -> dict | None:
    return conn.execute(
        "select a.*,l.status as lead_status,l.needs_review,l.is_test,l.first_name,l.last_name,"
        "l.email,l.phone_e164,l.date_of_birth,ct.stride_appointment_type_id "
        "from appointments a join leads l on l.id=a.lead_id "
        "left join case_types ct on ct.id=a.case_type_id "
        "where a.id=%s and a.state='unknown' for update of a,l",
        (appointment_id,),
    ).fetchone()


def confirm_booking(conn, appointment_id: int, stride_appointment_id: int) -> bool:
    """The unclear booking is in Stride: finish it as book_slot would have. False if already done."""
    appt = _locked_unknown(conn, appointment_id)
    if not appt:
        return False
    lead_id = str(appt["lead_id"])
    conn.execute(
        "update appointments set state='scheduled',stride_appointment_id=%s,confirmed_at=now(),"
        "stride_patient_id=coalesce(stride_patient_id,(select stride_patient_id from leads where id=lead_id)),"
        "needs_staff_review=false,stride_error=null,updated_at=now() where id=%s",
        (stride_appointment_id, appointment_id),
    )
    if appt["slot_hold_id"]:
        conn.execute("update slot_holds set status='booked' where id=%s", (appt["slot_hold_id"],))
    if appt["slot_id"]:
        conn.execute("update availability_slots set status='booked',updated_at=now() where id=%s",
                     (appt["slot_id"],))
    # The review closes with what happened, before mark_booked clears the flag.
    close_open_reviews(conn, lead_id, CONFIRMED, by="system", by_name="Stride check")
    mark_booked(conn, lead_id, "stride_check")
    record_status(conn, lead_id, "booked", "booked", "stride_check",
                  f"Booking confirmed in Stride (appointment {stride_appointment_id})")
    # The usual confirmation text for a booking. delivery skips it for calls only / STOP.
    conn.execute(
        "insert into notification_log(lead_id,appointment_id,notification_type,channel,status,payload) "
        "values(%s,%s,'sms_appointment_booked','sms','queued',%s) on conflict do nothing",
        (lead_id, appointment_id, Jsonb({"start_utc": appt["start_utc"].astimezone(UTC).isoformat()})),
    )
    enqueue_sheet_update(conn, lead_id=lead_id, event_type="lead_booked",
                         source_key=f"appointment:{appointment_id}")
    if not appt["is_test"]:  # synthetic leads never reach the client's CRM
        handoff_id = str(uuid4())
        conn.execute(
            "insert into integration_outbox(event_id,event_type,aggregate_id,payload,status,destination) "
            "values(%s,'appointment.booked.v1',%s,%s,'pending','keap') on conflict(event_id) do nothing",
            (handoff_id, str(appointment_id), Jsonb({
                "event_type": "appointment.booked.v1",
                "event_id": handoff_id,
                "lead_id": lead_id,
                "first_name": appt["first_name"],
                "last_name": appt["last_name"],
                "email": appt["email"],
                "phone": appt["phone_e164"],
                "birthday": appt["date_of_birth"].isoformat() if appt["date_of_birth"] else None,
                "appointment_type_id": appt["stride_appointment_type_id"],
                "appointment_start_utc": appt["start_utc"].astimezone(UTC).isoformat(),
                "provider_id": appt["clinician_id"],
                "stride_appointment_id": stride_appointment_id,
            })),
        )
    return True


def mark_not_made(conn, appointment_id: int, now: datetime) -> bool:
    """Stride still offers that time: the visit was not made. False if already settled."""
    appt = _locked_unknown(conn, appointment_id)
    if not appt:
        return False
    lead_id = str(appt["lead_id"])
    conn.execute(
        "update appointments set state='failed',needs_staff_review=false,stride_error=%s,"
        "updated_at=now() where id=%s",
        (f"not in Stride: that time was still open when checked at {now.isoformat()}", appointment_id),
    )
    if appt["slot_hold_id"]:
        conn.execute("update slot_holds set status='released',released_at=coalesce(released_at,now()) "
                     "where id=%s and status='active'", (appt["slot_hold_id"],))
    if appt["slot_id"]:
        # Stride lists it as open, so it is offerable again.
        conn.execute("update availability_slots set status='open',last_seen_at=now(),updated_at=now() "
                     "where id=%s and status<>'booked'", (appt["slot_id"],))
    if appt["needs_review"]:
        details = Jsonb({"stride_check": {"outcome": "not made: the time is still open in Stride",
                                          "last_at": now.isoformat()}})
        review = conn.execute(
            "update lead_reviews set code=%s,message=%s,details=details||%s,updated_at=now() "
            "where id=(select id from lead_reviews where lead_id=%s and resolved_at is null "
            "and code=any(%s) order by id desc limit 1) returning id",
            (NOT_MADE_CODE, NOT_MADE_MESSAGE, details, lead_id, list(_BOOKING_CODES)),
        ).fetchone()
        if review:
            conn.execute("update leads set review_reason=%s where id=%s", (NOT_MADE_MESSAGE, lead_id))
            record_status(conn, lead_id, appt["lead_status"], appt["lead_status"], "stride_check",
                          "Stride check: the booking did not go through (front desk books by hand)")
            enqueue_sheet_update(conn, lead_id=lead_id, event_type="review_flagged",
                                 source_key=f"{review['id']}:{NOT_MADE_CODE}")
    return True
