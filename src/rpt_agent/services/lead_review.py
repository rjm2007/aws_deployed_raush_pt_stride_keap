"""Staff reviews: why a lead was flagged, in plain words, and what staff chose to do about it.

Incident 2026-10-21: one lead was flagged because its Name cell was edited in the Sheet, another
because its Day 0 text failed (Twilio 30006, a landline). Staff saw "needs review", pressed Resume,
and the cadence looked live while the worker sent nothing (needs_review stayed set). Nobody could see
what had changed, and no record said who decided what.

Every flag now has a review row (`lead_reviews`, migration 048) with a reason code. The code puts it
in a group, and each group offers a short list of choices. A choice either fully resumes the lead
(review cleared, status in_progress, cadence active, the paused steps back on the schedule) or closes
it. Resume and the board's "In cadence" refuse a flagged lead, so a choice is the only way through.

Demo only (2026-10-22): two more groups. "Appointment": a booked lead whose Stride visit was
cancelled, late-cancelled, a no-show or removed; staff close it (front desk), restart the cadence
from Day 0 or text the booking link, never "Continue" (its cadence is over). "Booking": the booking
agent could not confirm a Stride booking; Stride's next file books the lead by itself
(stride_leads.mark_leads_booked_from_stride), otherwise only the front desk can settle it, so a patient who may
be booked is never called again. Migration 048 stays identical to production: these codes come
from Python, and older rows are recognised by their stored words (`_demo_code`).

Owner, 2026-10-25: a Sheet Restart on a lead in review is refused, so the cards offer restart from
Day 0 (name, details, text, call, technical, other); Location / Case / Date Of Birth edits are
reviewed like a name (group "details").
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .lead_status import _practice_clock, clamp_to_business_hours, record_status
from .review import (
    PAUSE_SKIP_REASON,
    REPLACED_REASON,
    RESTARTED_REASON,
    restore_pause_skipped,
    skip_remaining_planned,
)
from .sheet_sync import enqueue_sheet_update

# --- codes, groups, choices -----------------------------------------------------------------

GROUP_OF = {
    "name_changed": "name",
    # Location, case and date of birth edited in the Sheet (owner, 2026-10-25): reviewed like a name.
    "location_changed": "details",
    "case_changed": "details",
    "dob_changed": "details",
    "phone_changed": "phone",
    "sms_failed": "text",
    "booking_link_failed": "text",
    "wrong_person": "call",
    "call_no_outcome": "call",
    "call_outcome_not_applied": "call",
    "unrecognized_status": "call",
    "call_failed": "technical",
    "dispatch_failed": "technical",
    "dispatch_unconfirmed": "technical",
    "call_result_missing": "technical",
    "sms_unconfirmed": "technical",
    "booking_link_unconfirmed": "technical",
    "manual_sms_unconfirmed": "technical",
    "number_blocked": "blocked",
    "moved_by_staff": "other",
    # Demo only: Stride appointments and the booking agent (see the module docstring).
    "appointment_cancelled": "appointment",
    "appointment_late_cancel": "appointment",
    "appointment_no_show": "appointment",
    "appointment_removed": "appointment",
    "appointment_unclear": "appointment",
    "booking_unclear": "booking",
    "booking_patient_exists": "booking",
    "booking_existing_appointment": "booking",
    "booking_not_recorded": "booking",
    "booking_failed": "booking",
    "notification_unconfirmed": "technical",
    "other": "other",
}

GROUP_LABELS = {
    "name": "Name changed",
    "details": "Details changed",
    "phone": "Phone changed",
    "text": "Text failed",
    "call": "Call problem",
    "technical": "Not confirmed",
    "blocked": "Number blocked",
    "appointment": "Appointment",
    "booking": "Booking",
    "other": "Other",
}

TITLES = {
    "name_changed": "Name changed in the Sheet",
    "location_changed": "Location changed in the Sheet",
    "case_changed": "Case changed in the Sheet",
    "dob_changed": "Date of birth changed in the Sheet",
    "phone_changed": "Phone number changed in the Sheet",
    "sms_failed": "Text not delivered",
    "booking_link_failed": "Booking-link text not delivered",
    "wrong_person": "Wrong person or wrong number",
    "call_no_outcome": "Call answered, no outcome",
    "call_outcome_not_applied": "Call outcome could not be applied",
    "unrecognized_status": "Unknown call result",
    "call_failed": "Call could not be placed",
    "dispatch_failed": "Call or text could not be sent",
    "dispatch_unconfirmed": "Call or text not confirmed",
    "call_result_missing": "Call result never arrived",
    "sms_unconfirmed": "Text delivery not confirmed",
    "booking_link_unconfirmed": "Booking-link text not confirmed",
    "manual_sms_unconfirmed": "Dashboard text not confirmed",
    "number_blocked": "Number blocked",
    "moved_by_staff": "Moved to review by staff",
    "appointment_cancelled": "Appointment cancelled in Stride",
    "appointment_late_cancel": "Late cancel in Stride",
    "appointment_no_show": "No show in Stride",
    "appointment_removed": "Appointment removed in Stride",
    "appointment_unclear": "Unclear appointment change in Stride",
    "booking_unclear": "Booking not confirmed in Stride",
    "booking_patient_exists": "Patient already in Stride",
    "booking_existing_appointment": "Stride already has an appointment",
    "booking_not_recorded": "Booked in Stride, not recorded here",
    "booking_failed": "Booking did not go through",
    "notification_unconfirmed": "Booking text not confirmed",
    "other": "Needs review",
}

CHOICE_LABELS = {
    "use_new_name": "Same person, use new name & continue",
    "keep_old_name": "Same person, keep old name & continue",
    "use_new_name_restart": "Same person, use new name & restart from Day 0",
    "keep_old_name_restart": "Same person, keep old name & restart from Day 0",
    # Location / case / date of birth: "value" is the field the review is about (`choice_label`
    # names it, e.g. "Use new location & continue"); these generic words are the fallback.
    "use_new_value": "Use new value & continue",
    "keep_old_value": "Keep old value & continue",
    "use_new_value_restart": "Use new value & restart from Day 0",
    "keep_old_value_restart": "Keep old value & restart from Day 0",
    "different_person": "Different person, close this lead",
    "calls_only": "Continue with calls only",
    "retry_texts": "Try texting again",
    "number_ok": "Number is right, continue",
    "wrong_number": "Wrong number, close lead",
    "continue": "Continue",
    "close_phone_changed": "Close this lead (new number goes in a new row)",
    "restart_cadence": "Restart cadence from Day 0",
    "send_booking_link": "Text the booking link",
    "front_desk": "Front desk will handle",
}

# Owner, 2026-10-25: a Restart picked in the Sheet on a lead in review is refused; the review card
# offers the restart instead (name, details, text, call, technical, other; appointment had it).
# Never for phone (closed for good), blocked (an admin unblocks) or booking (may be booked already).
GROUP_CHOICES = {
    "name": ["use_new_name", "keep_old_name", "use_new_name_restart", "keep_old_name_restart",
             "different_person", "front_desk"],
    "details": ["use_new_value", "keep_old_value", "use_new_value_restart", "keep_old_value_restart",
                "front_desk"],
    # Only for leads flagged before 2026-10-22; a phone change now closes the lead at once.
    "phone": ["close_phone_changed", "front_desk"],
    "text": ["calls_only", "retry_texts", "restart_cadence", "front_desk"],
    "call": ["number_ok", "wrong_number", "restart_cadence", "front_desk"],
    "technical": ["continue", "restart_cadence", "front_desk"],
    # Unblocking stays where it was (lead page / Administration, admins only).
    "blocked": ["front_desk"],
    # Demo only. No "Continue": an appointment lead's cadence is over, and a booking lead may be
    # booked already (owner, 2026-10-22).
    "appointment": ["front_desk", "restart_cadence", "send_booking_link"],
    "booking": ["front_desk"],
    "other": ["continue", "restart_cadence", "front_desk"],
}

RESUME_CHOICES = {"use_new_name", "keep_old_name", "use_new_value", "keep_old_value", "calls_only",
                  "retry_texts", "number_ok", "continue"}
# Restart from Day 0. A variant also applies its name / value choice first (RESTART_BASE).
RESTART_CHOICES = {"restart_cadence", "use_new_name_restart", "keep_old_name_restart",
                   "use_new_value_restart", "keep_old_value_restart"}
RESTART_BASE = {"use_new_name_restart": "use_new_name", "keep_old_name_restart": "keep_old_name",
                "use_new_value_restart": "use_new_value",
                "keep_old_value_restart": "keep_old_value"}
KEEP_OLD_NAME = {"keep_old_name", "keep_old_name_restart"}
KEEP_OLD_VALUE = {"keep_old_value", "keep_old_value_restart"}
# Review code -> (lead column, Sheet column key, the field in staff words).
DETAIL_FIELDS = {
    "location_changed": ("location", "location", "location"),
    "case_changed": ("lead_type", "case", "case"),
    "dob_changed": ("date_of_birth", "date_of_birth", "date of birth"),
}


def choice_label(choice: str, code: str | None = None) -> str:
    """A choice in staff words; "value" choices name the field of their review ("Use new case & ...")."""
    label = CHOICE_LABELS.get(choice, choice)
    if code in DETAIL_FIELDS and "value" in label:
        label = label.replace("value", DETAIL_FIELDS[code][2])
    return label
# Closed for good: the number (or the person) on this lead is wrong. Staff add a new Sheet row.
WRONG_LEAD_CHOICES = {"different_person", "wrong_number", "close_phone_changed"}

FRONT_DESK_REASON = "Handled by front desk"
TEXTS_OFF_SKIP_REASON = "texts off (calls only)"

# Outcome Status in the Sheet (no new columns: only the words in the existing cell change).
SHEET_RESOLVED = {
    "use_new_name": "Resolved: new name used",
    "keep_old_name": "Resolved: old name kept",
    "use_new_name_restart": "Resolved: new name used; cadence restarted",
    "keep_old_name_restart": "Resolved: old name kept; cadence restarted",
    # "value": the field (sheet_resolved_text), e.g. "Resolved: old location kept".
    "use_new_value": "Resolved: new value used",
    "keep_old_value": "Resolved: old value kept",
    "use_new_value_restart": "Resolved: new value used; cadence restarted",
    "keep_old_value_restart": "Resolved: old value kept; cadence restarted",
    "different_person": "Closed: different person, add a new row",
    "calls_only": "Resolved: calls only",
    "retry_texts": "Resolved: texting again",
    "number_ok": "Resolved: number confirmed",
    "wrong_number": "Closed: wrong number",
    "continue": "Resolved: continued",
    "close_phone_changed": "Phone changed: add a new row",
    "restart_cadence": "Cadence restarted",
    "send_booking_link": "Resolved: booking link texted",
    "front_desk": "Closed: handled by front desk",
}
# The Action set with the resolution (default "Start cadence"), n8n mode only: direct mode never
# fills Action (owner, 2026-10-23/25; it only empties it once a command is handled). A restart is shown as the Sheet's own restart is.
SHEET_RESOLVED_ACTION = {"restart_cadence": "Restart cadence"}
SHEET_TEXTS_OFF = "Calls only (texts off)"


def sheet_resolved_text(resolution: str | None, code: str | None = None) -> str | None:
    """Outcome Status for a resolved review; "value" names the field ("Resolved: old case kept")."""
    text = SHEET_RESOLVED.get(resolution or "")
    if text and code in DETAIL_FIELDS and "value" in text:
        text = text.replace("value", DETAIL_FIELDS[code][2])
    return text


def sheet_open_status(code: str | None, details: dict | None) -> str:
    """Outcome Status while a review is open. Each starts "Needs review" (or "Number blocked;
    needs review"). On such a lead the Sheet runs only Booked / Do Not Contact; a Restart is
    refused with a reason (owner, 2026-10-25: restart it from the review card instead)."""
    group = group_of(code)
    if code == "number_blocked":
        return "Number blocked; needs review"  # the words the Start command already writes
    if code == "name_changed":
        return "Needs review: name changed"
    if code in DETAIL_FIELDS:
        return f"Needs review: {DETAIL_FIELDS[code][2]} changed"
    if group == "phone":
        return "Needs review: phone changed"
    if group == "text":
        landline = sms_failure((details or {}).get("error_code"))["landline"]
        return "Needs review: text failed (landline)" if landline else "Needs review: text failed"
    if code == "wrong_person":
        return "Needs review: wrong number"
    if group == "call":
        return "Needs review: call result unclear"
    if group == "technical":
        return "Needs review: not confirmed"
    if group == "appointment":
        return "Needs review: " + TITLES.get(code or "", "appointment changed in Stride").lower().replace(
            "stride", "Stride")
    if group == "booking":
        return "Needs review: booking not confirmed in Stride"
    return "Needs review"

SOURCE_LABELS = {
    "sheet": "Google Sheet",
    "text": "Text message",
    "call": "Call",
    "worker": "Outreach worker",
    "dashboard": "Dashboard",
    "db": "System",
    "legacy": "System",
    "system": "System",
    "stride": "Stride import",
    "booking": "Booking agent",
}

# Twilio's error codes, in staff words. Landline: the number cannot take texts at all, so trying
# again is pointless (the option is hidden). Permanent: the same, for other reasons.
TWILIO_REASONS = {
    "30006": "this number can't receive texts (likely a landline)",
    "21614": "this number can't receive texts (likely a landline)",
    "30005": "this number doesn't exist or is no longer in service",
    "21211": "this phone number is not valid",
    "21610": "the patient has replied STOP to our number before",
    "30003": "the phone was unreachable (switched off or out of signal)",
    "30004": "the message was blocked by the patient or their carrier",
    "30007": "the carrier filtered the message as spam",
    "30008": "the carrier reported an unknown error",
    "30034": "our texting number is not yet registered with US carriers",
    "30032": "our toll-free texting number is not verified yet",
}
LANDLINE_CODES = {"30006", "21614"}
PERMANENT_CODES = LANDLINE_CODES | {"30005", "21211", "21610"}


class ReviewError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def group_of(code: str | None) -> str:
    return GROUP_OF.get(code or "other", "other")


# Stored words -> demo code, for rows migration 048 wrote from review_reason (flagged before the
# port, or by a path that wrote no row of its own). 048's SQL function is production's, so it
# calls these "other" (Continue / Front desk); Continue would put a lead whose booking fell through,
# or may have gone through, back into calls.
_DEMO_EXACT = {
    "Cancelled in Stride": "appointment_cancelled",
    "Late cancel in Stride": "appointment_late_cancel",
    "No show in Stride": "appointment_no_show",
    "Appointment removed in Stride": "appointment_removed",
    "Unclear change in Stride": "appointment_unclear",
    "Stride duplicate patient requires mapping": "booking_patient_exists",
    "Stride says this patient already exists; staff must book manually": "booking_patient_exists",
    "Stride reports an existing appointment; reconcile mapping": "booking_existing_appointment",
    "ambiguous Stride booking result; do not retry": "booking_unclear",
    "unexpected Stride booking result; reconcile before retry": "booking_unclear",
    "stale Stride booking requires reconciliation": "booking_unclear",
    "ambiguous confirmation SMS; do not retry": "notification_unconfirmed",
}


def demo_code_for_reason(reason: str | None) -> str | None:
    """The demo review code for a stored reason, or None when the words are not a demo flag."""
    text = str(reason or "").strip()
    if text in _DEMO_EXACT:
        return _DEMO_EXACT[text]
    lowered = text.lower()
    if lowered.startswith("booked in stride (appointment"):
        return "booking_not_recorded"
    if lowered.endswith("; confirm in stride") and (
            lowered.startswith(("stride ", "unexpected error during"))):
        return "booking_unclear"
    return None


def _demo_code(row: dict) -> str:
    code = row.get("code") or "other"
    if code in {"other", "dispatch_unconfirmed"}:
        return demo_code_for_reason(row.get("message")) or code
    return code


def sms_failure(error_code: Any) -> dict[str, Any]:
    """Plain reason for a failed text, and whether trying again could ever work."""
    code = str(error_code or "").strip()
    if code in TWILIO_REASONS:
        reason = TWILIO_REASONS[code]
    elif code:
        reason = f"the carrier did not deliver it (Twilio error {code})"
    else:
        reason = "the carrier did not deliver it (no reason given)"
    return {
        "reason": reason,
        "error_code": code or None,
        "landline": code in LANDLINE_CODES,
        "permanent": code in PERMANENT_CODES,
    }


def step_name(channel: Any, day_offset: Any) -> str:
    kind = "call" if channel == "call" else "text"
    return f"Day {int(day_offset)} {kind}" if day_offset is not None else f"Callback {kind}"


def mask_phone(phone: Any) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"…{digits[-4:]}" if digits else "…"


def _clinic_time(value: datetime, tz) -> str:
    local = value.astimezone(tz)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%a} {local:%b} {local.day}, {hour}:{local:%M} {local:%p} PT"


def _clinic_date(value: datetime, tz) -> str:
    local = value.astimezone(tz)
    return f"{local:%b} {local.day}, {local.year}"


# --- reading reviews --------------------------------------------------------------------------

REVIEW_COLUMNS = (
    "id,lead_id,code,message,details,source,outreach_event_id,flagged_at,resolved_at,"
    "resolved_by,resolved_by_name,resolution,note"
)


def open_reviews(conn, lead_id: str) -> list[dict]:
    """Open review rows for a lead, oldest first: staff settle them one at a time, in order.

    QA 2026-10-22: one choice used to settle every open review, so a lead with a landline text
    failure AND a name change could be resumed by "use new name" with texts on, to a landline.
    """
    return conn.execute(
        f"select {REVIEW_COLUMNS} from lead_reviews where lead_id=%s and resolved_at is null "
        "order by flagged_at,id",
        (lead_id,),
    ).fetchall()


def _legacy_row(lead: dict) -> dict:
    """A flagged lead with no review row (written before migration 048 reached it)."""
    return {
        "id": None, "lead_id": lead.get("id"), "code": "other",
        "message": lead.get("review_reason") or "Needs review", "details": {},
        "source": "legacy", "outreach_event_id": None,
        "flagged_at": lead.get("review_flagged_at"), "resolved_at": None,
        "resolved_by": None, "resolved_by_name": None, "resolution": None, "note": None,
    }


def _plain_reason(row: dict, lead: dict) -> str:
    code = row["code"]
    details = row.get("details") or {}
    if code == "name_changed":
        old = details.get("old_name") or lead.get("full_name") or "?"
        if not details.get("new_name"):
            # Flagged before 2026-10-22: only "Name changed; needs review" was stored (the D. S. lead).
            return (f"The Name in the Google Sheet was changed; the new name was not recorded (flagged "
                    f"before Oct 22). The name on file is “{old}”. Check the Sheet row: keep the name on "
                    "file, or close the lead if it is a different person.")
        return (f"The Name in the Google Sheet changed from “{old}” "
                f"to “{details['new_name']}”. Is it the same person?")
    if code in DETAIL_FIELDS:
        field = DETAIL_FIELDS[code][2]
        return (f"The {field} in the Google Sheet changed from “{details.get('old_value') or '—'}” "
                f"to “{details.get('new_value') or '—'}”. Which one is right? Outreach is paused "
                "until staff choose.")
    if code in {"sms_failed", "booking_link_failed"}:
        what = "booking-link text" if code == "booking_link_failed" else (
            step_name("sms", details["day_offset"]) if details.get("day_offset") is not None
            else "text")
        failure = sms_failure(details.get("error_code"))
        if not details.get("error_code") and details.get("refused"):
            failure["reason"] = "Twilio refused to send it"
        return f"The {what} was not delivered: {failure['reason']}."
    if code == "wrong_person":
        # Rows from before 048 keep the agent's note only as the stored reason text.
        message = str(row.get("message") or "").strip()
        note = str(details.get("note") or "").strip() or (
            message if message.lower() not in {"", "wrong phone number", "needs review"} else "")
        base = "On the call, the person said this is the wrong number or not the patient."
        return f"{base} Agent's note: {note}" if note else base
    if code == "call_no_outcome":
        return "The call was answered, but the agent reported no outcome. Read the transcript."
    if code == "call_outcome_not_applied":
        return "The call summary gave an outcome the system could not apply. Read the transcript."
    if code == "unrecognized_status":
        return "The agent reported a call result the system does not know. Read the transcript."
    if code == "call_failed":
        return "The cadence call could not be placed (the phone provider refused it)."
    if code == "dispatch_failed":
        return "A cadence call or text could not be sent."
    if code == "dispatch_unconfirmed":
        return ("A call or text was handed to the provider but its result never came back. It was "
                "not retried, so the patient is not contacted twice.")
    if code == "call_result_missing":
        return "A call was placed but its result never arrived. It was not retried."
    if code == "sms_unconfirmed":
        return "A text was sent but its delivery report never arrived. It was not retried."
    if code == "booking_link_unconfirmed":
        return "The booking-link text may or may not have gone out. It was not retried."
    if code == "manual_sms_unconfirmed":
        return "A text sent from the dashboard may or may not have gone out. It was not retried."
    if code == "number_blocked":
        return ("This number is on the blocked list (the patient once asked not to be contacted). "
                "Nothing goes out until an admin unblocks it on this lead's page.")
    if code == "moved_by_staff":
        who = details.get("by")
        return f"{who} moved this lead to Needs Review on the board." if who else \
            "Staff moved this lead to Needs Review on the board."
    if group_of(code) == "appointment":
        what = {
            "appointment_cancelled": "was cancelled",
            "appointment_late_cancel": "was cancelled late",
            "appointment_no_show": "was marked a no-show",
            "appointment_removed": "was removed",
            "appointment_unclear": "changed in a way the import could not match to one visit",
        }.get(code, "changed")
        return (f"This lead was booked, but the appointment {what} in Stride. Outreach stays stopped "
                "until staff choose. If Stride's next file shows a new appointment, the lead is "
                "marked Booked again by itself.")
    if code == "booking_failed":
        return ("The booking agent's booking did not go through: Stride still shows that time as open "
                "(checked automatically). The patient was told our team would confirm by text, so the "
                "front desk books by hand. Calls and texts stay stopped.")
    if group_of(code) == "booking":
        what = {
            "booking_patient_exists": "Stride says this patient already exists, so the booking agent "
                                      "could not book. The front desk books by hand.",
            "booking_existing_appointment": "Stride reports an appointment already exists for this "
                                            "patient. The front desk checks it in Stride.",
            "booking_not_recorded": "The appointment was made in Stride, but our records did not "
                                    "update.",
        }.get(code, "The booking agent could not confirm whether the appointment was saved in "
                    "Stride, so it did not try again.")
        return (f"{what} If Stride's next file (about every 30 minutes) shows the appointment, the "
                "lead is marked Booked and this review closes by itself. Calls and texts stay "
                "stopped, so a booked patient is never called again.")
    if code == "notification_unconfirmed":
        return "A booking text may or may not have gone out. It was not retried."
    if code == "phone_changed":
        if not details.get("new_phone"):  # flagged before Oct 22: the numbers were not recorded
            return ("The phone number changed in the Google Sheet (flagged before Oct 22, so the numbers "
                    "were not recorded). Close this lead and add the new number as a new row.")
        return row.get("message") or "The phone number changed in the Google Sheet."
    return row.get("message") or lead.get("review_reason") or "This lead needs staff review."


def _proof(row: dict, lead_name: str | None = None) -> list[dict[str, str]]:
    code = row["code"]
    details = row.get("details") or {}
    proof: list[dict[str, str]] = []
    if code == "name_changed":
        proof += [{"label": "Old name", "value": str(details.get("old_name") or lead_name or "—")},
                  {"label": "New name", "value": str(details.get("new_name") or "Not recorded (see Sheet)")}]
    if code in DETAIL_FIELDS:
        field = DETAIL_FIELDS[code][2]
        proof += [{"label": f"Old {field}", "value": str(details.get("old_value") or "—")},
                  {"label": f"New {field}", "value": str(details.get("new_value") or "—")}]
    if code == "phone_changed":
        if details.get("old_phone"):
            proof.append({"label": "Old number", "value": str(details["old_phone"])})
        if details.get("new_phone"):
            proof.append({"label": "New number", "value": str(details["new_phone"])})
    if code in {"sms_failed", "booking_link_failed"}:
        if details.get("day_offset") is not None:
            proof.append({"label": "Step", "value": step_name("sms", details["day_offset"])})
        if details.get("error_code"):
            proof.append({"label": "Error code", "value": f"Twilio {details['error_code']}"})
    if group_of(code) in {"appointment", "booking"} and row.get("message"):
        proof.append({"label": "Recorded reason", "value": str(row["message"])[:200]})
    if code in {"call_failed", "dispatch_failed", "dispatch_unconfirmed", "sms_unconfirmed",
                "call_result_missing"} and details.get("channel"):
        proof.append({"label": "Step", "value": step_name(details["channel"], details.get("day_offset"))})
    return proof


def describe(row: dict, lead: dict, *, has_calls: bool = False, more: bool = False) -> dict[str, Any]:
    """One review as the dashboard shows it: what happened, the proof, and the choices.

    `more`: other reviews are open after this one. Then no restart is offered: a restart would
    settle every open review at once (the next one never decided, e.g. a changed location left
    unread after "use new name & restart"). Staff continue here and restart on the last review.
    """
    code = _demo_code(row)
    code = code if code in GROUP_OF else "other"
    details = row.get("details") or {}
    group = group_of(code)
    failure = sms_failure(details.get("error_code")) if group == "text" else None
    no_texts = bool(lead.get("sms_opt_out") or lead.get("sms_disabled"))
    choices = [
        choice for choice in GROUP_CHOICES[group]
        if not (choice == "retry_texts" and failure and failure["permanent"])
        # A pre-048 name review never recorded the new name, so there is nothing to "use".
        and not (choice in {"use_new_name", "use_new_name_restart"} and not details.get("new_name"))
        and not (choice in {"use_new_value", "use_new_value_restart"} and not details.get("new_value"))
        # Calls only / STOP: no text of any kind (owner, 2026-10-22).
        and not (choice == "send_booking_link" and no_texts)
        and not (choice in RESTART_CHOICES and more)
    ]
    lead_id = str(lead.get("id") or row.get("lead_id"))
    return {
        "id": row.get("id"),
        "code": code,
        "group": group,
        "group_label": GROUP_LABELS[group],
        "title": TITLES[code],
        "reason": _plain_reason({**row, "code": code}, lead),
        "proof": _proof({**row, "code": code}, lead.get("full_name")),
        "flagged_at": row.get("flagged_at"),
        "source": row.get("source"),
        "source_label": SOURCE_LABELS.get(str(row.get("source")), "System"),
        "landline": bool(failure and failure["landline"]),
        "transcript_url": f"/leads/{lead_id}/conversations/calls"
        if group == "call" and has_calls else None,
        "options": [{"choice": choice, "label": choice_label(choice, code)} for choice in choices],
    }


def lead_review_summary(conn, lead: dict) -> dict[str, Any] | None:
    """The open review a flagged lead shows (the oldest open one), with the others listed."""
    if not lead.get("needs_review"):
        return None
    rows = open_reviews(conn, str(lead["id"]))
    primary = rows[0] if rows else _legacy_row(lead)
    has_calls = bool(conn.execute(
        "select 1 from call_logs where lead_id=%s limit 1", (lead["id"],)
    ).fetchone())
    summary = describe(primary, lead, has_calls=has_calls, more=len(rows) > 1)
    summary["also"] = [
        {"title": TITLES.get(row["code"], TITLES["other"]), "flagged_at": row["flagged_at"]}
        for row in rows[1:]
    ]
    return summary


# --- what a choice will do ----------------------------------------------------------------------


def _first_flag_time(rows: list[dict], lead: dict) -> datetime:
    times = [row["flagged_at"] for row in rows if row.get("flagged_at")]
    if lead.get("review_flagged_at"):
        times.append(lead["review_flagged_at"])
    return min(times) if times else datetime.now(UTC)


def _resume_shift(conn, lead_id: str, flagged_at: datetime, now: datetime) -> tuple[timedelta, list]:
    """How far the paused steps move on resume, and those steps.

    Pause means postpone: every remaining step moves by the time the lead sat in review, so the
    cadence keeps its spacing (day 5 still lands two days after day 3). A step that was already
    overdue when the lead was flagged (held back by calling hours) would still be overdue, and with
    it everything behind it, so the shift is at least enough to bring the earliest step to now:
    one step goes out promptly, the rest keep their gaps. The worker still applies calling hours,
    holidays and daily limits.
    """
    steps = conn.execute(
        "select oe.id,oe.channel,oe.day_offset,oe.scheduled_for,oe.status,cs.description "
        "from outreach_events oe left join cadence_steps cs on cs.id=oe.cadence_step_id "
        "where oe.lead_id=%s and (oe.status='planned' or (oe.status='skipped' and oe.failure_reason=%s)) "
        "order by oe.scheduled_for nulls last,oe.id",
        (lead_id, PAUSE_SKIP_REASON),
    ).fetchall()
    times = [step["scheduled_for"] for step in steps if step["scheduled_for"]]
    shift = max(now - flagged_at, now - min(times) if times else timedelta(0), timedelta(0))
    return shift, steps


def _failed_text_event(conn, lead_id: str, rows: list[dict]) -> dict | None:
    """The failed cadence text a "Try texting again" sends again (the review's own step)."""
    for row in rows:
        if row.get("outreach_event_id") and group_of(row["code"]) == "text":
            event = conn.execute(
                "select id,cadence_step_id,cadence_version_id,channel,day_offset,status "
                "from outreach_events where id=%s and lead_id=%s and channel='sms' "
                "and status in ('failed','unknown')",
                (row["outreach_event_id"], lead_id),
            ).fetchone()
            if event:
                return event
    return None


def _next_step_text(conn, lead: dict, rows: list[dict], choice: str, now: datetime) -> str:
    lead_id = str(lead["id"])
    shift, steps = _resume_shift(conn, lead_id, _first_flag_time(rows, lead), now)
    upcoming = [
        (step["scheduled_for"] + shift, step_name(step["channel"], step["day_offset"]))
        for step in steps
        if step["scheduled_for"] and not (choice == "calls_only" and step["channel"] == "sms")
        and not (step["channel"] == "sms" and lead.get("sms_disabled") and choice != "retry_texts")
    ]
    if choice == "retry_texts" and (failed := _failed_text_event(conn, lead_id, rows)):
        upcoming.append((now, f"{step_name('sms', failed['day_offset'])} again"))
    if not upcoming:
        return "none left, so the lead moves to Closed"
    when, name = min(upcoming, key=lambda item: item[0])
    tz, hours, holidays = _practice_clock(conn, lead_id)
    if not lead.get("is_test"):
        when = clamp_to_business_hours(max(when, now), tz, hours, holidays)
    if when <= now + timedelta(minutes=2):
        return f"{name}, right away (within calling hours)"
    return f"{name}, {_clinic_time(when, tz)}"


def _effect(choice: str, details: dict, lead: dict, group: str = "other", code: str = "") -> str:
    if choice in RESTART_BASE:
        return (f"{_effect(RESTART_BASE[choice], details, lead, group, code)}. "
                f"{_effect('restart_cadence', details, lead, group, code)}")
    if choice in {"use_new_value", "keep_old_value"}:
        field = DETAIL_FIELDS.get(code, ("", "", "value"))[2]
        if choice == "use_new_value":
            return f"The {field} becomes {details.get('new_value') or 'the Sheet value'}"
        return (f"The {field} stays {details.get('old_value') or 'as on file'}; the Sheet cell is "
                "set back to it")
    if choice == "use_new_name":
        return f"Name becomes {details.get('new_name') or 'the Sheet name'}"
    if choice == "keep_old_name":
        if not details.get("new_name"):
            return (f"Name stays {lead.get('full_name')}; the name the Sheet worker last read from the row "
                    "is accepted")
        return f"Name stays {lead.get('full_name')}"
    if choice == "calls_only":
        return "Texts off"
    if choice == "retry_texts":
        if code == "booking_link_failed":
            # QA 2026-10-22: this promised a resend that never happened. The link was asked for on
            # one call; it is not sent again by itself (the agent offers it on the next call).
            return ("Texts stay on. The booking link is not resent automatically; the next cadence "
                    "text goes out as planned")
        return "Texts stay on and the failed text goes out again"
    if choice == "number_ok":
        return "Number kept"
    if choice == "continue":
        if group == "technical":
            return "The unconfirmed step is not repeated"
        return "Outreach picks up where it paused"
    if choice == "front_desk":
        if group == "booking":
            return ("The lead moves to Closed (Handled by front desk); it still becomes Booked if "
                    "Stride's next file shows the appointment")
        return "Outreach stops and the lead moves to Closed (Handled by front desk)"
    if choice == "restart_cadence":
        return "The old schedule is discarded and a new cadence starts from Day 0"
    if choice == "send_booking_link":
        return "The booking-link text is sent; no calls or other texts follow"
    return "Outreach stops and the lead moves to Closed for good"


def options_preview(conn, lead: dict, now: datetime | None = None) -> dict[str, Any]:
    """The review card plus, per choice, what will happen and the next step (confirm pop-up)."""
    now = now or datetime.now(UTC)
    summary = lead_review_summary(conn, lead)
    if not summary:
        return {"review": None, "options": [], "closed": closed_review(conn, lead)}
    rows = open_reviews(conn, str(lead["id"])) or [_legacy_row(lead)]
    details = rows[0].get("details") or {}
    options = []
    for option in summary["options"]:
        choice = option["choice"]
        effect = _effect(choice, details, lead, summary["group"], summary["code"])
        if choice in RESUME_CHOICES and len(rows) > 1:
            next_step = (f"Next step: the next open review ({TITLES.get(rows[1]['code'], TITLES['other'])}); "
                         "the lead stays paused until every review is resolved.")
        elif choice in RESUME_CHOICES:
            next_step = f"Next step: {_next_step_text(conn, lead, rows, choice, now)}."
        elif choice == "front_desk":
            next_step = "Next step: the front desk contacts the patient."
        elif choice in RESTART_CHOICES:
            next_step = "Next step: the Day 0 call, within calling hours."
        elif choice == "send_booking_link":
            next_step = "Next step: the booking link goes out by text within a minute."
        else:
            next_step = "Next step: add the patient as a new row in the Google Sheet."
        options.append({
            **option,
            "effect": effect,
            "next_step": next_step,
            "confirm": f"{option['label']} for {lead.get('full_name')}. {effect}. {next_step}",
        })
    return {"review": summary, "options": options}


# How a review closed without a staff choice (lead page Activity and the closed-review message).
OUTCOME_LABELS = {
    "booked": "Marked Booked", "do_not_contact": "Do Not Contact", "restarted": "Restarted",
    "replaced": "Replaced by a newer referral", "unblocked": "Number unblocked",
    "closed": "Closed", "board_closed": "Moved to Closed on the board",
    "board_booked": "Moved to Booked on the board",
    "board_new": "Restarted from the board",
    "confirmed_in_stride": "Booking confirmed in Stride",
}

REPLACED_REVIEW_MESSAGE = (
    "This lead was closed: a newer Sheet row uses the same phone number. Its history is kept."
)
RESTARTED_REVIEW_MESSAGE = (
    "This lead was closed: another row with this phone number was restarted. Its history is kept."
)
CLOSED_REVIEW_MESSAGE = "This review is already closed."


def resolution_label(resolution: str | None, code: str | None = None) -> str | None:
    """The plain words for a stored lead_reviews.resolution, or None when unknown."""
    if resolution in CHOICE_LABELS:
        return choice_label(resolution, code)
    return OUTCOME_LABELS.get(resolution or "")


def closed_review(conn, lead: dict) -> dict[str, str]:
    """Why a lead has no open review any more, for a page opened before it closed.

    A stale lead page still showed Resolve review after a newer Sheet row on the same number
    replaced the lead (2026-10-23), and Confirm only said the choice was no longer available.
    """
    latest = conn.execute(
        "select code,resolution,resolved_by_name from lead_reviews where lead_id=%s "
        "and resolved_at is not null order by resolved_at desc,id desc limit 1",
        (lead["id"],),
    ).fetchone()
    if lead.get("status_reason") == RESTARTED_REASON:
        return {"reason": "replaced", "message": RESTARTED_REVIEW_MESSAGE}
    if lead.get("status_reason") == REPLACED_REASON or (latest and latest["resolution"] == "replaced"):
        return {"reason": "replaced", "message": REPLACED_REVIEW_MESSAGE}
    label = resolution_label(latest["resolution"], latest.get("code")) if latest else None
    if latest and latest.get("resolved_by_name") and label:
        return {
            "reason": "resolved",
            "message": f"This review was already resolved by {latest['resolved_by_name']}: {label}.",
            "resolved_by_name": latest["resolved_by_name"],
            "resolution": latest["resolution"],
        }
    return {"reason": "closed", "message": CLOSED_REVIEW_MESSAGE}


# --- resolving ---------------------------------------------------------------------------------


def close_open_reviews(
    conn, lead_id: str, resolution: str, *, by: str | None, by_name: str | None,
    note: str | None = None,
) -> int:
    """Mark every open review of a lead resolved (before needs_review is cleared, so migration
    048's clear trigger finds nothing left and the record names the person and the choice)."""
    rows = conn.execute(
        "update lead_reviews set resolved_at=now(),resolved_by=%s,resolved_by_name=%s,"
        "resolution=%s,note=%s,updated_at=now() where lead_id=%s and resolved_at is null returning id",
        (by or "system", (by_name or by or "System")[:120], resolution, note, lead_id),
    ).fetchall()
    return len(rows)


def _legacy_sheet_name(conn, lead: dict, row: dict) -> dict:
    """"Keep old name" on a name review flagged before 2026-10-22, which never stored the new name.

    QA 2026-10-22: accepting "whatever the Sheet holds next" let a different person's name typed
    over the row pass silently. Accepted instead: only the Name the Sheet worker last read from the
    row (sheet_row_state.sheet_name), which is recorded on the review too. Unknown -> refused.
    """
    found = conn.execute(
        "select sheet_name from sheet_row_state where lead_id=%s", (lead["id"],)
    ).fetchone()
    name = " ".join(str((found or {}).get("sheet_name") or "").split())
    if not name:
        raise ReviewError(409, "The name in the Sheet row is not recorded yet (the next Sheet read, "
                               "within about 5 minutes, records it). Try again then, or choose "
                               "Different person or Front desk will handle.")
    details = {"old_name": lead.get("full_name"), "new_name": name}
    if row.get("id"):
        conn.execute("update lead_reviews set details=details||%s where id=%s",
                     (Jsonb(details), row["id"]))
    # The Sheet already matches the name on file: nothing to accept.
    same = " ".join(name.lower().split()) == " ".join(str(lead.get("full_name") or "").lower().split())
    return {**details, "new_name": None} if same else details


def _split_name(full_name: str) -> tuple[str, str | None]:
    parts = full_name.split()
    return parts[0], " ".join(parts[1:]) or None


def _resume_guards(conn, lead: dict, choice: str) -> None:
    if lead.get("call_opt_out") and (lead.get("sms_opt_out") or choice == "calls_only"
                                       or lead.get("sms_disabled")):
        raise ReviewError(409, "This lead has opted out of calls and cannot be texted, so outreach "
                               "cannot continue. Choose Front desk will handle.")
    if lead.get("phone_e164") and conn.execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],)
    ).fetchone():
        raise ReviewError(409, "This number is blocked. An admin can unblock it on this lead's page.")


