from __future__ import annotations

from psycopg.types.json import Jsonb

from .sheet_sync import enqueue_sheet_update

# Skipped by pause/review so Resume can restore only these rows, not DNC/closed skips.
PAUSE_SKIP_REASON = "paused_for_review"


def skip_remaining_planned(conn, lead_id: str, reason: str = PAUSE_SKIP_REASON) -> int:
    """Stop later cadence steps from being claimed while the lead is on hold."""
    rows = conn.execute(
        "update outreach_events set status='skipped',failure_reason=%s,updated_at=now() "
        "where lead_id=%s and status='planned' returning id",
        (reason[:500], lead_id),
    ).fetchall()
    return len(rows)


def restore_pause_skipped(conn, lead_id: str) -> int:
    """Put pause/review-skipped steps back on the schedule when outreach resumes."""
    rows = conn.execute(
        "update outreach_events set status='planned',failure_reason=null,updated_at=now() "
        "where lead_id=%s and status='skipped' and failure_reason=%s returning id",
        (lead_id, PAUSE_SKIP_REASON),
    ).fetchall()
    return len(rows)


# A lead whose outcome is final is never pulled back into review. A terminated or completed
# cadence (replaced by a newer referral, closed after a review) has nothing left to resume either.
_FINAL_STATUSES = "('booked','declined','do_not_contact','closed_no_response')"


def record_review(
    conn,
    lead_id: str,
    code: str,
    message: str,
    *,
    details: dict | None = None,
    source: str = "system",
    outreach_event_id: int | None = None,
) -> int | None:
    """Write (or refresh) the lead_reviews row for one flag; returns its id.

    One open row per thing that went wrong: a step's own row is refreshed rather than duplicated
    (migration 048's fail-safe may already have written a plain one for the same failed step), and
    the same code with the same words is the same flag seen again (a Sheet name re-read).
    """
    # Keys with no value are dropped, so a refresh never blanks what the first writer knew.
    payload = Jsonb({key: value for key, value in (details or {}).items() if value is not None})
    row = None
    first_words = False
    if outreach_event_id is not None:
        row = conn.execute(
            "with old as (select id,source from lead_reviews where lead_id=%s and resolved_at is null "
            "and outreach_event_id=%s order by id desc limit 1 for update) "
            "update lead_reviews r set code=%s,message=%s,details=r.details||%s,source=%s,"
            "updated_at=now() from old where r.id=old.id returning r.id,old.source as old_source",
            (lead_id, outreach_event_id, code, message[:500], payload, source),
        ).fetchone()
        # A row the 048 fail-safe wrote moments ago (source 'db') has no history line yet; the
        # application's words become that line.
        first_words = bool(row and row.get("old_source") == "db")
    if not row:
        row = conn.execute(
            "update lead_reviews set details=details||%s,updated_at=now() "
            "where id=(select id from lead_reviews where lead_id=%s and resolved_at is null "
            "and code=%s and message=%s order by id desc limit 1) returning id",
            (payload, lead_id, code, message[:500]),
        ).fetchone()
    if not row:
        row = conn.execute(
            "insert into lead_reviews(lead_id,code,message,details,source,outreach_event_id) "
            "values(%s,%s,%s,%s,%s,%s) returning id",
            (lead_id, code, message[:500], payload, source, outreach_event_id),
        ).fetchone()
        first_words = bool(row)
    if not row:
        return None
    if first_words:
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "select id,status,status,'system',%s from leads where id=%s",
            (f"Flagged for review: {message}"[:500], lead_id),
        )
    # The Sheet's Outcome Status names the reason ("Needs review: text failed (landline)").
    enqueue_sheet_update(
        conn, lead_id=str(lead_id), event_type="review_flagged", source_key=f"{row['id']}:{code}"
    )
    return row["id"]


def flag_lead_for_review(
    conn,
    lead_id: str,
    reason: str,
    *,
    code: str = "other",
    details: dict | None = None,
    source: str = "system",
    outreach_event_id: int | None = None,
) -> bool:
    """Pause non-terminal outreach whenever staff attention is required.

    This owns the outcome staff see: the lead's status, the reason, and the review row (code,
    details, source) the dashboard turns into a card with choices. Migration 030's trigger
    (replaced in 048) is the fail-safe for paths that never get here (a stale worker, a manual SQL
    fix) and deliberately does less - it flags, pauses, stops the schedule and writes a plain review
    row, but never sets leads.status. Change one, check the other.

    Returns whether the lead is now flagged (False for a finished lead, which is left alone).
    """
    flagged = conn.execute(
        "update leads set needs_review=true,review_reason=%s,review_flagged_at=now(),"
        "status=case when status in ('booked','declined','do_not_contact','closed_no_response',"
        "'invalid_phone') then status else 'needs_attention' end,"
        "cadence_state=case when status in ('booked','declined','do_not_contact',"
        "'closed_no_response') then cadence_state else 'paused' end,"
        "status_changed_at=case when status in ('booked','declined','do_not_contact',"
        "'closed_no_response') then status_changed_at else now() end where id=%s "
        f"and status not in {_FINAL_STATUSES} and cadence_state not in ('completed','terminated') "
        "returning id",
        (reason[:500], lead_id),
    ).fetchone()
    skip_remaining_planned(conn, lead_id)
    if not flagged:
        return False
    record_review(
        conn, lead_id, code, reason, details=details, source=source,
        outreach_event_id=outreach_event_id,
    )
    return True


