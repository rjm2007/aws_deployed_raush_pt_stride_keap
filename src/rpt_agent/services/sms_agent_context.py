"""What the SMS agent knows about the person texting, built fresh from the database for every reply.

The model has no memory of its own (SMS agent plan section 4): each reply gets the lead (name, case,
clinic, status, open review), the booked visit, the last calls and the last ~20 texts both ways.
Phone numbers never go to the model: the lead always comes from the job, never from the model, and
call summaries are scrubbed of anything that looks like a phone number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from . import appointment_views
from .front_desk import CLINIC_TZ
from .lead_review import SMS_FRONT_DESK_REASONS, TITLES
from .review import BOOKABLE_REVIEW_SQL

HISTORY_LIMIT = 20
CALL_LIMIT = 3
SUMMARY_CHARS = 200
MEDIA_ONLY = "(sent a picture or file, no text)"


@dataclass
class LeadView:
    """Everything the model may know about the person texting (no phone number)."""

    is_new_person: bool
    first_name: str
    last_name: str
    date_of_birth: date | None
    clinic: str
    service: str
    referral_word: str
    status: str
    cadence_state: str
    needs_review: bool
    review_reason: str
    booked_visit: str
    recent_calls: list[str] = field(default_factory=list)
    # Every open review is Elliot's own front-desk card for something he does not handle
    # (insurance, a person, ...): booking by text goes on (owner, 2026-11-02, review.py).
    front_desk_only: bool = False

    @property
    def booking_paused(self) -> bool:
        """A review stops booking by text: any open review but Elliot's own front-desk card."""
        return self.needs_review and not self.front_desk_only


@dataclass
class Turn:
    role: Literal["patient", "elliot", "cadence", "staff"]
    text: str
    at: datetime | None = None


@dataclass
class LiveContext:
    lead: LeadView
    history: list[Turn]
    practice_id: int
    holidays: list
    booking_link: str | None
    timezone: str
    services: str = ""          # every service we book ("Pelvic Health, Physical Therapy")
    service_aliases: str = ""   # everyday words per service ("Physical Therapy = ortho, PT")


# --- helpers ---------------------------------------------------------------------------------------

_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def scrub(text: str | None, limit: int = SUMMARY_CHARS) -> str:
    """A call summary for the model: one line, no phone numbers, short."""
    flat = " ".join(str(text or "").split())
    flat = _PHONE.sub("[number]", flat)
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _clock(value: datetime, tz: ZoneInfo) -> str:
    local = value.astimezone(tz)
    return f"{local:%a} {local:%b} {local.day}, {local:%I:%M %p}".replace(", 0", ", ")


STATUS_WORDS = {
    "new": "new referral, not contacted yet",
    "in_progress": "in our call and text schedule",
    "callback_scheduled": "asked us to call back",
    "booking_link_sent": "was sent the booking link",
    "booked": "booked",
    "declined": "said not interested before",
    "transferred_human": "was passed to the front desk before",
    "needs_attention": "waiting for staff review",
    "invalid_phone": "marked wrong number",
    "closed_no_response": "closed (no answer to our calls and texts)",
    "do_not_contact": "do not contact",
}
FRONT_DESK_STATUS = "front desk will reach out about a question (calls paused)"


def _role(direction: str, sender: str | None) -> str:
    if direction == "inbound" or sender == "patient":
        return "patient"
    if sender == "ai":
        return "elliot"
    if sender == "staff":
        return "staff"
    # 'cadence', 'system' and rows from before migration 054 (null): our automatic texts.
    return "cadence"


# --- live ------------------------------------------------------------------------------------------

LEAD_SQL = """
select l.id,l.practice_id,l.first_name,l.last_name,l.full_name,l.date_of_birth,l.location,
       l.lead_type,l.status,l.cadence_state,l.needs_review,l.review_reason,
       (select r.code from lead_reviews r where r.lead_id=l.id and r.resolved_at is null
         order by r.flagged_at,r.id limit 1) as review_code,
       (select array_agg(r.details->>'reason' order by r.flagged_at,r.id) from lead_reviews r
         where r.lead_id=l.id and r.resolved_at is null) as review_reasons,
       not exists(select 1 from lead_reviews r where r.lead_id=l.id and r.resolved_at is null
         and not """ + BOOKABLE_REVIEW_SQL + """) as reviews_bookable,
       bl.name as clinic,bl.timezone as clinic_tz,ps.holidays,ps.booking_link_url
from leads l
left join booking_locations bl on bl.practice_id=l.practice_id and bl.is_active
  and lower(bl.name)=lower(coalesce(l.location,''))
left join practice_settings ps on ps.practice_id=l.practice_id
where l.id=%s
"""


