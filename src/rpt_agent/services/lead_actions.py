from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import transaction
from ..worker import format_phone, materialize_cadence, next_due_soon
from .lead_review import (
    DETAIL_FIELDS,
    FRONT_DESK_REASON,
    KEEP_OLD_VALUE,
    close_for_phone_change,
    close_open_reviews,
    closed_for_good,
    last_review_restart,
)
from .lead_status import record_status
from .review import REPLACED_REASON, flag_lead_for_review, hand_over_number
from .sheet_sync import enqueue_sheet_update

TERMINAL_START_STATUSES = {
    "booked",
    "declined",
    "transferred_human",
    "booking_link_sent",
    "do_not_contact",
    "invalid_phone",
    "closed_no_response",
}
RESTART_COOLDOWN_MINUTES = 10
NUMBER_BLOCKED_REASON = "Number blocked: this number asked not to be contacted. Unblock it to start outreach."
# Restart from the Google Sheet (direct mode, `sheet_rules`), owner 2026-10-25: the Sheet never
# undoes a decision made on the dashboard. A lead in review is restarted from its review card; a
# booked or closed lead is reopened on the dashboard. (n8n mode keeps the old rules.)
RESTART_REVIEW_DETAIL = "This lead needs review. Resolve it on the dashboard; you can restart it from there."
RESTART_BOOKED_DETAIL = "This lead is booked. To start again, cancel the visit in Stride, or add a new row."
RESTART_CLOSED_DETAIL = "This lead is closed ({why}). Use the dashboard."


@dataclass(frozen=True)
class ActionExecution:
    status_code: int
    body: dict[str, Any]


def _hold_if_number_blocked(conn, lead_id: str, phone: str | None) -> bool:
    """A start or restart on a blocked number must not look like a running cadence.

    The worker never contacts a blocked number, so without this the lead sat "In cadence" with
    nothing going out (seen 2026-10-16: a lead re-added after its number said "don't call me").
    It goes to Needs Attention instead, where an admin can unblock it and continue.
    """
    if not phone or not conn.execute(
        "select 1 from suppressed_numbers where phone_e164=%s", (phone,)
    ).fetchone():
        return False
    flag_lead_for_review(conn, lead_id, NUMBER_BLOCKED_REASON, code="number_blocked", source="sheet")
    return True


class LeadActionError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str, *, lead_id: str | None = None,
                 extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.lead_id = lead_id
        self.extra = extra or {}  # more fields for the stored response (e.g. retry_at)


def _request_hash(
    action: str,
    lead_id: UUID | None,
    lead: dict[str, Any],
    *,
    require_new: bool = False,
    replaces_lead_id: UUID | None = None,
) -> str:
    request: dict[str, Any] = {
        "action": action,
        "lead_id": str(lead_id) if lead_id else None,
        "lead": lead,
    }
    if require_new:
        request["require_new"] = True
    if replaces_lead_id:
        request["replaces_lead_id"] = str(replaces_lead_id)
    canonical = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _name_parts(full_name: str) -> tuple[str, str | None]:
    parts = full_name.split()
    if not parts:
        raise LeadActionError(422, "invalid_name", "Name is required")
    return parts[0], " ".join(parts[1:]) or None


