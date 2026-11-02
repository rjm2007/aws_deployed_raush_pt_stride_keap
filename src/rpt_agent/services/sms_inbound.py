"""Inbound texts for the SMS agent ("Elliot by text", docs/SMS_AGENT.md).

The Twilio webhook stores the text and calls `queue_reply`: one pending job per phone, due a short
quiet window later, so three quick texts get one reply. The sms-agent-worker claims the due job and
asks `should_reply` (pure: facts in, decision out, first matching rule wins) before any model call.
Facts come from `load_facts`, so each rule is tested without a database and the SQL on a real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..config import Settings, get_settings
from .review import REPLACED_REASONS

# Twilio's own opt-out words (it confirms them itself) and ours. Never "yes": Twilio treats YES as
# a re-subscribe word only for an opted-out number, and our patients say YES to book.
STOP_WORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})
START_WORDS = frozenset({"start", "unstop"})
HELP_WORDS = frozenset({"help", "info"})

# A call to the lead in flight: the reply waits this long and looks again (the call's outcome may
# change what Elliot should say).
CALL_RETRY_SECONDS = 60
# A placed call stays 'attempted' until Vapi's end-of-call report; the cadence worker's sweeper
# settles an unreported one after 2 hours, so an older one is not "in flight".
CALL_IN_FLIGHT_WINDOW = "2 hours"
# The number-not-ours fixed text goes out at most once a day per phone (owner, 2026-11-02).
UNKNOWN_NUMBER_EVERY = "24 hours"

# Job outcomes that are not a skip reason. 'unknown_number' is a fixed text too, named apart so
# the once-a-day rule can find it.
OUTCOME_REPLIED = "replied"
OUTCOME_NO_REPLY = "no_reply"
OUTCOME_FIXED_TEXT = "fixed_text"
OUTCOME_UNKNOWN_NUMBER = "unknown_number"

# Needs Review codes (services/lead_review.py shows unknown codes in the "Other" group).
REVIEW_REPLY_CAP = "sms_agent_reply_cap"
REVIEW_AGENT_FAILED = "sms_agent_failed"


def inbound_command(body: str) -> str | None:
    """'stop' | 'start' | 'help' | 'call' for a keyword text, else None (the agent's)."""
    word = (body or "").strip().lower()
    if word in STOP_WORDS:
        return "stop"
    if word in START_WORDS:
        return "start"
    if word in HELP_WORDS:
        return "help"
    if word == "call":
        return "call"
    return None


# Which lead a phone's text belongs to. Repeat patients reuse their number, so several leads may
# share it; services/review.hand_over_number keeps one working it. That one first (not replaced,
# cadence not ended), then any lead not replaced and not closed (a booked patient asking about the
# visit), then any not replaced; newest first within each.
ACTIVE_LEAD_SQL = """
select id from leads where phone_e164=%(phone)s
order by case
  when coalesce(status_reason,'') <> all(%(replaced)s)
       and cadence_state in ('pending','active','paused') then 0
  when coalesce(status_reason,'') <> all(%(replaced)s)
       and status not in ('declined','do_not_contact','closed_no_response','invalid_phone') then 1
  when coalesce(status_reason,'') <> all(%(replaced)s) then 2
  else 3 end,
  created_at desc, id
limit 1
"""


def active_lead_id(conn, phone: str) -> str | None:
    row = conn.execute(ACTIVE_LEAD_SQL, {"phone": phone, "replaced": list(REPLACED_REASONS)}).fetchone()
    return str(row["id"]) if row else None


# One pending job per phone (migration 054's partial unique index). A new text while it waits
# pushes its due time out and moves it to the newest text; a text arriving while a job runs finds
# no pending row and starts the next job.
QUEUE_SQL = """
insert into sms_agent_jobs(lead_id,phone_e164,due_at,last_inbound_id)
values(%(lead_id)s,%(phone)s,now()+make_interval(secs=>%(quiet)s),%(inbound_id)s)
on conflict(phone_e164) where status='pending' do update set
  due_at=greatest(sms_agent_jobs.due_at,excluded.due_at),
  lead_id=excluded.lead_id,
  last_inbound_id=greatest(sms_agent_jobs.last_inbound_id,excluded.last_inbound_id),
  updated_at=now()
returning id
"""


def queue_reply(
    conn, phone: str, lead_id: str | None, inbound_id: int, *, settings: Settings | None = None
) -> int:
    """Queue (or push out) the reply to this phone; returns the pending job's id."""
    settings = settings or get_settings()
    row = conn.execute(
        QUEUE_SQL,
        {"lead_id": lead_id, "phone": phone, "quiet": settings.sms_agent_quiet_seconds,
         "inbound_id": inbound_id},
    ).fetchone()
    return row["id"]


def stop_number(conn, phone: str, *, has_lead: bool) -> None:
    """STOP: nothing queued for this phone goes out, and a number that is no lead is suppressed.

    explicit_opt_out only marks lead rows, so a STOP from a number with no lead used to leave its
    queued (or next day's) unknown-number text free to go out after the STOP. Like every other
    opt-out of a whole number it goes on suppressed_numbers (nothing removes it automatically).
    """
    if not has_lead:
        conn.execute(
            "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
            "values(%s,'texted STOP (number is not a lead)','twilio_inbound','internal') "
            "on conflict(phone_e164) do nothing",
            (phone,),
        )
    conn.execute(
        "update sms_agent_jobs set status='skipped',outcome='opted_out',finished_at=now(),"
        "updated_at=now() where phone_e164=%s and status='pending'",
        (phone,),
    )


@dataclass
class ReplyFacts:
    """What `should_reply` needs to know about one job. No message text, names or numbers."""

    lead: dict[str, Any] | None  # status, cadence_state, sms_opt_out, sms_disabled, is_test
    suppressed: bool = False
    unknown_number_recent: bool = False
    staff_texted_recently: bool = False
    call_in_flight: bool = False
    ai_replies_today: int = 0
    cap_flagged_today: bool = False
    has_new_text: bool = True
    media_only: bool = False


@dataclass
class Decision:
    action: Literal["agent", "fixed_text", "skip", "retry"]
    reason: str  # job outcome for skip / retry / fixed_text; 'agent' for the agent
    text_key: str | None = None  # sms_agent_texts key for a fixed text
    flag_review: bool = False  # open a Needs Review card (the reply cap, once a day)


def hard_stop(facts: ReplyFacts) -> str | None:
    """Reasons that also stop a reply already written (checked again just before sending)."""
    if facts.suppressed:
        return "suppressed"
    lead = facts.lead
    if lead is None:
        return None
    if lead.get("status") == "do_not_contact":
        return "do_not_contact"
    if lead.get("sms_opt_out"):
        return "sms_opt_out"
    # Staff chose "Continue with calls only" (migration 048): never text this lead.
    if lead.get("sms_disabled"):
        return "texts_off"
    if facts.staff_texted_recently:
        return "staff_took_over"
    return None


def should_reply(facts: ReplyFacts, settings: Settings) -> Decision:
    """docs/SMS_AGENT.md "Should we reply": first match wins; the order matters."""
    if not settings.sms_agent_enabled:
        return Decision("skip", "agent_disabled")
    if facts.suppressed:
        return Decision("skip", "suppressed")
    lead = facts.lead
    if lead is None:
        # Phase 1: a number that is not a lead gets one fixed text a day, never the AI. It carries
        # nothing about anyone, so the test-leads-only switch (about patients' texts reaching the
        # AI) does not hold it back.
        if facts.unknown_number_recent:
            return Decision("skip", "unknown_number_recent")
        return Decision("fixed_text", OUTCOME_UNKNOWN_NUMBER, text_key="unknown_number")
    if lead.get("status") == "do_not_contact":
        return Decision("skip", "do_not_contact")
    if lead.get("sms_opt_out"):
        return Decision("skip", "sms_opt_out")
    if lead.get("sms_disabled"):
        return Decision("skip", "texts_off")
    if settings.sms_agent_test_leads_only and not lead.get("is_test"):
        return Decision("skip", "not_test_lead")
    # Closed as a wrong number (report_lead_status wrong_person): this phone is not the patient's.
    if lead.get("status") == "invalid_phone":
        return Decision("skip", "wrong_person")
    if facts.staff_texted_recently:
        return Decision("skip", "staff_took_over")
    if facts.call_in_flight:
        return Decision("retry", "call_in_progress")
    if facts.ai_replies_today >= settings.sms_agent_daily_reply_cap:
        return Decision("skip", "reply_cap", flag_review=not facts.cap_flagged_today)
    if not facts.has_new_text:
        return Decision("skip", "nothing_to_answer")
    if facts.media_only:
        return Decision("fixed_text", OUTCOME_FIXED_TEXT, text_key="text_only")
    return Decision("agent", "agent")


# The leads sharing the job's phone: staff texts, calls and the daily cap count across them, as the
# patient is one person whichever referral the text was filed under.
_PHONE_LEADS = "(select id from leads where phone_e164=%(phone)s)"


def load_facts(conn, job: dict[str, Any], lead_id: str | None, settings: Settings) -> ReplyFacts:
    """Read the facts for one job. Call with the lead row already locked (worker)."""
    phone = job["phone_e164"]
    params = {"phone": phone, "lead_id": lead_id, "job_id": job["id"],
              "last_inbound": job.get("last_inbound_id"),
              "staff_hours": settings.sms_agent_staff_quiet_hours,
              "unknown_every": UNKNOWN_NUMBER_EVERY, "call_window": CALL_IN_FLIGHT_WINDOW,
              "cap_code": REVIEW_REPLY_CAP}
    suppressed = conn.execute(
        "select exists(select 1 from suppressed_numbers where phone_e164=%(phone)s) as hit", params
    ).fetchone()["hit"]
    if lead_id is None:
        recent = conn.execute(
            "select exists(select 1 from sms_agent_jobs where phone_e164=%(phone)s "
            "and id<>%(job_id)s and outcome='unknown_number' "
            "and finished_at>now()-%(unknown_every)s::interval) as hit",
            params,
        ).fetchone()["hit"]
        return ReplyFacts(lead=None, suppressed=suppressed, unknown_number_recent=recent)
    lead = conn.execute(
        "select l.id,l.status,l.cadence_state,l.sms_opt_out,l.sms_disabled,l.is_test,"
        "coalesce(l.timezone,p.timezone,'America/Los_Angeles') as tz "
        "from leads l left join practices p on p.id=l.practice_id where l.id=%(lead_id)s",
        params,
    ).fetchone()
    if lead is None:
        return ReplyFacts(lead=None, suppressed=suppressed)
    params["tz"] = lead["tz"]
    row = conn.execute(
        "select "
        f"exists(select 1 from sms_messages where lead_id in {_PHONE_LEADS} and direction='outbound' "
        "  and sender='staff' and occurred_at>now()-make_interval(hours=>%(staff_hours)s)) as staff,"
        f"exists(select 1 from outreach_events where lead_id in {_PHONE_LEADS} and channel='call' "
        "  and status in ('in_flight','attempted') "
        "  and coalesce(executed_at,updated_at)>now()-%(call_window)s::interval) as call_live,"
        f"(select count(*) from sms_messages where lead_id in {_PHONE_LEADS} and direction='outbound' "
        "  and sender='ai' and occurred_at>="
        "  (date_trunc('day',now() at time zone %(tz)s) at time zone %(tz)s)) as ai_today,"
        "exists(select 1 from lead_reviews where lead_id=%(lead_id)s and code=%(cap_code)s "
        "  and created_at>=(date_trunc('day',now() at time zone %(tz)s) at time zone %(tz)s)) "
        "  as cap_flagged",
        params,
    ).fetchone()
    # The texts this job answers: after the last text an earlier job of this phone took, up to the
    # job's own newest text. Empty bodies only = a photo or voice note with no words (Twilio sends
    # NumMedia with an empty Body).
    bodies = conn.execute(
        f"select body from sms_messages where lead_id in {_PHONE_LEADS} and direction='inbound' "
        "and id<=coalesce(%(last_inbound)s,id) and id>coalesce((select max(last_inbound_id) "
        "from sms_agent_jobs where phone_e164=%(phone)s and id<>%(job_id)s "
        "and status in ('done','skipped','failed','running')),0)",
        params,
    ).fetchall()
    has_new = bool(bodies)
    return ReplyFacts(
        lead=dict(lead),
        suppressed=suppressed,
        staff_texted_recently=row["staff"],
        call_in_flight=row["call_live"],
        ai_replies_today=int(row["ai_today"]),
        cap_flagged_today=row["cap_flagged"],
        has_new_text=has_new,
        media_only=has_new and all(not (b["body"] or "").strip() for b in bodies),
    )