def _apply_choice(conn, lead: dict, rows: list[dict], choice: str, details: dict) -> None:
    """What the choice itself changes on the lead (name, texts), before or without resuming."""
    lead_id = str(lead["id"])
    if choice == "use_new_name":
        new_name = " ".join(str(details.get("new_name") or "").split())
        if not new_name:
            raise ReviewError(409, "The new name is not recorded on this review.")
        first, last = _split_name(new_name)
        conn.execute(
            "update leads set full_name=%s,first_name=%s,last_name=%s,sheet_name_accepted=null "
            "where id=%s",
            (new_name, first, last, lead_id),
        )
    elif choice == "keep_old_name":
        conn.execute(
            "update leads set sheet_name_accepted=%s where id=%s",
            (" ".join(str(details.get("new_name") or "").split()) or None, lead_id),
        )
    elif choice == "use_new_value":
        # The value the review recorded (`new_raw`: the date for a date of birth), never re-read.
        column = DETAIL_FIELDS.get(rows[0].get("code"), (None,))[0]
        value = details.get("new_raw") or details.get("new_value")
        if not column or not value:
            raise ReviewError(409, "The new value is not recorded on this review.")
        if column == "date_of_birth":
            value = date.fromisoformat(str(value))
        conn.execute(f"update leads set {column}=%s,updated_at=now() where id=%s", (value, lead_id))
    # keep_old_value changes nothing here: the lead keeps its value, and the resolution itself is
    # what the profile sync reads as "staff kept the old one" (lead_actions._kept_value).
    elif choice == "calls_only":
        conn.execute(
            "update leads set sms_disabled=true,sms_disabled_at=now(),sms_disabled_reason=%s where id=%s",
            (_plain_reason(rows[0], lead)[:500], lead_id),
        )
    elif choice == "retry_texts":
        conn.execute(
            "update leads set sms_disabled=false,sms_disabled_at=null,sms_disabled_reason=null "
            "where id=%s",
            (lead_id,),
        )