REPLACED_REASON = "replaced by a newer referral on this number"
# An older lead on the number was restarted (board New / In cadence, Sheet Restart, review restart,
# unblock, resume) and took the number back (owner, 2026-10-26): "a newer referral" was wrong there.
RESTARTED_REASON = "replaced: another lead with this phone was restarted"
REPLACED_REASONS = (REPLACED_REASON, RESTARTED_REASON)


def hand_over_number(
    conn, *, practice_id: int, phone: str, lead_id: str, reason: str = REPLACED_REASON
) -> str | None:
    """Make `lead_id` the one lead working this phone number.

    `reason` says why the others stop: REPLACED_REASON when `lead_id` is a new lead (a new Sheet
    row, a lead added on the dashboard), RESTARTED_REASON when an existing lead is (re)started.

    Repeat patients come back for another body region and reuse their number, so
    several leads may share it - but only one cadence may run, or the patient is
    called twice a day. Every other lead still in outreach on the number stops
    here (its history stays). Opt-outs belong to the person, not the referral,
    so the new lead inherits any the number already has.

    Returns a warning naming the other leads on the number, or None.
    """
    others = conn.execute(
        "select id,full_name,lead_type,status,cadence_state,call_opt_out,sms_opt_out,"
        "status_changed_at "
        "from leads where practice_id=%s and phone_e164=%s and id<>%s "
        "order by created_at for update",
        (practice_id, phone, lead_id),
    ).fetchall()
    if not others:
        return None
    for other in others:
        # Booked is final (owner, 2026-10-26): a booked lead keeps its status whatever runs next.
        if other["status"] != "booked" and other["cadence_state"] in {"pending", "active", "paused"}:
            # Its open review closes as "replaced" here: migration 048's clear trigger only knows
            # REPLACED_REASON's words and would record the restarted case as plain "closed".
            conn.execute(
                "update lead_reviews set resolved_at=now(),resolved_by=coalesce(resolved_by,'system'),"
                "resolved_by_name=coalesce(resolved_by_name,'System'),"
                "resolution=coalesce(resolution,'replaced'),updated_at=now() "
                "where lead_id=%s and resolved_at is null",
                (other["id"],),
            )
            # needs_review goes too: the board shows any flagged lead in Needs
            # Attention first, and a replaced lead has nothing left to act on.
            # Its status closes as well (owner, 2026-10-26): it kept "In cadence" / "Callback
            # scheduled" (or "Needs review") after its calls had ended. Closed the way a front
            # desk close is; status_reason keeps the "replaced" words for the card and the Sheet.
            conn.execute(
                "update leads set status='closed_no_response',cadence_state='terminated',"
                "status_reason=%s,needs_review=false,status_changed_at=now() where id=%s",
                (reason, other["id"]),
            )
            skip_remaining_planned(conn, str(other["id"]), reason)
            conn.execute(
                "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
                "values(%s,%s,'closed_no_response','system',%s)",
                (other["id"], other["status"], reason),
            )
    conn.execute(
        "update leads set call_opt_out=call_opt_out or %s,sms_opt_out=sms_opt_out or %s "
        "where id=%s",
        (
            any(o["call_opt_out"] for o in others),
            any(o["sms_opt_out"] for o in others),
            lead_id,
        ),
    )
    names = ", ".join(
        f"{o['full_name']} ({o['lead_type']})" if o["lead_type"] else str(o["full_name"])
        for o in others
    )
    warning = f"This number is also on file as {names}."
    # A wrong number is a warning, not a block: staff may have corrected the
    # record (the name, not the number, is often what was wrong).
    wrong = [o for o in others if o["status"] == "invalid_phone"]
    if wrong:
        marked = ", ".join(
            f"{o['full_name']} on {o['status_changed_at']:%b} {o['status_changed_at'].day}"
            if o["status_changed_at"] else str(o["full_name"])
            for o in wrong
        )
        warning += f" It was marked as a wrong number on {marked}."
    return warning
