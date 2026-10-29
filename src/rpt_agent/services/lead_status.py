from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from ..db import transaction
from ..observability import WorkflowTrace
from .review import flag_lead_for_review, record_review, skip_remaining_planned
from .sheet_sync import enqueue_sheet_update

CALLBACK_MIN_MINUTES = 5
# Raised when a "booked" outcome has no scheduled appointment; delivery.py matches it to flag
# `booking_hung_up` instead of a generic "could not be applied" card.
BOOKED_WITHOUT_APPOINTMENT = "booked requires a confirmed appointment"
# Raised when a callback has no time at all; delivery.py matches it to flag `callback_no_time`.
CALLBACK_WITHOUT_TIME = "callback_scheduled requires a callback time"
# The patient said they already booked or are already a patient (owner, 2026-10-28: close the lead).
# A closed lead like `declined`, told apart by this status_reason (dashboard and Sheet name it).
ALREADY_BOOKED_REASON = "Already booked or already a patient"
CALLBACK_MAX_MINUTES = 240
# The patient asked about a different case (body part) than the referral (owner, 2026-10-29: demo
# call, referral for ankle, "I need an appointment for my spine", and Elliot booked the ankle).
# Elliot never books it: he offers the front desk and reports `case_change` with notes = what the
# patient asked for. The lead goes to Needs Review with this code (`case_changed` is the Sheet's
# Case column edit, a different thing). A booked lead keeps its booking; the card only asks the
# front desk to handle the other case.
CASE_CHANGE_STATUS = "case_change"
CASE_CHANGE_CODE = "case_change_asked"
# Every status the lead-status tool may send (config/vapi_booking_tools.json lists the same, tested).
REPORTED_STATUSES = (
    "booked", "declined", "callback_scheduled", "booking_link", "booking_link_and_transfer",
    "transferred_human", "no_answer", "wrong_person", "call_opt_out", "do_not_contact",
    "already_booked", CASE_CHANGE_STATUS,
)


def _practice_clock(conn, lead_id: str) -> tuple[ZoneInfo, dict, list]:
    row = conn.execute(
        "select coalesce(l.timezone,p.timezone) as tz,ps.business_hours,ps.holidays "
        "from leads l join practices p on p.id=l.practice_id "
        "join practice_settings ps on ps.practice_id=l.practice_id where l.id=%s",
        (lead_id,),
    ).fetchone()
    if not row:
        raise ValueError("lead not found")
    return ZoneInfo(row["tz"] or "America/Los_Angeles"), row["business_hours"] or {}, row["holidays"] or []


def resolve_callback_time(
    tz: ZoneInfo,
    *,
    callback_requested_at: datetime | None,
    callback_type: str | None,
    delay_minutes: str | int | None,
    callback_datetime_iso: str | None,
    now: datetime | None = None,
) -> datetime:
    """Turn the voice tool's callback fields into one absolute UTC moment.

    The model supplies a duration or a wall-clock string; the arithmetic and the
    timezone stay here so a mis-calculated offset cannot reach the database.
    """
    now = now or datetime.now(UTC)
    kind = (callback_type or "").strip().lower()
    if kind == "relative" or (not kind and delay_minutes not in (None, "")):
        try:
            minutes = int(float(str(delay_minutes).strip()))
        except (TypeError, ValueError) as exc:
            raise ValueError("delay_minutes must be a whole number of minutes") from exc
        minutes = max(CALLBACK_MIN_MINUTES, min(minutes, CALLBACK_MAX_MINUTES))
        return now + timedelta(minutes=minutes)
    raw = callback_datetime_iso if kind == "absolute" else (callback_datetime_iso or None)
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).strip())
        except ValueError as exc:
            raise ValueError("callback_datetime_iso must be an ISO 8601 timestamp") from exc
        # A bare wall-clock reading is the practice's local time, not UTC.
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(UTC)
    if callback_requested_at is not None:
        if callback_requested_at.tzinfo is None:
            return callback_requested_at.replace(tzinfo=tz).astimezone(UTC)
        return callback_requested_at.astimezone(UTC)
    raise ValueError(CALLBACK_WITHOUT_TIME)