def _resume(conn, lead: dict, rows: list[dict], choice: str, now: datetime) -> dict:
    lead_id = str(lead["id"])
    lead = {**lead, **conn.execute(
        "select sms_disabled from leads where id=%s", (lead_id,)).fetchone()}
    shift, _ = _resume_shift(conn, lead_id, _first_flag_time(rows, lead), now)
    restored = restore_pause_skipped(conn, lead_id)
    shifted = len(conn.execute(
        "update outreach_events set scheduled_for=scheduled_for+%s,updated_at=now() "
        "where lead_id=%s and status='planned' returning id",
        (shift, lead_id),
    ).fetchall())
    texts_off = choice == "calls_only" or (lead.get("sms_disabled") and choice != "retry_texts")
    if texts_off:
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and channel='sms' and status='planned'",
            (TEXTS_OFF_SKIP_REASON, lead_id),
        )
    pending = conn.execute(
        f"select {REVIEW_COLUMNS} from lead_reviews where lead_id=%s and resolution='retry_texts' "
        "and details->>'retry_pending'='true'",
        (lead_id,),
    ).fetchall()
    retry_rows = (rows if choice == "retry_texts" else []) + list(pending)
    if retry_rows and not lead.get("sms_disabled") and choice != "calls_only" and (
            failed := _failed_text_event(conn, lead_id, retry_rows)):
        # sms_messages links one row per event, so the retry is a new event for the same step.
        conn.execute(
            "insert into outreach_events(lead_id,cadence_step_id,cadence_version_id,channel,"
            "day_offset,scheduled_for,status,attempt_no) values(%s,%s,%s,'sms',%s,now(),'planned',1)",
            (lead_id, failed["cadence_step_id"], failed["cadence_version_id"], failed["day_offset"]),
        )
    if pending:
        conn.execute(
            "update lead_reviews set details=details-'retry_pending' where id=any(%s)",
            ([row["id"] for row in pending],),
        )
    conn.execute(
        "update leads set needs_review=false,review_reason=null,review_resolved_at=now(),"
        "status='in_progress',cadence_state='active',status_reason=%s,status_changed_at=now() "
        "where id=%s",
        (f"review resolved: {choice_label(choice, rows[0].get('code'))}"[:500], lead_id),
    )
    if lead.get("phone_e164"):
        # One active lead per phone on every path that sets a lead going again (2026-10-26).
        from .review import hand_over_number

        hand_over_number(conn, practice_id=lead["practice_id"], phone=lead["phone_e164"],
                         lead_id=lead_id, reason=RESTARTED_REASON)
    return {"restored_steps": restored, "shifted_steps": shifted}