def _response_envelope(http_status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Persist status inside jsonb because live lead_action_requests has no http_status column."""
    return {"http_status": http_status, "body": body}


def _replay_stored_response(stored: Any) -> ActionExecution | None:
    if not isinstance(stored, dict):
        return None
    if "http_status" in stored and "body" in stored and isinstance(stored["body"], dict):
        return ActionExecution(int(stored["http_status"]), dict(stored["body"]))
    return None


def _complete_request(
    conn,
    request_id: UUID,
    *,
    lead_id: str | None,
    status: str,
    http_status: int,
    body: dict[str, Any],
    error_category: str | None = None,
) -> None:
    del error_category  # live schema stores failure detail inside response_body only
    conn.execute(
        "update lead_action_requests set lead_id=%s,status=%s,response_body=%s,"
        "completed_at=now() where request_id=%s",
        (lead_id, status, Jsonb(_response_envelope(http_status, body)), str(request_id)),
    )


def _update_lead_profile(conn, lead_id: str | UUID, lead_data: dict[str, Any]) -> None:
    """Refresh Sheet-owned lead details without changing phone identity or consent."""
    full_name = str(lead_data.get("full_name") or "").strip() or None
    first_name: str | None = None
    last_name: str | None = None
    if full_name:
        first_name, last_name = _name_parts(full_name)
    lead_type = str(
        lead_data.get("lead_type") or lead_data.get("title") or ""
    ).strip() or None
    email = str(lead_data.get("email") or "").strip().lower() or None
    location = str(lead_data.get("location") or "").strip() or None
    gender = lead_data.get("gender") or None
    conn.execute(
        "update leads set full_name=coalesce(%s,full_name),"
        "first_name=coalesce(%s,first_name),"
        "last_name=case when %s::text is not null then %s else last_name end,"
        "email=coalesce(%s,email),date_of_birth=coalesce(%s,date_of_birth),"
        "location=coalesce(%s,location),lead_type=coalesce(%s,lead_type),gender=coalesce(%s,gender),"
        "updated_at=now() where id=%s",
        (
            full_name,
            first_name,
            full_name,
            last_name,
            email,
            lead_data.get("date_of_birth"),
            location,
            lead_type,
            gender,
            lead_id,
        ),
    )


def _case_key(conn, practice_id: int, text: Any) -> str:
    """What a Sheet Case means: the case type it names (by name or alias, else a unique partial
    match, as the booking engine reads it), else the text itself. No default fallback here: an
    unknown case ("Knee") is a different case from "Ortho", so the patient is called, not skipped.
    """
    wanted = " ".join(str(text or "").lower().split())
    if not wanted:
        return ""
    found = conn.execute(
        "select id from (select ct.id,case "
        " when lower(ct.name)=%(w)s or %(w)s=any(select lower(a) from unnest(ct.aliases) a) then 0 "
        " when strpos(lower(ct.name),%(w)s)>0 and (select count(*) from case_types o "
        "  where o.practice_id=ct.practice_id and o.is_active and strpos(lower(o.name),%(w)s)>0)=1 "
        " then 1 end as rank from case_types ct where ct.practice_id=%(p)s and ct.is_active) ranked "
        "where rank is not null order by rank limit 1",
        {"w": wanted, "p": practice_id},
    ).fetchone()
    return f"case:{found['id']}" if found else f"text:{wanted}"


def _same_person_already_booked(conn, practice_id: int, phone: str, lead_data: dict[str, Any]):
    """The same patient (phone + name + birthday) already booked for the SAME case is not called again.

    A repeat patient referred for another body region shares the number but is a new referral, and
    starts a new lead (hand_over_number). Only an exact repeat of a booked person is refused: seen in
    testing, a booked patient added to the Sheet again got a fresh cadence and was called.
    "Booked" means a visit still ahead (ours or Stride's), or a booking made in the last 30 days when
    no visit is on record (staff picked Booked in the Sheet).
    The case must match too (client, 2026-10-15): a patient booked for Ortho who comes back for
    Pelvic must be reached. Cases match by meaning ("Ortho" = "Physical Therapy" via aliases).
    """
    name = " ".join(str(lead_data.get("full_name") or "").lower().split())
    booked = conn.execute(
        "select l.id,l.lead_type from leads l where l.practice_id=%s and l.phone_e164=%s "
        "and l.status='booked' "
        "and lower(regexp_replace(trim(l.full_name),'\\s+',' ','g'))=%s "
        "and (l.date_of_birth is null or %s::date is null or l.date_of_birth=%s::date) "
        "and (exists(select 1 from appointments a where a.lead_id=l.id and a.start_utc>now() "
        "  and coalesce(a.state,'') not in ('cancelled','failed')) "
        " or exists(select 1 from stride_patients p join stride_appointments s on s.practice_id=p.practice_id "
        "  and s.patient_stride_id=p.stride_id and s.deleted_at is null and s.start_utc>now() "
        "  and lower(coalesce(s.status,'')) not in ('cancel','cancelled','late cancel','no show','a','l','n') "
        "  where p.lead_id=l.id) "
        " or coalesce(l.status_changed_at,l.updated_at)>now()-interval '30 days') "
        "order by l.status_changed_at desc",
        (practice_id, phone, name, lead_data.get("date_of_birth"), lead_data.get("date_of_birth")),
    ).fetchall()
    if not booked:
        return None
    wanted = _case_key(conn, practice_id, lead_data.get("lead_type") or lead_data.get("title"))
    for lead in booked:
        if _case_key(conn, practice_id, lead["lead_type"]) == wanted:
            return lead
    return None


def _start_cadence(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_data: dict[str, Any],
    require_new: bool = False,
    replaces_lead_id: UUID | None = None,
) -> ActionExecution:
    phone = format_phone(lead_data.get("phone"))
    if not phone:
        raise LeadActionError(422, "invalid_phone", "Phone number is not valid")
    conn.execute(
        "select pg_advisory_xact_lock(hashtext(%s))",
        (f"sheet-lead:{practice['id']}:{phone}",),
    )
    replaced_lead = None
    if replaces_lead_id:
        replaced_lead = conn.execute(
            "select id,status,phone_e164 from leads where id=%s and practice_id=%s for update",
            (replaces_lead_id, practice["id"]),
        ).fetchone()
        if not replaced_lead:
            raise LeadActionError(404, "lead_not_found", "Lead was not found")
        if replaced_lead["phone_e164"] == phone:
            raise LeadActionError(
                409,
                "phone_unchanged",
                "The phone number has not changed",
                lead_id=str(replaces_lead_id),
            )
    if not replaces_lead_id:
        booked = _same_person_already_booked(conn, practice["id"], phone, lead_data)
        if booked:
            # No lead_id: the Sheet writes it into the row, and two rows with one Lead ID break every
            # later Sheet update for that lead (the webhook needs exactly one matching row).
            return ActionExecution(200, {
                "request_id": str(request_id), "lead_id": None, "booked_lead_id": str(booked["id"]),
                "action": "start_cadence", "result": "already_booked", "created": False,
                "cadence_event_count": 0,
            })
    first_name, last_name = _name_parts(str(lead_data["full_name"]).strip())
    stored_lead_type = str(
        lead_data.get("lead_type") or lead_data.get("title") or ""
    ).strip() or None
    # Same rule as dashboard intake: while the deployment runs in test mode
    # every new lead is a test lead (accelerated cadence, no calling-hours
    # gate). Flipping TEST_MODE off makes Sheet leads real patients again.
    settings = get_settings()
    synthetic = settings.test_mode and settings.app_env.lower() in {"development", "test"}
    inserted = conn.execute(
        "insert into leads(practice_id,source_system,first_name,last_name,full_name,phone_e164,"
        "phone_original,email,date_of_birth,timezone,line_type,consent_captured_at,"
        "consent_source,consent_reference,consent_text_version,status,cadence_state,"
        "lead_type,location,is_test,gender) "
        "values(%s,'google_sheets',%s,%s,%s,%s,%s,%s,%s,%s,'unknown',"
        "now(),'dashboard_staff_attestation',%s,'google-sheet-staff-attestation-v1',"
        "'new','pending',%s,%s,%s,%s) returning id",
        (
            practice["id"],
            first_name,
            last_name,
            str(lead_data["full_name"]).strip(),
            phone,
            lead_data["phone"],
            str(lead_data["email"]).strip().lower() if lead_data.get("email") else None,
            lead_data["date_of_birth"],
            practice["timezone"],
            f"n8n:{request_id}",
            stored_lead_type,
            str(lead_data["location"]).strip(),
            synthetic,
            lead_data.get("gender") or None,
        ),
    ).fetchone()
    lead_id = str(inserted["id"])
    previous_status = "new"
    created = True

    event_count = materialize_cadence(
        conn, lead_id, practice["id"], datetime.now(UTC).date()
    )
    if not event_count:
        raise LeadActionError(
            409,
            "cadence_not_configured",
            "No active cadence is configured",
            lead_id=lead_id,
        )
    if replaced_lead:
        reason = f"Replaced by Sheet lead {lead_id} after phone number change"
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            (reason, replaces_lead_id),
        )
        conn.execute(
            "update leads set status='needs_attention',cadence_state='terminated',"
            "needs_review=true,review_reason=%s,review_flagged_at=now(),"
            "status_reason=%s,status_changed_at=now(),updated_at=now() where id=%s",
            (reason, reason, replaces_lead_id),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'needs_attention','n8n_sheet',%s)",
            (replaces_lead_id, replaced_lead["status"], reason),
        )
    conn.execute(
        "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
        "values(%s,%s,'in_progress','n8n_sheet','cadence started from Google Sheets')",
        (lead_id, previous_status),
    )
    warning = hand_over_number(conn, practice_id=practice["id"], phone=phone, lead_id=lead_id)
    blocked = _hold_if_number_blocked(conn, lead_id, phone)
    enqueue_sheet_update(
        conn,
        lead_id=lead_id,
        event_type="lead_link_ready",
        source_key=str(request_id),
    )
    # Intake/recovery owns Lead ID + Action Status; this snapshot only adds the link.
    # The AWS webhook omits those intake-owned fields, so it cannot overwrite them.
    return ActionExecution(
        201 if created else 200,
        {
            "request_id": str(request_id),
            "lead_id": lead_id,
            "action": "start_cadence",
            "result": "number_blocked_needs_review" if blocked else "cadence_started",
            "created": created,
            "cadence_event_count": event_count,
            **({"warning": warning} if warning else {}),
        },
    )


def _locked_lead(conn, practice_id: int, lead_id: UUID, phone: str) -> dict[str, Any]:
    normalized = format_phone(phone)
    if not normalized:
        raise LeadActionError(422, "invalid_phone", "Phone number is not valid")
    lead = conn.execute(
        "select id,practice_id,status,cadence_state,call_opt_out,sms_opt_out,phone_e164,"
        "status_reason,needs_review from leads where id=%s and practice_id=%s for update",
        (lead_id, practice_id),
    ).fetchone()
    if not lead:
        raise LeadActionError(404, "lead_not_found", "Lead was not found")
    if lead["phone_e164"] != normalized:
        raise LeadActionError(
            409,
            "lead_phone_mismatch",
            "Lead ID and phone number do not match",
            lead_id=str(lead_id),
        )
    return lead


def _closed_by_decision(lead: dict[str, Any]) -> str | None:
    """Why a lead was closed by someone's decision (staff words), or None.

    Front desk, a newer row on the number, "not interested" (a call outcome, or staff moving the card
    to Closed on the board). Booked, Do Not Contact and a wrong / changed number are refused with
    their own reasons. A cadence that only ran out ("Closed, no response", booking link sent,
    transferred) is not a decision: restarting it from the Sheet is how staff try the patient again
    (2026-10-20).
    """
    reason = str(lead.get("status_reason") or "")
    if reason == FRONT_DESK_REASON:
        return "handled by front desk"
    if reason == REPLACED_REASON:
        return "a newer row has this phone number"
    if lead["status"] == "declined":
        return "closed on the dashboard" if reason == "closed from the board" else "not interested"
    return None


def _sheet_review_refusal(conn, lead: dict[str, Any]) -> LeadActionError | None:
    """A Sheet Restart on a lead in review (owner, 2026-10-25): refused, restart it from the card.

    Read from the database under the lead lock, not from the row: a cleared Outcome Status must
    not let a Restart past a review (tester 2026-10-23). It no longer waits in Action. Checked
    before the status rules, so a wrong-number review says "needs review", not "invalid phone".
    """
    lead_id = str(lead["id"])
    open_review = lead.get("needs_review") or conn.execute(
        "select 1 from lead_reviews where lead_id=%s and resolved_at is null limit 1", (lead_id,)
    ).fetchone()
    if open_review:
        return LeadActionError(409, "restart_needs_review", RESTART_REVIEW_DETAIL, lead_id=lead_id)
    return None


def _sheet_closed_refusal(lead: dict[str, Any]) -> LeadActionError | None:
    """A Sheet Restart on a booked lead or one closed by a decision (owner, 2026-10-25)."""
    lead_id = str(lead["id"])
    if lead["status"] == "booked":
        return LeadActionError(409, "restart_booked", RESTART_BOOKED_DETAIL, lead_id=lead_id)
    if why := _closed_by_decision(lead):
        return LeadActionError(409, "restart_closed", RESTART_CLOSED_DETAIL.format(why=why),
                               lead_id=lead_id, extra={"closed_reason": why})
    return None


def _restart_cadence(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_id: UUID,
    phone: str,
    lead_data: dict[str, Any] | None = None,
    sheet_rules: bool = False,
) -> ActionExecution:
    lead = _locked_lead(conn, practice["id"], lead_id, phone)
    # A lead whose phone changed in the Sheet is never restarted on its old number (even if staff
    # put the old number back in the row, or the lead was Booked).
    if closed := closed_for_good(conn, lead):
        raise LeadActionError(409, "restart_not_allowed", closed, lead_id=str(lead_id))
    if sheet_rules and (refusal := _sheet_review_refusal(conn, lead)):
        raise refusal
    if lead["status"] in {"do_not_contact", "invalid_phone"}:
        raise LeadActionError(
            409,
            "restart_not_allowed",
            "An invalid phone lead cannot be restarted" if lead["status"] == "invalid_phone"
            else "A Do Not Contact lead cannot be restarted",
            lead_id=str(lead_id),
        )
    # n8n mode only: Booked may be restarted there (a mis-clicked Booked needs an undo, and marking
    # Booked again is the undo for a mis-clicked restart). The direct Sheet refuses a Restart on a
    # booked, closed or in-review lead (owner, 2026-10-25): the dashboard is the place to undo.
    if sheet_rules and (refusal := _sheet_closed_refusal(lead)):
        raise refusal
    was_booked = lead["status"] == "booked"
    if lead["call_opt_out"] and lead["sms_opt_out"]:
        raise LeadActionError(
            409, "all_channels_blocked", "This lead opted out of every channel", lead_id=str(lead_id)
        )
    unresolved = conn.execute(
        "select exists(select 1 from outreach_events where lead_id=%s "
        "and status in ('in_flight','attempted')) as unresolved",
        (lead_id,),
    ).fetchone()
    if unresolved["unresolved"]:
        raise LeadActionError(
            409,
            "outreach_unresolved",
            "Wait for the current call or SMS result before restarting",
            lead_id=str(lead_id),
        )
    # Each restart is a fresh Day 0 call. If the Sheet workflow re-fires the same
    # Restart command after our own status writeback, every request arrives with
    # a new request ID, so idempotency cannot catch it and the patient is called
    # every minute or two. A real second restart this soon is not plausible.
    recent = conn.execute(
        "select changed_at as at from lead_status_history "
        "where lead_id=%s and source='n8n_sheet' and reason='cadence restarted from Google Sheets' "
        "and changed_at>now()-make_interval(mins=>%s) order by changed_at desc limit 1",
        (lead_id, RESTART_COOLDOWN_MINUTES),
    ).fetchone()
    last = recent["at"] if recent else None
    if sheet_rules and (by_review := last_review_restart(conn, str(lead_id))):
        # A restart chosen on the dashboard's review card counts too (10 minutes for everyone).
        last = max(last, by_review) if last else by_review
    if last:
        # retry_at: when a new Restart is accepted, so the Sheet can say when to pick it again
        # (owner, 2026-10-25: Action empties after each command, staff re-pick it).
        raise LeadActionError(
            409,
            "restart_too_soon",
            f"Cadence was already restarted in the last {RESTART_COOLDOWN_MINUTES} minutes",
            lead_id=str(lead_id),
            extra={"retry_at": (last + timedelta(minutes=RESTART_COOLDOWN_MINUTES)).isoformat()},
        )
    # A call or text about to go out (owner, 2026-10-26): restarting now could ring the patient
    # twice (the old step and the new Day 0). retry_at = due time + the window.
    if due_retry := next_due_soon(conn, str(lead_id)):
        raise LeadActionError(
            409,
            "outreach_due_soon",
            "A call or text is due in the next few minutes; restart after it goes out",
            lead_id=str(lead_id),
            extra={"retry_at": due_retry.isoformat()},
        )
    if lead_data:
        _update_lead_profile(conn, lead_id, lead_data)

    # Restart always creates a fresh run from Day 0, including when the old
    # cadence was paused. Completed history remains intact; only unfinished
    # planned/skipped rows are replaced.
    conn.execute(
        "delete from outreach_events where lead_id=%s and status in ('planned','skipped')",
        (lead_id,),
    )
    # A restart settles an open review; the record says who decided it.
    close_open_reviews(conn, str(lead_id), "restarted", by="sheet", by_name="Google Sheet")
    conn.execute(
        "update leads set status='new',cadence_state='pending',needs_review=false,"
        "review_reason=null,review_flagged_at=null,last_call_outcome=null,status_reason=null,"
        "callback_requested_at=null,callback_notes=null,call_attempts=0,status_changed_at=now() "
        "where id=%s",
        (lead_id,),
    )
    event_count = materialize_cadence(
        conn, str(lead_id), practice["id"], datetime.now(UTC).date()
    )
    if not event_count:
        raise LeadActionError(
            409,
            "cadence_not_configured",
            "No active cadence is configured",
            lead_id=str(lead_id),
        )
    conn.execute(
        "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
        "values(%s,%s,'in_progress','n8n_sheet','cadence restarted from Google Sheets')",
        (lead_id, lead["status"]),
    )
    warning = hand_over_number(
        conn, practice_id=practice["id"], phone=lead["phone_e164"], lead_id=str(lead_id)
    )
    blocked = _hold_if_number_blocked(conn, str(lead_id), lead["phone_e164"])
    enqueue_sheet_update(
        conn,
        lead_id=str(lead_id),
        event_type="lead_link_ready",
        source_key=str(request_id),
    )
    # Intake/recovery writes Lead ID + Action Status for restart.
    return ActionExecution(
        200,
        {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "action": "restart_cadence",
            "result": "number_blocked_needs_review" if blocked else "cadence_restarted",
            "created": False,
            "cadence_event_count": event_count,
            **({"warning": warning} if warning else {}),
            **({"was_booked": True} if was_booked else {}),
        },
    )


def _do_not_contact(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_id: UUID,
    phone: str,
) -> ActionExecution:
    lead = _locked_lead(conn, practice["id"], lead_id, phone)
    if lead["status"] != "do_not_contact":
        close_open_reviews(conn, str(lead_id), "do_not_contact", by="sheet", by_name="Google Sheet")
        conn.execute(
            "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
            "needs_review=false,review_reason=null,review_resolved_at=case when needs_review then now() else review_resolved_at end,"
            "cadence_state='terminated',last_call_outcome='do_not_contact',"
            "status_reason='staff selected Do Not Contact in Google Sheets',"
            "status_changed_at=now() where id=%s",
            (lead_id,),
        )
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            ("Do Not Contact selected in Google Sheets", lead_id),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'do_not_contact','n8n_sheet','Do Not Contact selected in Google Sheets')",
            (lead_id, lead["status"]),
        )
    else:
        # Already Do Not Contact, but an Administration unblock may have released the number (and
        # its opt-outs) while the lead stayed closed: block it again (owner, 2026-10-26).
        conn.execute(
            "update leads set call_opt_out=true,sms_opt_out=true where id=%s", (lead_id,)
        )
    # Do Not Contact always blocks the number, whatever the lead's status was (idempotent).
    conn.execute(
        "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
        "values(%s,'staff selected Do Not Contact','n8n_sheet','internal') "
        "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
        "last_verified_at=now()",
        (lead["phone_e164"],),
    )
    # Intake/recovery writes Action Status for the Sheet DNC command.
    return ActionExecution(
        200,
        {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "action": "do_not_contact",
            "result": "do_not_contact_applied",
            "created": False,
            "cadence_event_count": 0,
        },
    )


def _mark_booked(
    conn,
    *,
    request_id: UUID,
    practice: dict[str, Any],
    lead_id: UUID,
    phone: str,
) -> ActionExecution:
    """Accept the Sheet team's manual booking decision without inventing an appointment."""
    lead = _locked_lead(conn, practice["id"], lead_id, phone)
    if lead["status"] != "booked":
        close_open_reviews(conn, str(lead_id), "booked", by="sheet", by_name="Google Sheet")
        conn.execute(
            "update leads set status='booked',cadence_state='completed',needs_review=false,"
            "review_reason=null,review_resolved_at=case when needs_review then now() "
            "else review_resolved_at end,status_reason=%s,status_changed_at=now() where id=%s",
            ("staff marked Booked in Google Sheets", lead_id),
        )
        conn.execute(
            "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
            "where lead_id=%s and status='planned'",
            ("Booked selected in Google Sheets", lead_id),
        )
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,%s,'booked','n8n_sheet','Booked selected in Google Sheets')",
            (lead_id, lead["status"]),
        )
    enqueue_sheet_update(
        conn,
        lead_id=str(lead_id),
        event_type="lead_booked",
        source_key=str(request_id),
    )
    return ActionExecution(
        200,
        {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "action": "booked",
            "result": "booked_applied",
            "created": False,
            "cadence_event_count": 0,
        },
    )


