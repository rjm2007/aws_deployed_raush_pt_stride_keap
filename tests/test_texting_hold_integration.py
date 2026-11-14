"""A patient's text holds the cadence while Elliot answers (Emma's test, 2026-11-13), real schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied, never production).
The worker runs `skip_steps_held_by_texting` and then CLAIM_SQL in one transaction; here both run
at a simulated time (now() replaced), so calling hours and the hold are exact.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from rpt_agent.config import get_settings
from rpt_agent.services.review import (
    PAUSE_SKIP_REASON,
    restore_pause_skipped,
    skip_remaining_planned,
)
from rpt_agent.worker import CLAIM_SQL, TEXTING_HOLD_REASON, skip_steps_held_by_texting

pytestmark = pytest.mark.integration
CLINIC = ZoneInfo("America/Los_Angeles")
HOURS = {str(d): {"open": "08:00", "close": "19:00"} for d in range(1, 6)}
MONDAY_10 = datetime(2026, 11, 16, 10, 0, tzinfo=CLINIC).astimezone(UTC)
SUNDAY_10 = datetime(2026, 11, 15, 10, 0, tzinfo=CLINIC).astimezone(UTC)


def _sim(sql: str) -> str:
    return sql.replace("now()", "%(sim)s::timestamptz")


class _SimConn:
    """The real connection, with now() at the simulated time (for skip_steps_held_by_texting)."""

    def __init__(self, conn, at):
        self.conn, self.at = conn, at

    def execute(self, sql, params):
        return self.conn.execute(_sim(sql), {**params, "sim": self.at})


def _settings(**overrides):
    base = {"sms_agent_enabled": True, "sms_agent_test_leads_only": False, "test_mode": False,
            "cadence_hold_after_text_hours": 24, "cadence_hold_after_text_test_minutes": 10}
    return get_settings().model_copy(update={**base, **overrides})


@pytest.fixture
def db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/Los_Angeles') returning id",
        (f"test-hold-{uuid4().hex[:10]}",)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id,business_hours) values(%s,%s)",
                 (practice, json.dumps(HOURS)))

    class Db:
        def lead(self, *, is_test=False, phone=None, **columns):
            phone = phone or f"+1555{uuid4().int % 10_000_000:07d}"
            lead = conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
                "status,cadence_state,is_test) values(%s,'google_sheets','T P','T','P',%s,'in_progress',"
                "'active',%s) returning id,phone_e164",
                (practice, phone, is_test)).fetchone()
            for column, value in columns.items():
                conn.execute(f"update leads set {column}=%s where id=%s", (value, lead["id"]))
            return lead

        def step(self, lead, at, channel="call"):
            return conn.execute(
                "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for) "
                "values(%s,1,%s,'planned',%s) returning id", (lead["id"], channel, at)).fetchone()["id"]

        def texted(self, lead, at, direction="inbound"):
            conn.execute(
                "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,sender) "
                "values(%s,%s,'synthetic',%s,%s,%s)",
                (lead["id"], direction, at, "received" if direction == "inbound" else "delivered",
                 "patient" if direction == "inbound" else "cadence"))

        def tick(self, at, settings):
            """One worker claim at `at`: the hold first, then the claim. Returns claimed event ids;
            claimed steps are marked delivered (as if sent), so a later tick goes on from there."""
            with conn.transaction():
                conn.execute("select set_config('rpt.worker_claim_protocol','v1',true)")
                skip_steps_held_by_texting(_SimConn(conn, at), settings)
                rows = conn.execute(_sim(CLAIM_SQL), {"sim": at, "limit": 500,
                                                       "test_mode": settings.test_mode}).fetchall()
                ids = [r["id"] for r in rows]
                conn.execute("update outreach_events set status='delivered',executed_at=%s "
                             "where id=any(%s)", (at, ids))
            return set(ids)

        def event(self, event_id):
            return conn.execute("select status,failure_reason from outreach_events where id=%s",
                                (event_id,)).fetchone()

    yield Db()
    leads = [r["id"] for r in conn.execute("select id from leads where practice_id=%s", (practice,))]
    conn.execute("delete from sms_messages where lead_id=any(%s)", (leads,))
    conn.execute("delete from outreach_events where lead_id=any(%s)", (leads,))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", ([str(i) for i in leads],))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()


def _held(db, event_id):
    return db.event(event_id) == {"status": "skipped", "failure_reason": TEXTING_HOLD_REASON}


@pytest.mark.parametrize(("texted_ago", "held"), [
    (timedelta(hours=2), True),
    (timedelta(hours=23, minutes=59), True),
    (timedelta(hours=25), False),
])
def test_real_lead_is_held_for_24_hours_after_a_text(db, texted_ago, held):
    lead = db.lead()
    db.texted(lead, MONDAY_10 - texted_ago)
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    claimed = db.tick(MONDAY_10, _settings())
    assert (step in claimed) is not held
    assert _held(db, step) is held


@pytest.mark.parametrize(("texted_ago", "held"), [
    (timedelta(minutes=5), True),
    (timedelta(minutes=11), False),
])
def test_test_mode_test_lead_uses_the_minutes(db, texted_ago, held):
    lead = db.lead(is_test=True)
    db.texted(lead, MONDAY_10 - texted_ago)
    step = db.step(lead, MONDAY_10 - timedelta(seconds=10), channel="sms")
    claimed = db.tick(MONDAY_10, _settings(test_mode=True))
    assert (step in claimed) is not held
    assert _held(db, step) is held


def test_test_lead_without_test_mode_uses_the_hours(db):
    lead = db.lead(is_test=True)
    db.texted(lead, MONDAY_10 - timedelta(minutes=30))
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    assert db.tick(MONDAY_10, _settings()) == set()
    assert _held(db, step)


def test_a_text_to_another_lead_on_the_phone_holds_this_lead(db):
    lead = db.lead()
    older = db.lead(phone=lead["phone_e164"], status="closed_no_response", cadence_state="completed")
    db.texted(older, MONDAY_10 - timedelta(hours=1))
    other_phone = db.lead()
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    free = db.step(other_phone, MONDAY_10 - timedelta(minutes=1))
    assert db.tick(MONDAY_10, _settings()) == {free}
    assert _held(db, step)


def test_only_the_patients_texts_count(db):
    lead = db.lead()
    db.texted(lead, MONDAY_10 - timedelta(hours=1), direction="outbound")
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    assert db.tick(MONDAY_10, _settings()) == {step}


@pytest.mark.parametrize("settings", [
    {"sms_agent_enabled": False},
    {"sms_agent_test_leads_only": True},  # a real lead: Elliot does not answer it
    {"cadence_hold_after_text_hours": 0},
])
def test_no_hold_when_elliot_does_not_answer_or_the_hold_is_off(db, settings):
    lead = db.lead()
    db.texted(lead, MONDAY_10 - timedelta(hours=1))
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    assert db.tick(MONDAY_10, _settings(**settings)) == {step}


def test_test_leads_only_still_holds_a_test_lead(db):
    lead = db.lead(is_test=True)
    db.texted(lead, MONDAY_10 - timedelta(minutes=2))
    step = db.step(lead, MONDAY_10 - timedelta(seconds=5))
    assert db.tick(MONDAY_10, _settings(test_mode=True, sms_agent_test_leads_only=True)) == set()
    assert _held(db, step)


def test_test_minutes_zero_turns_the_test_hold_off(db):
    lead = db.lead(is_test=True)
    db.texted(lead, MONDAY_10 - timedelta(minutes=2))
    step = db.step(lead, MONDAY_10 - timedelta(seconds=5))
    assert db.tick(MONDAY_10, _settings(test_mode=True, cadence_hold_after_text_test_minutes=0)) == {step}


def test_steps_due_in_the_hold_are_skipped_and_later_steps_go_out_on_time(db):
    """Emma's run: Day 3 call, Day 5 call and text came due while she texted. None is sent later."""
    lead = db.lead(is_test=True)
    settings = _settings(test_mode=True)
    start = MONDAY_10
    db.texted(lead, start)
    day3 = db.step(lead, start + timedelta(minutes=2))
    day5_call = db.step(lead, start + timedelta(minutes=4))
    day5_text = db.step(lead, start + timedelta(minutes=5), channel="sms")
    day7 = db.step(lead, start + timedelta(minutes=12))
    sent = set()
    for minute in range(16):
        sent |= db.tick(start + timedelta(minutes=minute, seconds=1), settings)
    assert sent == {day7}
    assert all(_held(db, step) for step in (day3, day5_call, day5_text))