def _window_for(hours: dict, holidays: list, day) -> tuple[dtime, dtime] | None:
    closed = set()
    overrides = {}
    for item in holidays or []:
        if isinstance(item, str):
            closed.add(item)
        elif isinstance(item, dict) and item.get("date"):
            if item.get("close"):
                overrides[item["date"]] = item["close"]
            else:
                closed.add(item["date"])
    key = day.isoformat()
    if key in closed:
        return None
    window = hours.get(str(day.isoweekday()))
    if not isinstance(window, dict):
        return None
    close_raw = overrides.get(key) or window["close"]
    return (
        dtime.fromisoformat(window["open"][:5]),
        dtime.fromisoformat(str(close_raw)[:5]),
    )


def clamp_to_business_hours(when_utc: datetime, tz: ZoneInfo, hours: dict, holidays: list) -> datetime:
    """Move a callback to the next moment the practice is actually open to call."""
    if not hours:
        return when_utc
    local = when_utc.astimezone(tz)
    for _ in range(21):
        window = _window_for(hours, holidays, local.date())
        if window:
            open_t, close_t = window
            if local.time() < open_t:
                return datetime.combine(local.date(), open_t, tzinfo=tz).astimezone(UTC)
            if local.time() < close_t:
                return when_utc
        local = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return when_utc

VALID_OUTCOMES = {
    "booked", "not_interested", "no_answer", "voicemail", "callback", "transferred", "manual",
    "booking_link",
    "call_opt_out", "do_not_contact",
}

def record_status(conn, lead_id: str, old: str | None, new: str, source: str, reason: str) -> None:
    conn.execute(
        "insert into lead_status_history (lead_id,from_status,to_status,source,reason) "
        "values (%s,%s,%s,%s,%s)",
        (lead_id, old, new, source, reason),
    )


def mark_booked(conn, lead_id: str, source: str) -> None:
    # FOR NO KEY UPDATE still makes status writers wait for each other, but not for rows being
    # inserted elsewhere that point at this lead (foreign keys take a key-share lock until they
    # commit). FOR UPDATE waited 58 s behind the keap-worker's hourly plan, which had just added
    # this lead's keap_sync row: past Vapi's tool timeout, so the agent told a booked patient the
    # system wasn't responding (demo 2026-10-26). Nothing here changes a key column of leads.
    lead = conn.execute(
        "select status from leads where id=%s for no key update", (lead_id,)
    ).fetchone()
    if not lead:
        raise ValueError("lead not found")
    conn.execute(
        "update outreach_events set status='skipped',updated_at=now() "
        "where lead_id=%s and status='planned'",
        (lead_id,),
    )
    # A confirmed booking settles any open review, as the Sheet's Booked does (migration 048's
    # trigger records it as "booked"): a booked lead left flagged showed in Needs Review, where
    # "Continue" would have put it back into calls. Except a different case the patient asked for
    # (owner, 2026-10-29): this booking is not that case, so its card stays open on the booked lead.
    case_change = conn.execute(
        "select message from lead_reviews where lead_id=%s and resolved_at is null and code=%s "
        "order by flagged_at,id limit 1",
        (lead_id, CASE_CHANGE_CODE),
    ).fetchone()
    if case_change:
        conn.execute(
            "update lead_reviews set resolved_at=now(),resolved_by='system',resolved_by_name='System',"
            "resolution='booked',updated_at=now() where lead_id=%s and resolved_at is null and code<>%s",
            (lead_id, CASE_CHANGE_CODE),
        )
        conn.execute(
            "update leads set status='booked',cadence_state='completed',last_call_outcome='booked',"
            "needs_review=true,review_reason=%s,review_flagged_at=coalesce(review_flagged_at,now()),"
            "status_changed_at=now() where id=%s",
            (case_change["message"], lead_id),
        )
    else:
        conn.execute(
            "update leads set status='booked',cadence_state='completed',last_call_outcome='booked',"
            "needs_review=false,review_reason=null,review_resolved_at=case when needs_review then now() "
            "else review_resolved_at end,status_changed_at=now() where id=%s",
            (lead_id,),
        )
    if lead["status"] != "booked":
        record_status(conn, lead_id, lead["status"], "booked", source, "appointment confirmed")


def case_change_message(asked: str | None, referral: str | None) -> str:
    """The review's words: "Patient asked about a different case: spine (referral is for ankle)"."""
    text = f"Patient asked about a different case: {(asked or '').strip() or 'not recorded'}"
    if (referral or "").strip():
        text += f" (referral is for {referral.strip()})"
    return text[:500]


