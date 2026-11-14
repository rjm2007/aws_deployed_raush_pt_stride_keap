"""Live test lead in the SMS agent test tab (2026-11-14, docs/SMS_AGENT.md "Test chat tab").

The sandbox chat cannot show the real system: the cadence hold while texting, several texts in a
row answered together, a booking on the demo calendar shown on the dashboard. Our tester's Indian
phone cannot text the US number either. So the tab may play the patient of a real test lead: each
text is stored and queued exactly like Twilio's webhook does, the sms-agent-worker answers it, and
the reply is saved, never texted (sms_inbound.DASHBOARD_TEXT_SID_PREFIX). Only is_test leads.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from .sms_inbound import DASHBOARD_TEXT_SID_PREFIX

MAX_LEADS = 50
MAX_MESSAGES = 200

LEADS_SQL = """
select l.id,l.full_name,right(coalesce(l.phone_e164,''),4) as phone_last4,l.status,l.cadence_state,
       l.needs_review,l.created_at
from leads l where l.practice_id=%s and l.is_test and l.phone_e164 is not null
order by l.created_at desc limit %s
"""


def test_leads(conn, practice_id: int) -> list[dict[str, Any]]:
    return conn.execute(LEADS_SQL, (practice_id, MAX_LEADS)).fetchall()


def test_lead(conn, practice_id: int, lead_id: str) -> dict[str, Any] | None:
    """The lead if it is one of this practice's test leads with a phone, else None."""
    return conn.execute(
        "select id,full_name,phone_e164,status,cadence_state,needs_review,review_reason,is_test "
        "from leads where id::text=%s and practice_id=%s and is_test and phone_e164 is not null",
        (lead_id, practice_id),
    ).fetchone()


def hold_until(lead: dict[str, Any], last_text_at: datetime | None, settings: Settings,
               now: datetime) -> datetime | None:
    """Until when the cadence holds for this lead (worker.TEXTING_HOLD_SQL's rule), or None."""
    if last_text_at is None or not settings.sms_agent_enabled:
        return None
    if settings.sms_agent_test_leads_only and not lead["is_test"]:
        return None
    if settings.test_mode and lead["is_test"]:
        hold = timedelta(minutes=settings.cadence_hold_after_text_test_minutes)
    else:
        hold = timedelta(hours=settings.cadence_hold_after_text_hours)
    until = last_text_at + hold
    return until if hold and until > now else None


def chat(conn, lead: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """The lead's real conversation, schedule and Elliot's actions, as the tab shows them."""
    lead_id = lead["id"]
    messages = conn.execute(
        "select * from (select id,direction,sender,body,occurred_at as at,"
        "coalesce(provider_message_id like %s,false) as from_dashboard "
        "from sms_messages where lead_id=%s order by occurred_at desc,id desc limit %s) m "
        "order by at,id",
        (DASHBOARD_TEXT_SID_PREFIX + "%", lead_id, MAX_MESSAGES),
    ).fetchall()
    steps = conn.execute(
        "select id::text as id,channel,day_offset,status,scheduled_for,failure_reason as note "
        "from outreach_events where lead_id=%s order by scheduled_for nulls last,day_offset,id",
        (lead_id,),
    ).fetchall()
    actions = conn.execute(
        "select tool,result_code,created_at as at from sms_agent_actions where lead_id=%s "
        "order by created_at,id",
        (lead_id,),
    ).fetchall()
    state = conn.execute(
        "select now() as now,"
        " exists(select 1 from sms_agent_jobs where phone_e164=%(phone)s"
        "        and status in ('pending','running')) as typing,"
        " (select max(sm.occurred_at) from sms_messages sm join leads peer on peer.id=sm.lead_id"
        "  where peer.phone_e164=%(phone)s and sm.direction='inbound') as last_text_at",
        {"phone": lead["phone_e164"]},
    ).fetchone()
    return {
        "lead": {key: lead[key] for key in
                 ("id", "full_name", "status", "cadence_state", "needs_review", "review_reason")},
        "messages": messages,
        "steps": steps,
        "actions": actions,
        "elliot_typing": bool(state["typing"]),
        "hold_until": hold_until(lead, state["last_text_at"], settings, state["now"]),
    }