def _close(conn, lead: dict, choice: str) -> None:
    lead_id = str(lead["id"])
    if choice == "front_desk":
        status, reason = "closed_no_response", FRONT_DESK_REASON
    elif choice == "different_person":
        status, reason = "invalid_phone", "Different person: add the new patient as a new row in the Sheet"
    elif choice == "wrong_number":
        status, reason = "invalid_phone", "Wrong number confirmed by staff: add the right number as a new row"
    else:
        status, reason = "invalid_phone", "Phone number changed in the Sheet: add the new number as a new row"
    skip_remaining_planned(conn, lead_id, reason)
    # Steps the review paused stay stopped: nothing may bring them back on this lead.
    conn.execute(
        "update outreach_events set failure_reason=%s,updated_at=now() "
        "where lead_id=%s and status='skipped' and failure_reason=%s",
        (reason[:500], lead_id, PAUSE_SKIP_REASON),
    )
    conn.execute(
        "update leads set needs_review=false,review_reason=null,review_resolved_at=now(),"
        "status=%s,cadence_state='terminated',status_reason=%s,status_changed_at=now() where id=%s",
        (status, reason, lead_id),
    )


# --- restart from Day 0, or (demo) text the booking link ------------------------------------------

# Same window as a Sheet restart (lead_actions.RESTART_COOLDOWN_MINUTES): a second Day 0 call this
# soon is never what staff meant.
RESTART_COOLDOWN_MINUTES = 10