def flag_case_change(
    conn, lead: dict, *, note: str, source: str, event_id: int | None, call_id: str | None,
) -> bool:
    """Needs Review for a different case (owner, 2026-10-29). Returns whether a card is open.

    Not booked: paused and flagged like any call review. Booked: the booking stays (booked is
    final, the lead stays in the Booked column); only the card opens, and its one choice (front
    desk) closes it without touching the booking.
    """
    lead_id = str(lead["id"])
    message = case_change_message(note, lead.get("lead_type"))
    # The call's event goes in details, not outreach_event_id: record_review refreshes the open row
    # of the same event, which would turn another card raised on this call into this one. The same
    # report twice (same words) refreshes this card instead of adding a second.
    details = {"asked": note or None, "referral_case": lead.get("lead_type") or None,
               "call_id": call_id, "outreach_event_id": event_id,
               "booked": lead["status"] == "booked" or None}
    if lead["status"] != "booked":
        flagged = flag_lead_for_review(conn, lead_id, message, code=CASE_CHANGE_CODE, details=details,
                                       source="call")
        if flagged:
            record_status(conn, lead_id, lead["status"], "needs_attention", source, message)
        return flagged
    conn.execute(
        "update leads set needs_review=true,review_reason=coalesce(case when needs_review "
        "then review_reason end,%s),review_flagged_at=coalesce(case when needs_review "
        "then review_flagged_at end,now()) where id=%s",
        (message, lead_id),
    )
    review_id = record_review(conn, lead_id, CASE_CHANGE_CODE, message, details=details, source="call")
    return review_id is not None


def _schedule_callback(conn, lead_id: str, callback_utc: datetime) -> None:
    """Put the callback first and preserve the remaining cadence's spacing."""
    # A lead has at most one pending callback. A second one is the patient
    # correcting the time (ten, then "no, nine"), so it replaces the first rather
    # than booking a call at both times. Only never-dispatched standalone
    # callbacks go; cadence steps and anything already sent are untouched.
    conn.execute(
        "delete from outreach_events where lead_id=%s and status='planned' "
        "and cadence_step_id is null and day_offset is null",
        (lead_id,),
    )
    row = conn.execute(
        "select min(scheduled_for) as earliest from outreach_events "
        "where lead_id=%s and status='planned'",
        (lead_id,),
    ).fetchone()
    earliest = row["earliest"] if row else None
    if earliest is not None and earliest <= callback_utc:
        conn.execute(
            "update outreach_events set scheduled_for=scheduled_for+%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            (callback_utc + timedelta(seconds=1) - earliest, lead_id),
        )
    conn.execute(
        "insert into outreach_events(lead_id,channel,scheduled_for,status) "
        "values(%s,'call',%s,'planned')",
        (lead_id, callback_utc),
    )


TRANSFER_WITH_REVIEW = "Transferred to the front desk; the open review stays"


def _transfer_with_review(note: str | None) -> str:
    return (f"{TRANSFER_WITH_REVIEW}. {note}" if note else TRANSFER_WITH_REVIEW)[:500]