def execute_lead_action(
    *,
    request_id: UUID,
    action: str,
    lead_id: UUID | None,
    lead: dict[str, Any],
    require_new: bool = False,
    replaces_lead_id: UUID | None = None,
    sheet_rules: bool = False,
) -> ActionExecution:
    """Apply one signed n8n command with a durable, replayable response.

    `sheet_rules`: the direct Sheet's rules (owner, 2026-10-25; sheet_direct passes it): a Restart
    on a lead in review, booked or closed by a decision is refused, and a restart chosen on the
    dashboard's review card counts for the cooldown. The n8n endpoint leaves it off (n8n mode
    unchanged).
    """
    request_hash = _request_hash(
        action,
        lead_id,
        lead,
        require_new=require_new,
        replaces_lead_id=replaces_lead_id,
    )
    with transaction() as conn:
        practice = conn.execute(
            "select id,timezone from practices where slug=%s and is_active=true",
            (get_settings().n8n_practice_slug,),
        ).fetchone()
        if not practice:
            return ActionExecution(503, {"detail": "Configured practice is not available"})

        inserted = conn.execute(
            "insert into lead_action_requests(request_id,practice_id,action,request_hash,status) "
            "values(%s,%s,%s,%s,'processing') on conflict(request_id) do nothing "
            "returning request_id",
            (str(request_id), str(practice["id"]), action, request_hash),
        ).fetchone()
        if not inserted:
            existing = conn.execute(
                "select practice_id,action,request_hash,status,response_body "
                "from lead_action_requests where request_id=%s for update",
                (str(request_id),),
            ).fetchone()
            if (
                not existing
                or str(existing["practice_id"]) != str(practice["id"])
                or existing["action"] != action
                or existing["request_hash"] != request_hash
            ):
                return ActionExecution(
                    409, {"request_id": str(request_id), "detail": "Request ID was reused"}
                )
            if existing["status"] in {"completed", "failed"} and existing["response_body"]:
                replayed = _replay_stored_response(existing["response_body"])
                if replayed is not None:
                    return replayed
            return ActionExecution(
                409,
                {"request_id": str(request_id), "detail": "Request is still processing"},
            )

        try:
            with conn.transaction():
                if action == "start_cadence":
                    result = _start_cadence(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_data=lead,
                        require_new=require_new,
                        replaces_lead_id=replaces_lead_id,
                    )
                elif action == "restart_cadence":
                    if lead_id is None:
                        raise LeadActionError(422, "lead_id_required", "Lead ID is required")
                    result = _restart_cadence(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_id=lead_id,
                        phone=str(lead.get("phone") or ""),
                        lead_data=lead,
                        sheet_rules=sheet_rules,
                    )
                elif action == "do_not_contact":
                    if lead_id is None:
                        raise LeadActionError(422, "lead_id_required", "Lead ID is required")
                    result = _do_not_contact(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_id=lead_id,
                        phone=str(lead.get("phone") or ""),
                    )
                elif action == "booked":
                    if lead_id is None:
                        raise LeadActionError(422, "lead_id_required", "Lead ID is required")
                    result = _mark_booked(
                        conn,
                        request_id=request_id,
                        practice=practice,
                        lead_id=lead_id,
                        phone=str(lead.get("phone") or ""),
                    )
                else:
                    raise LeadActionError(422, "invalid_action", "Action is not supported")
        except LeadActionError as exc:
            persisted_lead_id = None
            if exc.lead_id:
                persisted = conn.execute(
                    "select id from leads where id=%s",
                    (exc.lead_id,),
                ).fetchone()
                persisted_lead_id = str(persisted["id"]) if persisted else None
            body = {
                "request_id": str(request_id),
                "lead_id": persisted_lead_id,
                "action": action,
                "code": exc.code,
                "detail": exc.detail,
                **exc.extra,
            }
            _complete_request(
                conn,
                request_id,
                lead_id=persisted_lead_id,
                status="failed",
                http_status=exc.status_code,
                body=body,
                error_category=exc.code,
            )
            return ActionExecution(exc.status_code, body)

        _complete_request(
            conn,
            request_id,
            lead_id=result.body.get("lead_id"),
            status="completed",
            http_status=result.status_code,
            body=result.body,
        )
        return result