def test_every_new_text_starts_the_hold_again(db):
    lead = db.lead(is_test=True)
    settings = _settings(test_mode=True)
    db.texted(lead, MONDAY_10)
    db.texted(lead, MONDAY_10 + timedelta(minutes=8))
    step = db.step(lead, MONDAY_10 + timedelta(minutes=15))
    assert db.tick(MONDAY_10 + timedelta(minutes=15), settings) == set()
    assert _held(db, step)
    later = db.step(lead, MONDAY_10 + timedelta(minutes=19))
    assert db.tick(MONDAY_10 + timedelta(minutes=19), settings) == {later}


def test_a_step_due_after_the_hold_waits_for_calling_hours(db):
    lead = db.lead()
    db.texted(lead, SUNDAY_10 - timedelta(hours=30))
    step = db.step(lead, SUNDAY_10 - timedelta(minutes=1))
    assert db.tick(SUNDAY_10, _settings()) == set()  # Sunday: closed, and not held
    assert db.event(step) == {"status": "planned", "failure_reason": None}
    assert db.tick(MONDAY_10, _settings()) == {step}


@pytest.mark.parametrize("rule", [
    {"sms_opt_out": True}, {"sms_disabled": True}, {"status": "do_not_contact"},
    {"needs_review": True}, {"cadence_state": "paused"},
])
def test_other_stops_keep_their_own_rule(db, rule):
    lead = db.lead(**rule)
    db.texted(lead, MONDAY_10 - timedelta(hours=1))
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1), channel="sms")
    assert db.tick(MONDAY_10, _settings()) == set()
    assert db.event(step) == {"status": "planned", "failure_reason": None}


def test_a_suppressed_number_keeps_its_own_rule(db):
    lead = db.lead()
    db.texted(lead, MONDAY_10 - timedelta(hours=1))
    step = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("insert into suppressed_numbers(phone_e164,reason) values(%s,'test') "
                     "on conflict do nothing", (lead["phone_e164"],))
        try:
            assert db.tick(MONDAY_10, _settings()) == set()
            assert db.event(step) == {"status": "planned", "failure_reason": None}
        finally:
            conn.execute("delete from suppressed_numbers where phone_e164=%s", (lead["phone_e164"],))


def test_resume_restores_paused_steps_not_held_ones(db):
    lead = db.lead()
    db.texted(lead, MONDAY_10 - timedelta(hours=1))
    held = db.step(lead, MONDAY_10 - timedelta(minutes=1))
    later = db.step(lead, MONDAY_10 + timedelta(days=2))
    db.tick(MONDAY_10, _settings())
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], row_factory=dict_row) as conn:
        assert skip_remaining_planned(conn, str(lead["id"])) == 1
        assert restore_pause_skipped(conn, str(lead["id"])) == 1
    assert _held(db, held)
    assert db.event(later) == {"status": "planned", "failure_reason": None}
    assert PAUSE_SKIP_REASON != TEXTING_HOLD_REASON