def report_lead_status(
    trace: WorkflowTrace,
    *,
    lead_id: str,
    status: str,
    call_id: str | None,
    tool_call_id: str | None = None,
    event_id: int | None = None,
    notes: str | None = None,
    callback_requested_at: datetime | None = None,
    callback_type: str | None = None,
    delay_minutes: str | int | None = None,
    callback_datetime_iso: str | None = None,
    source: str = "tool",
) -> str:
    """Apply the direct Vapi lead-status contract with per-call/status idempotency.

    ``source`` labels the lead's history: "tool" when the agent reported during
    the call, "call summary" when the post-call summary settled it instead.
    """
    normalized = {
        "not_interested": "declined",
        "callback": "callback_scheduled",
        "transferred": "transferred_human",
        "manual": "wrong_person",
    }.get(status.strip().lower(), status.strip().lower())
    note = (notes or "").strip()[:500]
    trace.log("validation_started", lead_id=lead_id, event_id=event_id, reported_status=normalized)
    with transaction() as conn:
        lead = conn.execute(
            "select id,status,cadence_state,phone_e164,sms_opt_out,needs_review,lead_type "
            "from leads where id=%s for update",
            (lead_id,),
        ).fetchone()
        if not lead:
            raise ValueError(
                "I could not find that patient record. Please flag the call for staff follow-up."
            )
        if event_id is not None:
            event = conn.execute(
                "select id,lead_id,vapi_call_id,outcome from outreach_events where id=%s for update",
                (event_id,),
            ).fetchone()
        elif call_id:
            event = conn.execute(
                "select id,lead_id,vapi_call_id,outcome from outreach_events "
                "where vapi_call_id=%s for update",
                (call_id,),
            ).fetchone()
        else:
            event = conn.execute(
                "select id,lead_id,vapi_call_id,outcome from outreach_events where lead_id=%s "
                "and channel='call' and status in ('in_flight','attempted','delivered') "
                "order by executed_at desc nulls last,id desc limit 1 for update",
                (lead_id,),
            ).fetchone()
        if event and str(event["lead_id"]) != lead_id:
            raise ValueError("lead and outreach event do not match")
        stable_call_id = call_id or (str(event["vapi_call_id"]) if event and event["vapi_call_id"] else None)
        if not stable_call_id:
            raise ValueError("call_id or outreach_event_id is required")
        # The same tool call arriving twice is a duplicate; a new tool call is a
        # new decision, even one the patient already made earlier in the call.
        # Keying on call and status alone dropped every correction: link, then
        # "not interested", then link again lost the final link, and a callback
        # moved from ten to nine kept ten. Reports with no tool call id (the
        # post-call backup, direct requests) keep the per-call key.
        receipt_id = (
            f"tool:{tool_call_id}:{lead_id}:{normalized}"
            if tool_call_id
            else f"{stable_call_id}:{lead_id}:{normalized}"
        )
        receipt = conn.execute(
            "insert into provider_events(provider,event_id,event_type,payload) "
            "values('vapi',%s,'lead-status',%s) on conflict(provider,event_id) do nothing returning id",
            (receipt_id, json.dumps({"lead_id": lead_id, "status": normalized})),
        ).fetchone()
        if not receipt:
            trace.log("state_transition_skipped", reason="duplicate_lead_status")
            return "already recorded"
        if lead["status"] == "booked" and normalized == CASE_CHANGE_STATUS:
            # Booked, then "also my spine" (owner, 2026-10-29): the booking stays, the card opens.
            # The call is settled only if the booking has not settled it already.
            if event:
                conn.execute(
                    "update outreach_events set status='delivered',settled_at=now(),settled_by=%s,"
                    "outcome='manual',updated_at=now() where id=%s and status in ('in_flight','attempted')",
                    ("tool" if source == "tool" else "webhook", event["id"]),
                )
            flag_case_change(conn, lead, note=note, source=source,
                             event_id=event["id"] if event else None, call_id=stable_call_id)
            conn.execute("update provider_events set processed_at=now() where id=%s", (receipt["id"],))
            trace.log("state_transition_applied", reported_status=normalized, booked=True)
            return "recorded"
        if lead["status"] == "booked" and normalized != "booked":
            conn.execute("update provider_events set processed_at=now() where id=%s", (receipt["id"],))
            trace.log("state_transition_skipped", reason="confirmed_booking_is_terminal")
            return "already booked"

        # A transfer to the front desk while a review is open (a booking that failed on the call, a
        # different case, ...; owner, 2026-10-29) is only noted: the review and the pause stay, so the
        # transfer can never put the lead back into calls or hide the card.
        review_open = bool(lead.get("needs_review"))
        outcome = {
            "booked": "booked",
            "declined": "not_interested",
            "callback_scheduled": "callback",
            "transferred_human": "transferred",
            "no_answer": "no_answer",
            "call_opt_out": "call_opt_out",
            "do_not_contact": "do_not_contact",
            "booking_link_sent": "booking_link",
            "booking_link": "booking_link",
            "booking_link_and_transfer": "transferred",
            # No outcome of its own (the column's check list); the lead's status_reason says why.
            "already_booked": "not_interested",
        }.get(normalized, "manual")
        if event:
            conn.execute(
                "update outreach_events set status='delivered',settled_at=now(),settled_by=%s,"
                "outcome=%s,updated_at=now() where id=%s",
                # settled_by only allows worker/tool/webhook/sweeper; the post-call
                # summary arrives on the end-of-call webhook.
                ("tool" if source == "tool" else "webhook", outcome, event["id"]),
            )

        if normalized == "booked":
            appointment = conn.execute(
                "select id from appointments where lead_id=%s and state='scheduled' limit 1",
                (lead_id,),
            ).fetchone()
            if not appointment:
                raise ValueError(BOOKED_WITHOUT_APPOINTMENT)
            mark_booked(conn, lead_id, source)
        elif normalized == "declined":
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',"
                "last_call_outcome='not_interested',status_reason=%s,status_changed_at=now() where id=%s",
                (note or "patient declined scheduling", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "declined", source, note or "declined")
        elif normalized == "already_booked":
            history = f"{ALREADY_BOOKED_REASON}. {note}" if note else ALREADY_BOOKED_REASON
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',"
                "last_call_outcome='not_interested',status_reason=%s,status_changed_at=now() where id=%s",
                (ALREADY_BOOKED_REASON, lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "declined", source, history[:500])
        elif normalized == "callback_scheduled":
            tz, hours, holidays = _practice_clock(conn, lead_id)
            now = datetime.now(UTC)
            callback_utc = clamp_to_business_hours(
                resolve_callback_time(
                    tz,
                    callback_requested_at=callback_requested_at,
                    callback_type=callback_type,
                    delay_minutes=delay_minutes,
                    callback_datetime_iso=callback_datetime_iso,
                    now=now,
                ),
                tz,
                hours,
                holidays,
            )
            if callback_utc <= now or callback_utc > now + timedelta(days=30):
                raise ValueError("callback time must be in the next 30 days")
            conn.execute(
                "update leads set status='callback_scheduled',cadence_state='active',"
                "last_call_outcome='callback',callback_requested_at=%s,callback_notes=%s,"
                "callback_reschedule_count=callback_reschedule_count+1,"
                "status_changed_at=now() where id=%s",
                (callback_utc, note or None, lead_id),
            )
            _schedule_callback(conn, lead_id, callback_utc)
            record_status(
                conn, lead_id, lead["status"], "callback_scheduled", source, note or "callback requested"
            )
        elif normalized in {"booking_link", "booking_link_and_transfer"}:
            # "Text me the link AND connect me" (2026-10-16: the agent could only pick one and the
            # patient got no link). The text is marked with_transfer so the transfer status that
            # follows does not cancel it; a plain change of mind to a transfer still does.
            with_transfer = normalized == "booking_link_and_transfer"
            settings = conn.execute(
                "select booking_link_url from practice_settings ps join leads l "
                "on l.practice_id=ps.practice_id where l.id=%s",
                (lead_id,),
            ).fetchone()
            if not settings or not settings["booking_link_url"]:
                raise ValueError("booking link is not configured")
            suppressed = conn.execute(
                "select 1 from suppressed_numbers where phone_e164=%s",
                (lead["phone_e164"],),
            ).fetchone()
            if lead["sms_opt_out"] or suppressed or not lead["phone_e164"]:
                raise ValueError("the lead cannot receive an SMS booking link")
            # Held for two minutes so we act on the patient's last word on the
            # call: the sender drops the text if the lead has moved off
            # booking_link_sent by then. One link per call, so asking again after
            # a change of mind re-arms this call's text instead of adding another.
            existing = conn.execute(
                "select id,status from notification_log where lead_id=%s "
                "and notification_type='sms_booking_link' and payload->>'call_id'=%s "
                "order by id desc limit 1",
                (lead_id, stable_call_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "insert into notification_log(lead_id,notification_type,channel,status,payload,"
                    "next_attempt_at) values(%s,'sms_booking_link','sms','queued',%s,"
                    "now()+interval '2 minutes')",
                    (lead_id, json.dumps({
                        "booking_link_url": settings["booking_link_url"],
                        "call_id": stable_call_id,
                        **({"with_transfer": True} if with_transfer else {}),
                    })),
                )
            else:
                if existing["status"] == "skipped":
                    conn.execute(
                        "update notification_log set status='queued',error=null,"
                        "next_attempt_at=now()+interval '2 minutes',updated_at=now() where id=%s",
                        (existing["id"],),
                    )
                # The latest decision sets whether this call's text survives a transfer.
                conn.execute(
                    "update notification_log set payload=payload - 'with_transfer' || %s::jsonb,"
                    "updated_at=now() where id=%s",
                    (json.dumps({"with_transfer": True} if with_transfer else {}), existing["id"]),
                )
            if with_transfer and review_open:
                record_status(
                    conn, lead_id, lead["status"], lead["status"], source,
                    _transfer_with_review(note or "booking link texted and transferred to staff"),
                )
            elif with_transfer:
                conn.execute(
                    "update leads set status='transferred_human',cadence_state='active',"
                    "last_call_outcome='transferred',status_reason=%s,status_changed_at=now() where id=%s",
                    (note or "booking link texted and transferred to staff", lead_id),
                )
                record_status(
                    conn, lead_id, lead["status"], "transferred_human", source,
                    note or "booking link texted and transferred to staff",
                )
            else:
                # A booking link is an outcome, not proof of a booking. Keep the
                # remaining cadence active until staff explicitly marks the lead booked.
                conn.execute(
                    "update leads set status='booking_link_sent',cadence_state='active',"
                    "last_call_outcome='manual',status_reason=%s,status_changed_at=now() where id=%s",
                    (note or "booking link requested", lead_id),
                )
                record_status(
                    conn, lead_id, lead["status"], "booking_link_sent", source,
                    note or "booking link requested",
                )
        elif normalized == "transferred_human":
            # A plain transfer after "link and transfer" in this call is a change of mind: the
            # queued text loses its with_transfer mark, so the sender drops it.
            conn.execute(
                "update notification_log set payload=payload - 'with_transfer',updated_at=now() "
                "where lead_id=%s and notification_type='sms_booking_link' and status='queued' "
                "and payload->>'call_id'=%s",
                (lead_id, stable_call_id),
            )
            if review_open:
                record_status(conn, lead_id, lead["status"], lead["status"], source,
                              _transfer_with_review(note))
            else:
                # Record the handoff, but only an explicit staff booking closes cadence.
                conn.execute(
                    "update leads set status='transferred_human',cadence_state='active',"
                    "last_call_outcome='transferred',status_reason=%s,status_changed_at=now() where id=%s",
                    (note or "transferred to staff", lead_id),
                )
                record_status(
                    conn, lead_id, lead["status"], "transferred_human", source, note or "transferred"
                )
        elif normalized == CASE_CHANGE_STATUS:
            flag_case_change(conn, lead, note=note, source=source,
                             event_id=event["id"] if event else None, call_id=stable_call_id)
        elif normalized == "no_answer":
            conn.execute(
                "update leads set last_call_outcome='no_answer',status_reason=%s where id=%s",
                (note or None, lead_id),
            )
        elif normalized == "wrong_person":
            reason = note or "wrong phone number"
            conn.execute(
                "update leads set status='invalid_phone',cadence_state='paused',"
                "last_call_outcome='manual',needs_review=true,review_reason=%s,"
                "review_flagged_at=now(),status_changed_at=now() where id=%s",
                (reason, lead_id),
            )
            skip_remaining_planned(conn, lead_id)
            record_status(conn, lead_id, lead["status"], "invalid_phone", source, reason)
            record_review(
                conn, lead_id, "wrong_person", reason, source="call",
                outreach_event_id=event["id"] if event else None,
                details={"note": note or None, "call_id": stable_call_id},
            )
        elif normalized == "call_opt_out":
            conn.execute(
                "update leads set call_opt_out=true,last_call_outcome='call_opt_out',"
                "status_reason=%s,status_changed_at=now() where id=%s",
                (note or "patient opted out of calls", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() where lead_id=%s "
                "and channel='call' and status='planned'",
                (lead_id,),
            )
        elif normalized == "do_not_contact":
            conn.execute(
                # Do Not Contact settles the lead: any older "needs attention" note is resolved, so
                # the card moves to Closed instead of waiting for review (user decision 2026-10-14).
                "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
                "needs_review=false,review_reason=null,review_resolved_at=case when needs_review then now() else review_resolved_at end,"
                "cadence_state='terminated',last_call_outcome='do_not_contact',status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (note or "patient requested no further contact", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            if lead["phone_e164"]:
                conn.execute(
                    "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                    "values(%s,'explicit do-not-contact request','tool','internal') "
                    "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
                    "last_verified_at=now()",
                    (lead["phone_e164"],),
                )
            record_status(
                conn, lead_id, lead["status"], "do_not_contact", source, note or "do not contact"
            )
        else:
            reason = note or f"unrecognized status: {normalized}"
            conn.execute(
                "update leads set status='needs_attention',cadence_state='paused',"
                "last_call_outcome='manual',needs_review=true,review_reason=%s,"
                "review_flagged_at=now(),status_changed_at=now() where id=%s",
                (reason, lead_id),
            )
            skip_remaining_planned(conn, lead_id)
            record_status(conn, lead_id, lead["status"], "needs_attention", source, reason)
            record_review(
                conn, lead_id, "unrecognized_status", reason, source="call",
                outreach_event_id=event["id"] if event else None,
                details={"reported_status": normalized, "note": note or None, "call_id": stable_call_id},
            )
        conn.execute("update provider_events set processed_at=now() where id=%s", (receipt["id"],))
        enqueue_sheet_update(
            conn,
            lead_id=lead_id,
            event_type="call_settled",
            source_key=receipt_id,
            outreach_event_id=event["id"] if event else None,
        )
    trace.log("state_transition_applied", reported_status=normalized)
    return "recorded"


def apply_call_outcome(
    trace: WorkflowTrace,
    *,
    lead_id: str,
    event_id: int,
    outcome: str,
    source: str = "tool",
    callback_requested_at: datetime | None = None,
    callback_notes: str | None = None,
) -> str:
    trace.log("validation_started", lead_id=lead_id, event_id=event_id)
    if outcome not in VALID_OUTCOMES:
        trace.log("validation_failed", reason="invalid_outcome")
        raise ValueError("invalid call outcome")
    with transaction() as conn:
        trace.log("database_operation_started", operation="lock_lead_event")
        # Lead first, then the event: the order report_lead_status and the voice booking
        # (_finish_booking: mark_booked, then this call's event) use. Event-then-lead could deadlock
        # with a booking finishing as the end-of-call report arrives, and Postgres would abort one
        # side; losing the booking's save leaves a visit in Stride our records do not show.
        lead = conn.execute(
            "select id,status,phone_e164,needs_review from leads where id=%s for update", (lead_id,)
        ).fetchone()
        event = conn.execute(
            "select id,lead_id,channel,status,outcome,vapi_call_id from outreach_events "
            "where id=%s for update",
            (event_id,),
        ).fetchone()
        if not lead or not event or str(event["lead_id"]) != str(lead_id):
            trace.log("validation_failed", reason="lead_event_mismatch")
            raise ValueError("lead and outreach event do not match")
        if event["channel"] != "call":
            trace.log("validation_failed", reason="event_is_not_call")
            raise ValueError("outreach event is not a call")
        if event["status"] in {"delivered", "failed", "skipped"}:
            if event["status"] == "delivered" and event["outcome"] == outcome:
                trace.log("state_transition_skipped", current_status=event["status"])
                return "already recorded"
            trace.log("validation_failed", reason="conflicting_terminal_outcome")
            raise ValueError("outreach event is already settled with a different outcome")
        if event["status"] not in {"in_flight", "attempted"}:
            trace.log("validation_failed", reason="event_not_dispatched")
            raise ValueError("call outcome cannot be recorded before dispatch")
        if outcome == "booked":
            appointment = conn.execute(
                "select id from appointments where lead_id=%s and state='scheduled' limit 1",
                (lead_id,),
            ).fetchone()
            if not appointment:
                trace.log("validation_failed", reason="booked_without_appointment")
                raise ValueError(BOOKED_WITHOUT_APPOINTMENT)
        callback_utc = None
        if outcome == "callback":
            if callback_requested_at is None or callback_requested_at.tzinfo is None:
                trace.log("validation_failed", reason="callback_time_required")
                raise ValueError("callback requires a timezone-aware callback_requested_at")
            now = datetime.now(UTC)
            callback_utc = callback_requested_at.astimezone(UTC)
            if callback_utc <= now or callback_utc > now + timedelta(days=30):
                trace.log("validation_failed", reason="callback_time_out_of_range")
                raise ValueError("callback time must be in the next 30 days")
        conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by=%s,outcome=%s "
            "where id=%s and status not in ('delivered','failed','skipped')",
            (source, outcome, event_id),
        )
        if outcome == "booked":
            mark_booked(conn, lead_id, source)
        elif outcome == "not_interested":
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'", (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "declined", source, "not interested")
        elif outcome == "callback":
            conn.execute(
                "update leads set status='callback_scheduled',cadence_state='active',last_call_outcome=%s,"
                "callback_requested_at=%s,callback_notes=%s,status_changed_at=now() where id=%s",
                (outcome, callback_utc, (callback_notes or "")[:500] or None, lead_id),
            )
            _schedule_callback(conn, lead_id, callback_utc)
            record_status(
                conn, lead_id, lead["status"], "callback_scheduled", source, "callback requested"
            )
        elif outcome in {"no_answer", "voicemail"}:
            conn.execute("update leads set last_call_outcome=%s where id=%s", (outcome, lead_id))
        elif outcome == "transferred" and lead.get("needs_review"):
            # An open review stays open through a transfer (see report_lead_status).
            record_status(conn, lead_id, lead["status"], lead["status"], source,
                          _transfer_with_review(None))
        elif outcome == "transferred":
            conn.execute(
                "update leads set status='transferred_human',cadence_state='active',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            record_status(conn, lead_id, lead["status"], "transferred_human", source, outcome)
        elif outcome == "call_opt_out":
            conn.execute(
                "update leads set call_opt_out=true,last_call_outcome=%s,status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (outcome, "caller explicitly opted out of calls", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and channel='call' and status='planned'",
                (lead_id,),
            )
        elif outcome == "do_not_contact":
            conn.execute(
                "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
                "needs_review=false,review_reason=null,review_resolved_at=case when needs_review then now() else review_resolved_at end,"
                "cadence_state='terminated',last_call_outcome=%s,status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (outcome, "caller explicitly requested no contact", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            conn.execute(
                "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                "values(%s,'explicit do-not-contact request',%s,'internal') "
                "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
                "last_verified_at=now()",
                (lead["phone_e164"], source),
            )
            record_status(conn, lead_id, lead["status"], "do_not_contact", source, outcome)
        else:
            conn.execute(
                "update leads set status='needs_attention',cadence_state='paused',last_call_outcome=%s,"
                "needs_review=true,review_reason=%s,review_flagged_at=now(),status_changed_at=now() "
                "where id=%s",
                (outcome, f"call outcome: {outcome}", lead_id),
            )
            skip_remaining_planned(conn, lead_id)
            record_status(conn, lead_id, lead["status"], "needs_attention", source, outcome)
            record_review(
                conn, lead_id, "unrecognized_status", f"call outcome: {outcome}", source="call",
                outreach_event_id=event_id, details={"reported_status": outcome},
            )
        if event["vapi_call_id"]:
            conn.execute(
                "update test_usage_ledger set outcome=%s where provider='vapi' and provider_ref=%s",
                (outcome, event["vapi_call_id"]),
            )
        enqueue_sheet_update(
            conn,
            lead_id=lead_id,
            event_type="call_settled",
            source_key=f"event:{event_id}:{outcome}",
            outreach_event_id=event_id,
        )
        trace.log("database_operation_completed", operation="settle_call")
    trace.log("state_transition_applied", outcome=outcome)
    return "recorded"


def explicit_opt_out(trace: WorkflowTrace, phone: str, channel: str, source: str) -> None:
    if channel not in {"sms", "call"}:
        raise ValueError("channel must be sms or call")
    with transaction() as conn:
        leads = conn.execute(
            "select id,status,call_opt_out,sms_opt_out from leads where phone_e164=%s for update",
            (phone,),
        ).fetchall()
        for lead in leads:
            column = "sms_opt_out" if channel == "sms" else "call_opt_out"
            other_opted_out = lead["call_opt_out"] if channel == "sms" else lead["sms_opt_out"]
            conn.execute(
                f"update leads set {column}=true,status=case when %s then 'do_not_contact' else status end,"
                "cadence_state=case when %s then 'terminated' else cadence_state end,"
                "review_resolved_at=case when %s and needs_review then now() else review_resolved_at end,"
                "review_reason=case when %s then null else review_reason end,"
                "needs_review=case when %s then false else needs_review end,"
                "status_changed_at=case when %s then now() else status_changed_at end where id=%s",
                (other_opted_out, other_opted_out, other_opted_out, other_opted_out, other_opted_out,
                 other_opted_out, lead["id"]),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and channel=%s and status='planned'",
                (lead["id"], channel),
            )
            if other_opted_out and lead["status"] != "do_not_contact":
                record_status(
                    conn,
                    str(lead["id"]),
                    lead["status"],
                    "do_not_contact",
                    source,
                    "all outreach channels opted out",
                )
            if other_opted_out:
                # Do Not Contact always blocks the number (owner, 2026-10-26), as every other path does.
                conn.execute(
                    "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                    "values(%s,'all outreach channels opted out',%s,'internal') "
                    "on conflict(phone_e164) do update set reason=excluded.reason,"
                    "source=excluded.source,last_verified_at=now()",
                    (phone, source),
                )
    trace.log("state_transition_applied", transition="explicit_opt_out", channel=channel)