def _same_name(left: Any, right: Any) -> bool:
    return " ".join(str(left or "").split()).casefold() == " ".join(str(right or "").split()).casefold()


def _us_date(value: Any) -> str:
    """A date as Sheet staff type it (MM/DD/YYYY, sheet_direct.normalize_dob reads it back)."""
    return value.strftime("%m/%d/%Y") if hasattr(value, "strftime") else str(value or "")


def _detail_changes(current: dict[str, Any], lead: dict[str, Any]) -> list[dict[str, Any]]:
    """Location / case / date of birth that differ from the lead's (owner, 2026-10-25: reviewed).

    A blank cell is no change (it never erased a value); a lead with no value yet just takes it.
    """
    changes = []
    for code, (column, _, field) in DETAIL_FIELDS.items():
        if column == "date_of_birth":
            new, old = lead.get("date_of_birth"), current.get("date_of_birth")
            if not new or not old or new == old:
                continue
            changes.append({"code": code, "column": column, "field": field,
                            "old_value": _us_date(old), "new_value": _us_date(new),
                            "new_raw": new.isoformat() if hasattr(new, "isoformat") else str(new)})
            continue
        incoming = lead.get("location") if column == "location" else (
            lead.get("lead_type") or lead.get("title"))
        new = " ".join(str(incoming or "").split())
        old = " ".join(str(current.get(column) or "").split())
        if not new or not old or _same_name(new, old):
            continue
        changes.append({"code": code, "column": column, "field": field,
                        "old_value": old, "new_value": new})
    return changes