def load_live(conn, lead_id: str, *, now: datetime | None = None,
              up_to_inbound_id: int | None = None) -> LiveContext:
    """LeadView + text history for a real lead (read only). `up_to_inbound_id`: the job's newest
    text; the patient's texts after it are left out (text_history)."""
    from .slot_booking import call_case_variables  # local: slot_booking imports review modules

    now = now or datetime.now(UTC)
    row = conn.execute(LEAD_SQL, (lead_id,)).fetchone()
    if not row:
        raise LookupError("lead not found")
    case = call_case_variables(conn, lead_id)
    names = (row["full_name"] or "").split()
    last_on_file = (row["last_name"] or "").strip()
    # The full name's first word is a first name only when it is not the last name on file (x-04,
    # round 2: full name "Quill" = the last name, and Elliot texted "You're all set, Quill!").
    first = (row["first_name"] or "").strip() or (
        names[0] if names and names[0].lower() != last_on_file.lower() else "")
    last = last_on_file or (" ".join(names[1:]) if len(names) > 1 else "")
    review = ""
    # needs_review with no open review row (a manual SQL fix) stops booking: only open front-desk
    # cards that allow booking make it front desk only.
    front_desk_only = bool(row["needs_review"] and row["review_code"] and row["reviews_bookable"])
    if front_desk_only:
        review = ", ".join(dict.fromkeys(
            SMS_FRONT_DESK_REASONS.get(reason or "", SMS_FRONT_DESK_REASONS["other"])
            for reason in row["review_reasons"] or []))
    elif row["needs_review"]:
        review = TITLES.get(row["review_code"] or "", "") or (row["review_reason"] or "")
    lead = LeadView(
        is_new_person=False,
        first_name=first,
        last_name=last,
        date_of_birth=row["date_of_birth"],
        clinic=row["clinic"] or (row["location"] or "").strip(),
        service=case.get("case_type", ""),
        referral_word=(row["lead_type"] or "").strip(),
        status=row["status"],
        cadence_state=row["cadence_state"],
        needs_review=bool(row["needs_review"]),
        review_reason=review,
        booked_visit=booked_visit(conn, row, now),
        recent_calls=recent_calls(conn, lead_id),
        front_desk_only=front_desk_only,
    )
    return LiveContext(
        lead=lead, history=text_history(conn, lead_id, up_to_inbound_id=up_to_inbound_id),
        practice_id=row["practice_id"],
        holidays=row["holidays"] or [], booking_link=row["booking_link_url"],
        timezone=row["clinic_tz"] or str(CLINIC_TZ),
        services=case.get("case_types_offered", ""), service_aliases=case.get("service_aliases", ""),
    )


def booked_visit(conn, lead_row: dict, now: datetime) -> str:
    """"Tue Nov 3 at 9:00 AM with Alan Rome at Laguna Niguel" for the visit still on the books."""
    card = appointment_views.booking_card(conn, lead_row["practice_id"], lead_row["id"], now)
    if card and appointment_views.status_bucket(card["state"]) == "booked" and card["start_utc"] >= now:
        tz = ZoneInfo(lead_row.get("clinic_tz") or str(CLINIC_TZ))
        local = card["start_utc"].astimezone(tz)
        text = f"{local:%a} {local:%b} {local.day} at {local:%I:%M %p}".replace(" 0", " ")
        who = card.get("practitioner_name")
        if who and who != "Unassigned practitioner":
            text += f" with {who}"
        if card.get("location") and card["location"] != "Location unavailable":
            text += f" at {card['location']}"
        return text
    if lead_row["status"] == "booked":
        return "booked (the visit's day and time are not in our records yet)"
    return ""


def recent_calls(conn, lead_id: str, limit: int = CALL_LIMIT) -> list[str]:
    rows = conn.execute(
        "select cl.dialed_at,cl.answer_state,cl.summary_text,oe.outcome from call_logs cl "
        "left join outreach_events oe on oe.id=cl.outreach_event_id where cl.lead_id=%s "
        "order by cl.dialed_at desc limit %s",
        (lead_id, limit),
    ).fetchall()
    lines = []
    for row in rows:
        line = f"{_clock(row['dialed_at'], CLINIC_TZ)}: {row['answer_state'] or 'unknown'}"
        if row["outcome"]:
            line += f", outcome {row['outcome']}"
        if row["summary_text"]:
            line += f". Summary: {scrub(row['summary_text'])}"
        lines.append(line)
    return lines


def text_history(conn, lead_id: str, limit: int = HISTORY_LIMIT, *,
                 up_to_inbound_id: int | None = None) -> list[Turn]:
    # A patient's text after the job's newest one (it committed after the worker moved the job
    # there, e.g. one that waited on the worker's lead lock) queued the next job, which answers
    # it: read here too, it was answered twice.
    rows = conn.execute(
        "select direction,sender,body,occurred_at from sms_messages where lead_id=%s "
        "and (direction<>'inbound' or %s::bigint is null or id<=%s) "
        "order by occurred_at desc,id desc limit %s",
        (lead_id, up_to_inbound_id, up_to_inbound_id, limit),
    ).fetchall()
    turns = []
    for row in reversed(rows):
        role = _role(row["direction"], row.get("sender"))
        text = str(row["body"] or "").strip()
        if not text:
            if role != "patient":
                continue
            text = MEDIA_ONLY
        turns.append(Turn(role=role, text=text, at=row["occurred_at"]))
    return turns


