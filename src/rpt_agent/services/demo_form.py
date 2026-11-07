"""Lead from the public demo form (demo.aibolt.ai/try, conference demo 2026-11-07).

The lead then runs exactly like a lead from the Sheet: the active cadence, Elliot's calls and texts,
updates on the dashboard. It is not written to the Sheet (only `google_sheets` leads are).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ..config import get_settings
from ..db import transaction
from ..worker import materialize_cadence
from .review import hand_over_number

SOURCE = "demo_form"
CONSENT_TEXT_VERSION = "demo-form-v1"


class DemoFormError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def start_demo_lead(
    *,
    idempotency_key: str,
    first_name: str,
    last_name: str,
    phone: str,
    phone_original: str,
    date_of_birth: date,
) -> str:
    """Create the lead and plan its cadence; the same key twice returns the first lead."""
    settings = get_settings()
    # Same rule as the Sheet and dashboard intake: in test mode every new lead is a test lead
    # (the compressed cadence, no calling-hours gate).
    synthetic = settings.test_mode and settings.app_env.lower() in {"development", "test"}
    with transaction() as conn:
        practice = conn.execute(
            "select id,timezone from practices where slug=%s and is_active=true",
            (settings.n8n_practice_slug,),
        ).fetchone()
        if not practice:
            raise DemoFormError(503, "practice is not configured")
        # One form at a time: the hourly cap and the key check must see each other's lead.
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"demo-form:{practice['id']}",))
        existing = conn.execute(
            "select id from leads where practice_id=%s and source_system=%s "
            "and external_referral_id=%s",
            (practice["id"], SOURCE, idempotency_key),
        ).fetchone()
        if existing:
            return str(existing["id"])
        # The page is public: a QR code passed around must not start hundreds of cadences.
        recent = conn.execute(
            "select count(*) as count from leads where practice_id=%s and source_system=%s "
            "and created_at>now()-interval '1 hour'",
            (practice["id"], SOURCE),
        ).fetchone()
        if recent and recent["count"] >= settings.demo_form_max_per_hour:
            raise DemoFormError(429, "too many demo requests, try again soon")
        inserted = conn.execute(
            "insert into leads(practice_id,source_system,external_referral_id,first_name,"
            "last_name,full_name,phone_e164,phone_original,date_of_birth,timezone,line_type,"
            "consent_captured_at,consent_source,consent_reference,consent_text_version,status,"
            "cadence_state,lead_type,location,owner,is_test) "
            "values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'unknown',now(),'web_form',%s,%s,"
            "'new','pending',%s,%s,'Demo form',%s) returning id",
            (
                practice["id"],
                SOURCE,
                idempotency_key,
                first_name,
                last_name,
                f"{first_name} {last_name}",
                phone,
                phone_original,
                date_of_birth,
                practice["timezone"],
                f"demo-form:{idempotency_key}",
                CONSENT_TEXT_VERSION,
                settings.demo_form_lead_type,
                settings.booking_default_city,
                synthetic,
            ),
        ).fetchone()
        lead_id = str(inserted["id"])
        if not materialize_cadence(conn, lead_id, practice["id"], datetime.now(UTC).date()):
            raise DemoFormError(409, "no active cadence is configured")
        conn.execute(
            "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
            "values(%s,'new','in_progress',%s,'cadence started from the demo form')",
            (lead_id, SOURCE),
        )
        # Booth visitors try again on the same phone: the new lead takes the number and the older
        # one stops, as with a new Sheet row.
        hand_over_number(conn, practice_id=practice["id"], phone=phone, lead_id=lead_id)
    return lead_id