def last_review_restart(conn, lead_id: str):
    """When a review choice last restarted this lead within the cooldown (or None). Shared with the
    Sheet's Restart (lead_actions), so neither side starts a second Day 0 run within 10 minutes."""
    found = conn.execute(
        "select max(resolved_at) as at from lead_reviews where lead_id=%s and resolution=any(%s) "
        "and resolved_at>now()-make_interval(mins=>%s)",
        (lead_id, sorted(RESTART_CHOICES), RESTART_COOLDOWN_MINUTES),
    ).fetchone()
    return found["at"] if found else None


def _restart_guards(conn, lead: dict) -> None:
    lead_id = str(lead["id"])
    if closed := closed_for_good(conn, lead):
        raise ReviewError(409, closed)
    if lead.get("call_opt_out") and (lead.get("sms_opt_out") or lead.get("sms_disabled")):
        raise ReviewError(409, "This lead has opted out of calls and cannot be texted, so a new "
                               "cadence cannot run. Choose Front desk will handle.")
    if lead.get("phone_e164") and conn.execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],)
    ).fetchone():
        raise ReviewError(409, "This number is blocked. An admin can unblock it on this lead's page.")
    if conn.execute(
        "select 1 from outreach_events where lead_id=%s and status in ('in_flight','attempted') limit 1",
        (lead_id,),
    ).fetchone():
        raise ReviewError(409, "Wait for the current call or text result before restarting.")
    if conn.execute(
        "select 1 from lead_status_history where lead_id=%s "
        "and changed_at>now()-make_interval(mins=>%s) "
        "and source='n8n_sheet' and reason='cadence restarted from Google Sheets' limit 1",
        (lead_id, RESTART_COOLDOWN_MINUTES),
    ).fetchone() or last_review_restart(conn, lead_id):
        raise ReviewError(409, f"The cadence was already restarted in the last "
                               f"{RESTART_COOLDOWN_MINUTES} minutes. Try the restart again later.")