# --- what the model reads --------------------------------------------------------------------------

def context_block(lead: LeadView, *, now: datetime, front_desk_open: bool,
                  booking_link: str | None, front_desk_phone: str, timezone: str | None = None,
                  services: str = "", service_aliases: str = "") -> str:
    """The developer "Context" message (built by code, changes every reply)."""
    tz = ZoneInfo(timezone or str(CLINIC_TZ))
    local = now.astimezone(tz)
    tomorrow = local.date() + timedelta(days=1)
    lines = [
        "Context (from our records; the patient does not see this):",
        f"- Now: {local:%A}, {local:%B} {local.day}, {local.year}, "
        f"{local:%I:%M %p}".replace(", 0", ", ") + " clinic time (Pacific)",
        # Spelled out so "tmrw" is searched as tomorrow, not today (gpt-5.5 eval, book-12).
        (f"- Tomorrow: {tomorrow:%A}, {tomorrow:%B} {tomorrow.day}, {tomorrow.year} "
         f"({tomorrow.isoformat()})"),
        f"- Front desk open now: {'yes' if front_desk_open else 'no'}",
        f"- Front desk phone: {front_desk_phone}",
        f"- Booking link: {booking_link or 'not set'}",
    ]
    if lead.is_new_person:
        lines.append("- Person texting: someone new, not in our records (no referral on file)")
    else:
        lines.append("- Person texting: a patient with a referral on file")
    name = " ".join(part for part in (lead.first_name, lead.last_name) if part)
    lines.append(f"- Name on file: {name or 'unknown'}")
    if lead.date_of_birth:
        dob = lead.date_of_birth
        lines.append(f"- Birthday on file (only to confirm identity before booking): "
                     f"{dob:%B} {dob.day}, {dob.year}")
    lines += [
        f"- Clinic: {lead.clinic or 'unknown'}",
        f"- Service: {lead.service or 'unknown'}",
        f"- Referral is for: {lead.referral_word or 'unknown'}",
        f"- Services we book: {services or 'unknown'}",
    ]
    if service_aliases:
        lines.append(f"- Other words for services: {service_aliases}")
    if not lead.is_new_person:
        status = STATUS_WORDS.get(lead.status, lead.status)
        if lead.front_desk_only and lead.status == "needs_attention":
            # Paused only for Elliot's own front-desk card: "waiting for staff review" would read
            # as "don't book" (prompt).
            status = FRONT_DESK_STATUS
        lines.append(f"- Lead status: {status}")
        lines.append(f"- Cadence: {lead.cadence_state}")
    if lead.front_desk_only:
        lines.append(f"- Needs review: front desk only ({lead.review_reason or 'a question'}; "
                     "booking by text still works)")
    else:
        lines.append(f"- Needs review: {'yes' if lead.needs_review else 'no'}"
                     + (f" ({lead.review_reason})" if lead.needs_review and lead.review_reason
                        else ""))
    if lead.booked_visit:
        lines.append(f"- Booked visit: {lead.booked_visit}")
    if lead.recent_calls:
        lines.append("- Recent calls from Elliot (newest first):")
        lines += [f"  - {call}" for call in lead.recent_calls]
    return "\n".join(lines)


def history_items(history: list[Turn], timezone: str | None = None) -> list[dict[str, Any]]:
    """History as model input: patient = user, Elliot = assistant; our other texts (cadence, staff)
    are labelled developer lines, never assistant turns (Elliot did not write them)."""
    tz = ZoneInfo(timezone or str(CLINIC_TZ))
    items: list[dict[str, Any]] = []
    for turn in history:
        when = f" ({_clock(turn.at, tz)})" if turn.at else ""
        # Patient and Elliot turns carry their clinic time too, so the model can tell today's texts
        # from last week's (an old list, an intro days ago). The prompt says never to write one;
        # sms_agent._clean drops a copied one.
        stamp = f"[{_clock(turn.at, tz)}] " if turn.at else ""
        if turn.role == "patient":
            items.append({"role": "user", "content": stamp + turn.text})
        elif turn.role == "elliot":
            items.append({"role": "assistant", "content": stamp + turn.text})
        else:
            who = "clinic staff" if turn.role == "staff" else "the clinic's automatic schedule"
            items.append({"role": "developer",
                          "content": f"Earlier text to the patient from {who}, not Elliot{when}:\n"
                                     f"{turn.text}"})
    return items