def _kept_value(conn, lead_id: str, code: str) -> str | None:
    """The Sheet value staff last chose to keep the old one over ("Keep old ..."), until spent.

    Like `leads.sheet_name_accepted` for the Name, without a new column: the resolution itself is
    the record. Spent (`kept_spent`) once the row shows the value on file again (write-back landed,
    or staff typed it back), so the same new value typed later opens a new review.
    """
    found = conn.execute(
        "select details->>'new_value' as value from lead_reviews where lead_id=%s and code=%s "
        "and resolution=any(%s) and not (details ? 'kept_spent') "
        "order by resolved_at desc,id desc limit 1",
        (lead_id, code, sorted(KEEP_OLD_VALUE)),
    ).fetchone()
    return found["value"] if found else None


def spend_kept_values(conn, lead_id: str, codes: list[str] | None = None) -> None:
    """The row shows the kept value again: a "keep old" no longer accepts the reviewed new value."""
    conn.execute(
        "update lead_reviews set details=details||'{\"kept_spent\":true}'::jsonb,updated_at=now() "
        "where lead_id=%s and code=any(%s) and resolution=any(%s) and not (details ? 'kept_spent')",
        (lead_id, codes or sorted(DETAIL_FIELDS), sorted(KEEP_OLD_VALUE)),
    )