def _restart(conn, lead: dict) -> dict:
    """A fresh run from Day 0, the same as the board's New and the Sheet's Restart cadence."""
    from ..worker import materialize_cadence  # the worker imports services that import this module
    from .review import hand_over_number

    lead_id = str(lead["id"])
    conn.execute(
        "delete from outreach_events where lead_id=%s and status in ('planned','skipped')", (lead_id,)
    )
    conn.execute(
        "update leads set status='new',cadence_state='pending',needs_review=false,review_reason=null,"
        "review_flagged_at=null,review_resolved_at=now(),last_call_outcome=null,status_reason=null,"
        "callback_requested_at=null,callback_notes=null,call_attempts=0,status_changed_at=now() "
        "where id=%s",
        (lead_id,),
    )
    planned = materialize_cadence(conn, lead_id, lead["practice_id"], datetime.now(UTC).date())
    if not planned:
        raise ReviewError(409, "No active cadence is configured.")
    hand_over_number(conn, practice_id=lead["practice_id"], phone=lead["phone_e164"], lead_id=lead_id,
                     reason=RESTARTED_REASON)
    return {"planned_steps": planned}


def _booking_link_guards(conn, lead: dict) -> str:
    if lead.get("sms_opt_out") or lead.get("sms_disabled") or not lead.get("phone_e164"):
        raise ReviewError(409, "Texts are off for this lead (calls only or STOP), so the booking "
                               "link cannot be sent. Choose Front desk will handle.")
    if conn.execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],)
    ).fetchone():
        raise ReviewError(409, "This number is blocked. An admin can unblock it on this lead's page.")
    settings = conn.execute(
        "select booking_link_url from practice_settings where practice_id=%s", (lead["practice_id"],)
    ).fetchone()
    if not settings or not settings["booking_link_url"]:
        raise ReviewError(409, "The booking link is not configured (Administration).")
    return settings["booking_link_url"]


def _send_booking_link(conn, lead: dict, link: str) -> None:
    """Queue the booking-link text (delivery.process_pending_integrations sends it) and end the
    lead's outreach: booking_link_sent with nothing left to send, so no call follows it."""
    lead_id = str(lead["id"])
    skip_remaining_planned(conn, lead_id, "booking link texted after a review")
    conn.execute(
        "insert into notification_log(lead_id,notification_type,channel,status,payload,"
        "next_attempt_at) values(%s,'sms_booking_link','sms','queued',%s,now())",
        (lead_id, Jsonb({"booking_link_url": link, "source": "review"})),
    )
    conn.execute(
        "update leads set needs_review=false,review_reason=null,review_resolved_at=now(),"
        "status='booking_link_sent',cadence_state='completed',status_reason=%s,"
        "status_changed_at=now() where id=%s",
        ("booking link texted after a review", lead_id),
    )


LEAD_COLUMNS = (
    "id,practice_id,full_name,phone_e164,status,status_reason,cadence_state,needs_review,"
    "review_reason,review_flagged_at,call_opt_out,sms_opt_out,sms_disabled,is_test"
)


def resolve_review(
    conn, lead_id: str, choice: str, *, note: str | None, actor_id: str, actor_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one staff choice to a flagged lead. Any staff member or admin may do this."""
    now = now or datetime.now(UTC)
    lead = conn.execute(f"select {LEAD_COLUMNS} from leads where id=%s for update", (lead_id,)).fetchone()
    if not lead:
        raise ReviewError(404, "lead not found")
    rows = open_reviews(conn, lead_id)
    if not lead["needs_review"]:
        raise ReviewError(409, closed_review(conn, lead)["message"])
    rows = rows or [_legacy_row(lead)]
    primary = describe(rows[0], lead, more=len(rows) > 1)
    allowed = {option["choice"] for option in primary["options"]}
    if choice not in CHOICE_LABELS:
        raise ReviewError(422, "Unknown choice.")
    code = primary["code"]
    label = choice_label(choice, code)
    if choice not in allowed:
        if choice in RESTART_CHOICES and len(rows) > 1:
            raise ReviewError(409, "This lead has another open review. Choose a continue option "
                                   "here; the restart is offered on the last open review.")
        raise ReviewError(409, f"“{label}” is not an option for this review.")
    details = rows[0].get("details") or {}
    if choice in KEEP_OLD_NAME and not details.get("new_name"):
        details = _legacy_sheet_name(conn, lead, rows[0])
    note = (note or "").strip()[:500] or None
    result: dict[str, Any] = {}
    remaining = len(rows) - 1 if rows[0].get("id") is not None else 0
    if choice in RESUME_CHOICES and remaining:
        # Only this review is settled; its effect (new name, texts off) applies now, and the lead
        # stays paused for the next one, so no choice can undo another open problem.
        _apply_choice(conn, lead, rows, choice, details)
        # "Try texting again" is kept on the review and honoured when the lead finally resumes
        # (QA round 2: it was lost when another review was still open).
        conn.execute(
            "update lead_reviews set resolved_at=now(),resolved_by=%s,resolved_by_name=%s,"
            "resolution=%s,note=%s,details=details||%s,updated_at=now() where id=%s",
            (actor_id, actor_name[:120], choice, note,
             Jsonb({"retry_pending": True} if choice == "retry_texts" else {}), rows[0]["id"]),
        )
        conn.execute(
            "update leads set review_reason=%s where id=%s",
            (str(rows[1].get("message") or "Needs review")[:500], lead_id),
        )
        result = {"remaining_reviews": remaining}
    elif choice in RESUME_CHOICES:
        _resume_guards(conn, lead, choice)
        close_open_reviews(conn, lead_id, choice, by=actor_id, by_name=actor_name, note=note)
        _apply_choice(conn, lead, rows, choice, details)
        result = _resume(conn, lead, rows, choice, now)
    elif choice in RESTART_CHOICES:
        # Guards first: a refused restart (cooldown, call going out) changes nothing, not even
        # the name; the pop-up shows why and staff choose again.
        _restart_guards(conn, lead)
        close_open_reviews(conn, lead_id, choice, by=actor_id, by_name=actor_name, note=note)
        if choice in RESTART_BASE:
            _apply_choice(conn, lead, rows, RESTART_BASE[choice], details)
        result = _restart(conn, lead)
    elif choice == "send_booking_link":
        link = _booking_link_guards(conn, lead)
        close_open_reviews(conn, lead_id, choice, by=actor_id, by_name=actor_name, note=note)
        _send_booking_link(conn, lead, link)
    else:
        close_open_reviews(conn, lead_id, choice, by=actor_id, by_name=actor_name, note=note)
        _close(conn, lead, choice)
    new = conn.execute("select status,cadence_state from leads where id=%s", (lead_id,)).fetchone()
    record_status(
        conn, lead_id, lead["status"], new["status"], "dashboard",
        (f"Review resolved by {actor_name}: {label}" + (f". Note: {note}" if note else ""))[:500],
    )
    enqueue_sheet_update(
        conn, lead_id=lead_id,
        # With reviews left, the Sheet shows the next one ("Needs review: …").
        event_type="review_flagged" if result.get("remaining_reviews") else "review_resolved",
        source_key=f"{lead_id}:{rows[0].get('id')}:{choice}:{now.isoformat()}",
    )
    if choice in KEEP_OLD_NAME and details.get("new_name"):
        # The Sheet's Name cell still shows the new name; this update writes the kept one back.
        enqueue_sheet_update(conn, lead_id=lead_id, event_type="name_kept",
                             source_key=f"{lead_id}:{rows[0].get('id')}:{now.isoformat()}")
    if choice in KEEP_OLD_VALUE and details.get("new_value"):
        # The same for a kept location / case / date of birth (sheet_sync._kept_values).
        enqueue_sheet_update(conn, lead_id=lead_id, event_type="value_kept",
                             source_key=f"{lead_id}:{rows[0].get('id')}:{now.isoformat()}")
    return {
        "status": "resolved",
        "choice": choice,
        "label": label,
        "lead_status": new["status"],
        "cadence_state": new["cadence_state"],
        **result,
    }


# --- a phone number changed in the Sheet ---------------------------------------------------------


def close_for_phone_change(
    conn, lead: dict, *, new_phone: str, new_name: str | None, now: datetime | None = None,
) -> bool:
    """The Sheet row now holds another number: this lead is over (owner, 2026-10-22).

    Not a review: the old lead closes at once and can never be resumed or restarted (the number it
    calls is no longer the patient's), and staff add the new number as a new row. A booked,
    declined or Do Not Contact lead keeps its outcome; the change is still recorded.
    Returns False when it was already recorded for this number (a later profile sync).
    """
    now = now or datetime.now(UTC)
    lead_id = str(lead["id"])
    old_mask, new_mask = mask_phone(lead["phone_e164"]), mask_phone(new_phone)
    seen = conn.execute(
        "select 1 from lead_reviews where lead_id=%s and code='phone_changed' "
        "and details->>'new_phone'=%s limit 1",
        (lead_id, new_mask),
    ).fetchone()
    if seen:
        return False
    tz, _, _ = _practice_clock(conn, lead_id)
    # No digits in the words (owner, 2026-10-26): the masks stay in details.
    message = (f"Phone number changed in the Sheet on {_clinic_date(now, tz)}. "
               "Add the new number as a new row.")
    details: dict[str, Any] = {"old_phone": old_mask, "new_phone": new_mask}
    if new_name and new_name.strip() != str(lead.get("full_name") or "").strip():
        details.update(old_name=lead.get("full_name"), new_name=new_name.strip())
    finished = lead["status"] in {"booked", "declined", "do_not_contact"}
    conn.execute(
        "insert into lead_reviews(lead_id,code,message,details,source,resolved_at,resolved_by,"
        "resolved_by_name,resolution) values(%s,'phone_changed',%s,%s,'sheet',now(),'sheet',"
        "'Google Sheet','close_phone_changed')",
        (lead_id, message[:500], Jsonb(details)),
    )
    if finished:
        record_status(conn, lead_id, lead["status"], lead["status"], "n8n_sheet", message)
        return True
    # Any open review is settled by this: nothing is left to decide on this lead.
    close_open_reviews(conn, lead_id, "close_phone_changed", by="sheet", by_name="Google Sheet")
    skip_remaining_planned(conn, lead_id, "phone number changed in the Sheet")
    conn.execute(
        "update outreach_events set failure_reason='phone number changed in the Sheet',updated_at=now() "
        "where lead_id=%s and status='skipped' and failure_reason=%s",
        (lead_id, PAUSE_SKIP_REASON),
    )
    conn.execute(
        "update leads set status='invalid_phone',cadence_state='terminated',needs_review=false,"
        "review_reason=null,review_resolved_at=case when needs_review then now() else review_resolved_at end,"
        "status_reason=%s,status_changed_at=now() where id=%s",
        (message[:500], lead_id),
    )
    record_status(conn, lead_id, lead["status"], "invalid_phone", "n8n_sheet", message)
    return True


PHONE_CHANGED_DETAIL = (
    "The phone number changed in the Google Sheet, so this lead cannot be restarted or resumed on "
    "the old number. Add the new number as a new row in the Sheet."
)


def closed_for_good(conn, lead: dict) -> str | None:
    """Why a lead may never be resumed or restarted, or None.

    Closed because its number (or person) is wrong, or its phone changed in the Sheet. QA
    2026-10-22: a Booked lead whose phone changed kept its status (it is still Booked for staff),
    and the board's New restarted it on the OLD number. The phone change record now blocks that,
    whatever the status.
    """
    # Any phone-change record, however it was settled: an old (pre-048) one closed with Front desk
    # or a board move blocked nothing (QA round 2).
    phone_changed = conn.execute(
        "select 1 from lead_reviews where lead_id=%s and code='phone_changed' limit 1",
        (lead["id"],),
    ).fetchone()
    if phone_changed:
        return PHONE_CHANGED_DETAIL
    if lead.get("status") == "invalid_phone" and lead.get("cadence_state") == "terminated":
        return CLOSED_FOR_GOOD_DETAIL
    return None


CLOSED_FOR_GOOD_DETAIL = (
    "This lead is closed for good (wrong number, different person or phone changed). "
    "Add the patient as a new row in the Google Sheet."
)
OPEN_REVIEW_DETAIL = (
    "This lead has an open review. Choose what happens next with Resolve review."
)