EMAIL_CHANGED = "Email changed in Google Sheet"
# Lead column -> the keys of the Sheet's lead data that carry it (_update_lead_profile).
_PROFILE_KEYS = {"location": ("location",), "lead_type": ("title", "lead_type"),
                 "date_of_birth": ("date_of_birth",)}


def sync_sheet_lead(
    *, request_id: UUID, lead_id: UUID, lead: dict[str, Any], sheet_rules: bool = False,
) -> ActionExecution:
    """Update one linked Sheet lead; a changed name needs review, a changed phone closes it.

    2026-10-21: a Name edited in the Sheet flagged the lead "needs review" with nothing saying
    what changed. Now the review records the old and new name, and staff choose: use the new
    name, keep the old one (that Sheet value is then accepted and never flags again), or close.
    A new phone number is a new lead: this one closes at once (owner, 2026-10-22).

    `sheet_rules` (direct Sheet, owner 2026-10-25): Location, Case and Date Of Birth are reviewed
    like the Name (old -> new, the lead keeps its value until staff choose), and a changed Email is
    applied and noted in the lead's Activity. Before, all three and the Email were applied at once
    without a word (n8n mode still does).
    """
    phone = format_phone(lead.get("phone"))
    if not phone:
        return ActionExecution(422, {"code": "invalid_phone", "detail": "Phone is invalid"})

    with transaction() as conn:
        practice = conn.execute(
            "select id from practices where slug=%s and is_active=true",
            (get_settings().n8n_practice_slug,),
        ).fetchone()
        if not practice:
            return ActionExecution(503, {"detail": "Configured practice is not available"})
        current = conn.execute(
            "select id,full_name,phone_e164,status,cadence_state,sheet_name_accepted,email,"
            "date_of_birth,location,lead_type from leads "
            "where id=%s and practice_id=%s for update",
            (lead_id, practice["id"]),
        ).fetchone()
        if not current:
            return ActionExecution(404, {"code": "lead_not_found", "detail": "Lead was not found"})
        incoming_name = " ".join(str(lead.get("full_name") or "").split())
        stored_name = str(current["full_name"] or "").strip()
        # The Sheet is back to the name on file: the earlier "keep old name" no longer applies.
        if current.get("sheet_name_accepted") and incoming_name and _same_name(incoming_name, stored_name):
            conn.execute("update leads set sheet_name_accepted=null where id=%s", (lead_id,))
        # Staff already looked at this exact Sheet name and kept the stored one.
        accepted = bool(incoming_name and current.get("sheet_name_accepted")
                        and _same_name(incoming_name, current["sheet_name_accepted"]))
        name_changed = bool(incoming_name and not _same_name(incoming_name, stored_name)
                            and not accepted)
        phone_changed = current["phone_e164"] != phone
        base = {
            "request_id": str(request_id),
            "lead_id": str(lead_id),
            "previous_lead_id": str(lead_id),
            "created": False,
        }
        if phone_changed:
            close_for_phone_change(conn, current, new_phone=phone, new_name=incoming_name or None)
            code = "phone_changed_closed"
            return ActionExecution(409, {
                **base, "result": code, "code": code,
                "detail": "Phone number changed; the lead is closed. Add the new number as a new row",
            })
        if not sheet_rules:  # n8n mode, as before: only the name is reviewed
            if name_changed:
                flagged = flag_lead_for_review(
                    conn, str(lead_id), "Name changed; needs review", code="name_changed",
                    source="sheet", details={"old_name": stored_name, "new_name": incoming_name},
                )
                if flagged:
                    code = "name_changed_needs_review"
                    return ActionExecution(409, {
                        **base, "result": code, "code": code, "detail": "Name changed; needs review",
                    })
                # A finished lead (booked, closed) has no outreach left to protect: nothing to
                # review, and the stored name stays as it was.
            keep_name = name_changed or accepted
            _update_lead_profile(conn, lead_id, {**lead, "full_name": None} if keep_name else lead)
            return ActionExecution(200, {**base, "result": "profile_updated"})

        held: dict[str, Any] = {}  # Sheet values the lead does not take (reviewed or kept)
        flagged_codes: list[str] = []
        if name_changed and flag_lead_for_review(
            conn, str(lead_id), "Name changed; needs review", code="name_changed",
            source="sheet", details={"old_name": stored_name, "new_name": incoming_name},
        ):
            flagged_codes.append("name_changed")
        if name_changed or accepted:
            # A finished lead (booked, closed) has no outreach left to protect: nothing to review,
            # and the stored name stays as it was. The same for the fields below.
            held["full_name"] = None
        changes = {change["code"]: change for change in _detail_changes(current, lead)}
        back = [code for code in DETAIL_FIELDS if code not in changes]
        if back:  # the row shows the value on file again: an earlier "keep old" is spent
            spend_kept_values(conn, str(lead_id), back)
        for code, change in changes.items():
            # The lead keeps its value until staff choose (or for good on a finished lead).
            held.update(dict.fromkeys(_PROFILE_KEYS[change["column"]]))
            kept = _kept_value(conn, str(lead_id), code)
            if kept and _same_name(kept, change["new_value"]):
                continue  # staff already chose the old value over this one
            details = {key: change[key] for key in ("field", "old_value", "new_value", "new_raw")
                       if key in change}
            if flag_lead_for_review(
                conn, str(lead_id), f"{change['field'].capitalize()} changed; needs review",
                code=code, source="sheet", details=details,
            ):
                flagged_codes.append(code)
        new_email = str(lead.get("email") or "").strip().lower()
        if new_email and new_email != str(current.get("email") or "").strip().lower():
            # Applied at once (not identity), and said in the lead's Activity (owner, 2026-10-25).
            record_status(conn, str(lead_id), current["status"], current["status"], "n8n_sheet",
                          EMAIL_CHANGED)
        _update_lead_profile(conn, lead_id, {**lead, **held})
        if flagged_codes:
            code = f"{flagged_codes[0]}_needs_review"
            detail = ("Name" if flagged_codes[0] == "name_changed"
                      else DETAIL_FIELDS[flagged_codes[0]][2].capitalize()) + " changed; needs review"
            return ActionExecution(409, {**base, "result": code, "code": code, "detail": detail,
                                         "review_codes": flagged_codes})
        return ActionExecution(200, {**base, "result": "profile_updated"})
