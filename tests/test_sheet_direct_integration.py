"""Direct Google Sheet sync end to end: fake Sheet, real Postgres schema.

Needs TEST_DATABASE_URL (a disposable database with the migrations applied, never production).
Each test builds its own practice and cadence, uses synthetic 555 numbers, and removes them.
"""

import os
import re
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fake_sheet import FakeSheet
from psycopg.rows import dict_row

from rpt_agent import sheet_sync_worker
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import sheet_direct
from rpt_agent.services.google_sheet import parse_grid
from rpt_agent.services.lead_actions import execute_lead_action
from rpt_agent.services.sheet_sync import enqueue_sheet_update
from rpt_agent.sheet_sync_worker import run_direct_tick

pytestmark = pytest.mark.integration
STEPS = [(0, "call"), (0, "sms"), (3, "call"), (5, "sms")]


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    slug = f"test-sheet-{uuid4().hex[:10]}"
    for key, value in {
        "SUPABASE_DB_URL": url, "N8N_PRACTICE_SLUG": slug, "SHEET_SYNC_ENABLED": "true",
        "SHEET_SYNC_MODE": "direct", "DASHBOARD_PUBLIC_URL": "https://dash.example.test",
        "GOOGLE_SHEET_ID": "sheet", "SHEET_STUCK_SECONDS": "120", "SHEET_SETTLE_SECONDS": "0",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_pool.cache_clear()
    monkeypatch.setattr(sheet_sync_worker, "_direct_state",
                        {"last_full": 0.0, "archived_on": None, "settle": None, "profiles": None})
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/Los_Angeles') "
        "returning id", (slug,)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id) values(%s)", (practice,))
    version = conn.execute(
        "insert into cadence_versions(practice_id,version_number,name,status,activated_at) "
        "values(%s,1,'Synthetic v1','active',now()) returning id", (practice,)).fetchone()["id"]
    for order, (day, channel) in enumerate(STEPS):
        conn.execute(
            "insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,"
            "channel,key,description,is_active) values(%s,%s,%s,%s,%s,%s,%s,true)",
            (practice, version, order, day, channel, f"s{order}-{uuid4().hex[:6]}", "step"))
    # Outbox rows other tests left behind must not be delivered by this test's ticks.
    conn.execute("update integration_outbox set status='dead' where destination='n8n' "
                 "and status in ('pending','sending')")

    class Env:
        db = conn
        practice_id = practice
        sheet = FakeSheet()

        def tick(self, full=True):
            return run_direct_tick(self.sheet, full=full)

        def start_row(self, phone=None, **cells):
            values = {"name": "Asha Rao", "phone": phone or _phone(), "email": "asha@example.test",
                      "date_of_birth": "16/01/1990", "location": "Dana Point", "case": "Ortho",
                      "action": "Start cadence"}
            values.update(cells)
            return self.sheet.add(**values)

        def cell(self, number, key):
            return self.sheet.cell(number, key)

        def leads(self):
            return conn.execute("select * from leads where practice_id=%s order by created_at",
                                (practice,)).fetchall()

        def lead(self, lead_id):
            return conn.execute("select * from leads where id=%s", (lead_id,)).fetchone()

    yield Env()

    leads = conn.execute("select id::text as id,phone_e164 from leads where practice_id=%s",
                         (practice,)).fetchall()
    ids, phones = [r["id"] for r in leads], [r["phone_e164"] for r in leads]
    conn.execute("delete from suppressed_numbers where phone_e164=any(%s)", (phones,))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)", (ids,))
    conn.execute("delete from lead_action_requests where practice_id=%s", (str(practice),))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


def _phone():
    return f"555{uuid4().int % 10_000_000:07d}"


def test_a_start_row_creates_the_lead_and_the_sheet_shows_it(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    assert UUID(lead_id)
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "lead_status") == "In cadence"
    assert env.cell(number, "transcript_link") == (
        f"https://dash.example.test/leads/{lead_id}/conversations/calls")
    # Handled: Action and the Action Request ID empty (owner, 2026-10-25).
    assert env.cell(number, "action") == "" and env.cell(number, "action_request_id") == ""
    lead = env.lead(lead_id)
    assert lead["full_name"] == "Asha Rao" and lead["source_system"] == "google_sheets"
    assert str(lead["date_of_birth"]) == "1990-01-16"
    events = env.db.execute("select count(*) as n from outreach_events where lead_id=%s",
                            (lead_id,)).fetchone()["n"]
    assert events == len(STEPS)
    # Our own writes never re-trigger: more polls change nothing.
    writes = len(env.sheet.writes)
    env.tick()
    env.tick()
    assert len(env.leads()) == 1 and len(env.sheet.writes) == writes


def test_an_invalid_row_shows_why_and_runs_once_staff_fix_it(env):
    number = env.start_row(date_of_birth="")
    env.tick()
    assert env.cell(number, "outcome_status") == (
        "Error: Date Of Birth is required")
    assert env.cell(number, "action_request_id") == "" and env.leads() == []
    assert env.cell(number, "action") == ""  # handled: picked again once the row is fixed
    env.tick()
    assert env.leads() == []  # an Error is not retried by itself
    env.sheet.set(number, "date_of_birth", "16/01/1990")
    env.sheet.set(number, "action", "Start cadence")
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence started" and len(env.leads()) == 1


def test_do_not_contact_and_booked_from_the_sheet(env):
    first, second = env.start_row(), env.start_row(name="Ravi Rao")
    env.tick()
    env.sheet.set(first, "action", "Do Not Contact")  # picked next to "Cadence started"
    env.sheet.set(second, "action", "Booked")
    env.tick()
    assert env.cell(first, "outcome_status") == "Do Not Contact applied"
    assert env.cell(first, "lead_status") == "Do not outreach"
    assert env.lead(env.cell(first, "lead_id"))["status"] == "do_not_contact"
    assert env.cell(second, "outcome_status") == "Booked"
    assert env.cell(second, "lead_status") == "Booked"
    assert env.lead(env.cell(second, "lead_id"))["status"] == "booked"
    assert env.cell(first, "action") == "" and env.cell(second, "action") == ""


def test_a_row_left_processing_is_replayed_not_started_twice(env):
    # The worker stopped after the backend committed but before the Sheet heard back.
    phone = _phone()
    request_id = uuid4()
    execute_lead_action(request_id=request_id, action="start_cadence", lead_id=None, lead={
        "full_name": "Asha Rao", "phone": "+1" + phone, "email": "asha@example.test",
        "date_of_birth": date(1990, 1, 16), "location": "Dana Point", "title": "Ortho",
        "lead_type": "Ortho"})
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    number = env.start_row(phone=phone, outcome_status="Processing: Start cadence",
                           action_request_id=str(request_id), action_started_at=old)
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "lead_id") == str(env.leads()[0]["id"])
    assert len(env.leads()) == 1


def test_a_fresh_processing_row_is_left_alone_until_it_is_stale(env):
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    number = env.start_row(outcome_status="Processing: Start cadence",
                           action_request_id=str(uuid4()), action_started_at=now)
    env.tick()
    assert env.cell(number, "outcome_status") == "Processing: Start cadence" and env.leads() == []


def test_profile_edits_sync_and_a_name_change_goes_to_review(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "email", "new@example.test")
    env.sheet.set(number, "gender", "F")
    env.tick()
    assert env.lead(lead_id)["email"] == "asha@example.test"  # seen once: wait for it to hold
    env.tick()
    lead = env.lead(lead_id)
    # Email (and gender) apply at once (owner, 2026-10-25: the email change is in the Activity).
    assert lead["email"] == "new@example.test" and lead["gender"] == "F"
    assert lead["needs_review"] is False
    assert env.cell(number, "outcome_status") == "Cadence started"  # success writes nothing
    env.sheet.set(number, "name", "Asha Kumar")
    env.tick()
    env.tick()
    assert env.cell(number, "outcome_status") == "Needs review: name changed"
    assert env.cell(number, "lead_status") == "Needs Review"
    assert env.lead(lead_id)["needs_review"] is True
    assert env.lead(lead_id)["full_name"] == "Asha Rao"  # identity is never overwritten


def test_a_wrong_email_is_only_a_warning(env):
    # Owner, 2026-10-26: the cadence starts / goes on, no review, the wrong email is never stored.
    number = env.start_row(email="maria@gmail")
    env.tick()
    lead_id = env.cell(number, "lead_id")
    assert env.cell(number, "outcome_status") == "Cadence started; email looks wrong, please fix it"
    assert env.cell(number, "lead_status") == "In cadence"
    assert env.lead(lead_id)["status"] != "needs_review" and env.lead(lead_id)["email"] is None
    env.sheet.set(number, "email", "maria@gmail.com")  # fixed: applied, warning gone
    env.tick()
    env.tick()
    assert env.lead(lead_id)["email"] == "maria@gmail.com"
    assert env.cell(number, "outcome_status") == "Cadence started"
    notes = env.db.execute("select reason from lead_status_history where lead_id=%s",
                           (lead_id,)).fetchall()
    assert "Email changed in Google Sheet" in [n["reason"] for n in notes]
    env.sheet.set(number, "email", "maria@gmail")  # edited to a wrong one: kept the old one
    env.tick()
    env.tick()
    lead = env.lead(lead_id)
    assert lead["email"] == "maria@gmail.com" and lead["needs_review"] is False
    assert env.cell(number, "lead_status") == "In cadence"
    assert env.cell(number, "outcome_status") == "Email looks wrong, please fix it"
    reviews = env.db.execute("select count(*) as n from lead_reviews where lead_id=%s",
                             (lead_id,)).fetchone()["n"]
    assert reviews == 0
    env.sheet.set(number, "email", "maria@example.test")
    env.tick()
    env.tick()
    assert env.lead(lead_id)["email"] == "maria@example.test"
    assert env.cell(number, "outcome_status") == ""


def test_call_results_are_written_into_the_leads_row(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    event = env.db.execute(
        "update outreach_events set status='delivered',outcome='no_answer',settled_at=now(),"
        "executed_at=now() where id=(select id from outreach_events where lead_id=%s "
        "and channel='call' order by day_offset limit 1) returning id", (lead_id,)).fetchone()
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="call_settled",
                             source_key=f"call:{event['id']}")
    env.tick()
    assert env.cell(number, "cadence_day") == "Day 0"
    assert env.cell(number, "call_outcome") == "No answer"
    assert env.cell(number, "outcome_status") == "Cadence started"
    status = env.db.execute("select status from integration_outbox where aggregate_id=%s "
                            "and event_type='sheet.call_settled'", (lead_id,)).fetchone()
    assert status["status"] == "delivered"


def test_a_new_row_on_a_blocked_number_goes_to_review_not_into_cadence(env):
    # 2026-10-16: a lead re-added on a number that had said "don't call me" showed In cadence
    # while nothing could go out. It must show as Needs Review with the reason instead.
    phone = _phone()
    env.db.execute("insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                   "values(%s,'explicit do-not-contact request','test','internal')", ("+1" + phone,))
    number = env.start_row(phone=phone)
    env.tick()
    assert env.cell(number, "outcome_status") == "Number blocked; needs review"
    lead = env.lead(env.cell(number, "lead_id"))
    assert (lead["status"], lead["cadence_state"], lead["needs_review"]) == ("needs_attention", "paused", True)
    assert "Number blocked" in lead["review_reason"]
    planned = env.db.execute("select count(*) as n from outreach_events where lead_id=%s and status='planned'",
                             (lead["id"],)).fetchone()["n"]
    assert planned == 0
    env.tick()
    assert env.cell(number, "lead_status") == "Needs Review"


def test_the_sheet_gender_is_saved_on_the_lead_and_an_edit_updates_it(env):
    number = env.start_row(gender="Male")
    env.tick()
    lead_id = env.cell(number, "lead_id")
    assert env.lead(lead_id)["gender"] == "M"
    env.sheet.set(number, "gender", "F")
    env.tick(full=True)
    env.tick(full=True)  # profile edits apply on the second full read, like any other field
    assert env.lead(lead_id)["gender"] == "F"


def test_an_unknown_sheet_gender_is_reported_on_the_row(env):
    number = env.start_row(gender="X")
    env.tick()
    assert "Gender must be M, F or Other" in env.cell(number, "outcome_status")


def test_restart_on_a_finished_cadence_runs_once_and_never_restarts_itself(env):
    # Owner, 2026-10-20: Restart on a "Cadence completed" row did nothing until staff cleared the cell.
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "outcome_status", "Cadence completed")  # the first cadence ended
    env.sheet.set(number, "action", "Restart cadence")
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence restarted"
    assert env.cell(number, "action") == "" and env.cell(number, "action_request_id") == ""
    planned = env.db.execute("select count(*) as n from outreach_events where lead_id=%s and status='planned'",
                             (lead_id,)).fetchone()["n"]
    assert planned > 0
    # The restarted cadence ends too: nothing runs by itself.
    env.sheet.set(number, "outcome_status", "Cadence completed")
    env.tick()
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence completed"
    assert _requests(env, lead_id, "restart_cadence") == 1


def test_a_patient_is_restarted_again_by_picking_restart_again(env):
    # Owner 2026-10-25: no clearing Outcome Status. The 10-minute cooldown still holds, and says when.
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    for runs in (1, 2, 3):
        env.sheet.set(number, "action", "Restart cadence")
        env.tick()
        assert env.cell(number, "outcome_status") == "Cadence restarted"
        assert env.cell(number, "action") == ""
        assert _requests(env, lead_id, "restart_cadence") == runs
        # Picked again at once: too soon. The row says until when; Action empties again.
        env.sheet.set(number, "action", "Restart cadence")
        env.tick()
        status = env.cell(number, "outcome_status")
        assert re.fullmatch(r"Error: restarted less than 10 minutes ago\. Pick Restart cadence again "
                            r"after \d{1,2}:\d\d [AP]M PT\.", status), status
        assert env.cell(number, "action") == "" and env.cell(number, "lead_status") == "In cadence"
        assert _requests(env, lead_id, "restart_cadence") == runs
        env.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                       "where lead_id=%s", (lead_id,))  # ten minutes later


def test_a_restart_while_a_call_is_going_out_says_to_pick_it_again(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    # A call placed, its result not in yet ('attempted'; the claim guard keeps tests off 'in_flight').
    env.db.execute("update outreach_events set status='attempted' where id=(select id from "
                   "outreach_events where lead_id=%s order by id limit 1)", (lead_id,))
    env.sheet.set(number, "action", "Restart cadence")
    env.tick()
    assert env.cell(number, "outcome_status") == (
        "Error: a call or text is going out right now. Pick Restart cadence again in a minute.")
    assert env.cell(number, "action") == ""
    env.db.execute("update outreach_events set status='delivered' where lead_id=%s and status='attempted'",
                   (lead_id,))
    env.sheet.set(number, "action", "Restart cadence")
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence restarted"


def test_a_restart_with_another_phone_in_the_row_is_refused_and_emptied(env):
    number = env.start_row()
    env.tick()
    env.sheet.set(number, "phone", _phone())
    env.sheet.set(number, "action", "Restart cadence")
    env.tick()
    assert env.cell(number, "outcome_status") == "Error: Lead ID and phone number do not match"
    assert env.cell(number, "action") == "" and env.cell(number, "action_request_id") == ""


def test_a_command_staff_pick_while_the_result_is_written_is_kept_and_runs(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "action", "Restart cadence")
    # Fires on the second pre-write read: after the Restart ran, before its result is written.
    env.sheet.before_write = lambda: setattr(
        env.sheet, "before_write", lambda: env.sheet.set(number, "action", "Booked"))
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence restarted"
    assert env.cell(number, "action") == "Booked"  # not wiped
    assert env.cell(number, "action_request_id") == ""
    env.tick()
    assert env.lead(lead_id)["status"] == "booked" and env.cell(number, "outcome_status") == "Booked"
    assert env.cell(number, "action") == ""


def test_a_result_for_a_lead_missing_from_the_sheet_is_retried_not_lost(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "lead_id", "")  # staff cleared it by mistake
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="cadence_finished",
                             source_key="test")
    env.tick()
    row = env.db.execute("select status,last_error from integration_outbox where aggregate_id=%s "
                         "and event_type='sheet.cadence_finished'", (lead_id,)).fetchone()
    assert row["status"] == "pending" and "not found" in row["last_error"]


def test_a_renamed_column_stops_the_sync_and_says_why(env):
    env.sheet.values[1][env.sheet.values[1].index("Outcome Status")] = "Status"
    env.start_row()
    assert env.tick()["claimed"] == 0
    beat = env.db.execute("select details from service_heartbeats where service='sheet-worker'"
                          ).fetchone()["details"]
    assert beat["ok"] is False and "Outcome Status" in beat["error"]
    assert env.leads() == []


def test_a_row_inserted_while_a_command_is_claimed_never_hits_the_wrong_patient(env):
    first = env.start_row()
    env.sheet.before_write = lambda: env.sheet.insert_row_above(first)  # staff insert a row
    env.tick()
    assert env.leads() == [] and env.sheet.writes == []  # nothing claimed, nothing run
    env.tick()  # next poll reads the new layout and runs the command on the moved row
    moved = first + 1
    assert env.cell(moved, "outcome_status") == "Cadence started"
    assert env.cell(first, "outcome_status") == "" and len(env.leads()) == 1


def test_recovery_replays_the_stored_result_even_after_staff_fixed_the_name(env):
    # Review 2026-10-14: rebuilding from the edited row gave "Request ID was reused" and the
    # Sheet never learnt the Lead ID of a lead that was already being called.
    phone = _phone()
    request_id = uuid4()
    execute_lead_action(request_id=request_id, action="start_cadence", lead_id=None, lead={
        "full_name": "Asha Rao", "phone": "+1" + phone, "email": "asha@example.test",
        "date_of_birth": date(1990, 1, 16), "location": "Dana Point", "title": "Ortho",
        "lead_type": "Ortho"})
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    number = env.start_row(phone=phone, name="Asha Rao-Kumar", action_started_at=old,
                           outcome_status="Processing: Start cadence",
                           action_request_id=str(request_id))
    env.tick()
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "lead_id") == str(env.leads()[0]["id"]) and len(env.leads()) == 1


def test_a_row_copied_with_the_old_request_id_is_its_own_person(env):
    # Review 2026-10-14: a copied row for a family member must not inherit the other's lead.
    phone = _phone()
    first = env.start_row(phone=phone)
    env.tick()
    request_id = env.db.execute("select request_id::text as r from lead_action_requests where "
                                "lead_id::text=%s", (env.cell(first, "lead_id"),)).fetchone()["r"]
    copy = env.start_row(phone=phone, name="Ravi Rao", action_request_id=request_id)
    env.tick()
    assert env.cell(copy, "outcome_status") == "Cadence started"
    assert env.cell(copy, "lead_id") not in ("", env.cell(first, "lead_id"))
    assert env.lead(env.cell(copy, "lead_id"))["full_name"] == "Ravi Rao"


def test_a_start_row_rerun_after_a_lost_result_links_the_lead_instead_of_starting_twice(env):
    # A row from before 2026-10-25 whose result never reached the Sheet; staff cleared the status.
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    request_id = env.db.execute("select request_id::text as r from lead_action_requests where "
                                "lead_id::text=%s", (lead_id,)).fetchone()["r"]
    for key, value in {"lead_id": "", "outcome_status": "", "action": "Start cadence",
                       "action_request_id": request_id}.items():
        env.sheet.set(number, key, value)
    env.tick()
    assert env.cell(number, "lead_id") == lead_id and len(env.leads()) == 1
    assert env.cell(number, "outcome_status") == "Cadence started"
    assert env.cell(number, "action") == ""


def test_action_not_from_the_drop_down_says_so_and_empties_and_nothing_runs(env):
    # Owner, 2026-10-26: free text sat in Action without a word; "start cadence" was ignored. Action
    # must be chosen from the drop-down: another case of a command is not one.
    unknown = "Error: action must be chosen from the drop-down."
    new = env.start_row(action="please call")
    env.tick()
    assert (env.cell(new, "outcome_status"), env.cell(new, "action")) == (unknown, "")
    assert env.leads() == []
    env.sheet.set(new, "action", " start  CADENCE ")
    env.tick()
    assert (env.cell(new, "outcome_status"), env.cell(new, "action")) == (unknown, "")
    assert env.leads() == []
    env.sheet.set(new, "action", " Start  cadence ")
    env.tick()
    assert env.cell(new, "outcome_status") == "Cadence started" and env.cell(new, "action") == ""
    lead_id = env.cell(new, "lead_id")
    before = env.lead(lead_id)
    for text in ("restart", "restart cadence"):  # linked row
        env.sheet.set(new, "action", text)
        env.tick()
        assert (env.cell(new, "outcome_status"), env.cell(new, "action")) == (unknown, "")
    after = env.lead(lead_id)
    assert (after["status"], after["cadence_state"]) == (before["status"], before["cadence_state"])
    assert env.db.execute("select count(*) n from lead_action_requests where lead_id::text=%s",
                          (lead_id,)).fetchone()["n"] == 1  # only the Start
    noted = env.db.execute("select reason from lead_status_history where lead_id=%s and "
                           "source='n8n_sheet' order by changed_at", (lead_id,)).fetchall()
    assert [r["reason"] for r in noted][-2:] == [
        '"restart" refused: action must be chosen from the drop-down.',
        '"restart cadence" refused: action must be chosen from the drop-down.']


def test_only_one_sheet_worker_polls_at_a_time(env):
    holder = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
    try:
        holder.execute("select pg_advisory_lock(hashtext('rpt:sheet-direct'))")
        env.start_row()
        assert env.tick().get("locked") == 1 and env.leads() == []
    finally:
        holder.close()
    env.tick()
    assert len(env.leads()) == 1


# --- narrow polls (no full read) ------------------------------------------------------------


def test_a_narrow_poll_runs_a_new_command_without_reading_the_whole_tab(env):
    for _ in range(50):  # a busy Sheet: finished rows the narrow poll must not load
        env.sheet.add(name="Old Patient", phone=_phone(), action="", outcome_status="Booked",
                      lead_id=str(uuid4()))
    number = env.start_row()
    env.tick(full=False)
    assert env.sheet.reads == 0  # never the whole tab
    assert env.cell(number, "outcome_status") == "Cadence started" and len(env.leads()) == 1


def test_a_narrow_poll_writes_results_and_recovers_stuck_rows(env):
    number = env.start_row()
    env.tick(full=False)
    lead_id = env.cell(number, "lead_id")
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="cadence_finished",
                             source_key="narrow")
    env.db.execute("update leads set cadence_state='completed' where id=%s", (lead_id,))
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    stuck = env.start_row(outcome_status="Processing: Start cadence",
                          action_request_id=str(uuid4()), action_started_at=old)
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == "Cadence completed"
    assert env.cell(stuck, "outcome_status") == "Cadence started" and len(env.leads()) == 2
    assert env.sheet.reads == 0


def test_profile_edits_wait_for_the_full_read(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.sheet.set(number, "email", "later@example.test")
    env.tick(full=False)
    env.tick(full=False)
    assert env.lead(lead_id)["email"] == "asha@example.test"
    env.tick(full=True)
    env.tick(full=True)
    assert env.lead(lead_id)["email"] == "later@example.test"


def test_a_half_edited_row_is_never_sent_as_a_name_change(env):
    # Staff retype a row over two full reads: the name first, the corrected phone later.
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    env.tick()
    env.sheet.set(number, "name", "Asha R")       # caught mid-edit by one full read
    env.tick()
    env.sheet.set(number, "name", "Asha Rao")     # finished: back to the stored name
    env.tick()
    env.tick()
    assert env.lead(lead_id)["needs_review"] is False
    assert env.cell(number, "outcome_status") == "Cadence started"


def test_a_command_waits_until_the_row_stops_changing(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "sheet_settle_seconds", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sheet_sync_worker, "_clock", lambda: clock["t"])
    number = env.start_row(date_of_birth="")  # Action picked before the row is finished
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == ""  # no error yet: staff are still typing
    clock["t"] += 30
    env.sheet.set(number, "date_of_birth", "16/01/1990")  # finished 30 s later
    env.tick(full=False)
    clock["t"] += 45
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == "" and env.leads() == []  # 45 s still: wait
    clock["t"] += 20
    env.tick(full=False)
    assert env.cell(number, "outcome_status") == "Cadence started" and len(env.leads()) == 1


def test_a_pasted_block_is_worked_through_a_few_rows_per_poll(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "sheet_max_commands_per_poll", 3)
    numbers = [env.start_row(name=f"Patient {i}") for i in range(7)]
    env.tick(full=False)
    assert len(env.leads()) == 3
    env.tick(full=False)
    env.tick(full=False)
    assert len(env.leads()) == 7
    assert all(env.cell(n, "outcome_status") == "Cadence started" for n in numbers)


# --- nightly archive ------------------------------------------------------------------------


def _finished(env, number, *, status="booked", days_ago=40, review=False):
    lead_id = env.cell(number, "lead_id")
    env.db.execute(
        "update leads set status=%s,cadence_state='completed',needs_review=%s,"
        "status_changed_at=now()-make_interval(days=>%s) where id=%s",
        (status, review, days_ago, lead_id))
    return lead_id


def _archive(env, days=30):
    grid = parse_grid(env.sheet.read_values(), 2)
    trace = WorkflowTrace("t", "t")
    return sheet_direct.archive(env.sheet, grid, days, "Archive", trace)


def test_finished_quiet_rows_move_to_the_archive_and_others_stay(env):
    done = env.start_row(name="Done Patient")
    recent = env.start_row(name="Recent Patient")
    review = env.start_row(name="Review Patient")
    active = env.start_row(name="Active Patient")
    dnc = env.start_row(name="Dnc Patient")
    env.tick()
    done_id = _finished(env, done)
    _finished(env, recent, days_ago=3)
    _finished(env, review, review=True)
    _finished(env, dnc, status="do_not_contact")
    active_id = env.cell(active, "lead_id")
    assert _archive(env) == 2
    names = [row[0] for row in env.sheet.values[2:]]
    assert names == ["Recent Patient", "Review Patient", "Active Patient"]
    archived_names = [row[0] for row in env.sheet.archive[2:]]
    assert sorted(archived_names) == ["Dnc Patient", "Done Patient"]
    assert env.sheet.archive[1] == env.sheet.values[1]  # same headers
    state = env.db.execute("select archived_at from sheet_row_state where lead_id=%s",
                           (done_id,)).fetchone()
    assert state["archived_at"] is not None
    # A later update for an archived lead is dropped quietly, not retried as "not found".
    with env.db.transaction():
        enqueue_sheet_update(env.db, lead_id=done_id, event_type="lead_booked", source_key="late")
        enqueue_sheet_update(env.db, lead_id=active_id, event_type="call_settled", source_key="x")
    env.tick()
    row = env.db.execute("select status from integration_outbox where aggregate_id=%s "
                         "and event_type='sheet.lead_booked'", (done_id,)).fetchone()
    assert row["status"] == "delivered"


def test_a_row_that_moved_before_the_archive_is_left_alone(env):
    done = env.start_row(name="Done Patient")
    env.tick()
    _finished(env, done)
    env.sheet.before_write = lambda: env.sheet.insert_row_above(done)
    assert _archive(env) == 0 and env.sheet.archive is None


def test_the_archive_runs_once_in_its_hour_and_only_when_enabled(env, monkeypatch):
    settings = get_settings()
    at = {"now": datetime(2026, 10, 15, 3, 10, tzinfo=sheet_direct.SHEET_TZ)}
    monkeypatch.setattr(sheet_sync_worker, "_pacific_now", lambda: at["now"])
    assert not sheet_sync_worker._archive_due(settings)  # off by default
    monkeypatch.setattr(settings, "sheet_archive_enabled", True)
    assert sheet_sync_worker._archive_due(settings)
    sheet_sync_worker._direct_state["archived_on"] = at["now"].date()
    assert not sheet_sync_worker._archive_due(settings)  # already ran today
    at["now"] = at["now"].replace(hour=4)
    sheet_sync_worker._direct_state["archived_on"] = None
    assert not sheet_sync_worker._archive_due(settings)  # outside the hour


# --- Lead Status mirrors the dashboard (2026-10-15) --------------------------------------------


def test_pause_resume_and_review_show_in_the_sheets_lead_status(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")
    assert env.cell(number, "lead_status") == "In cadence"
    env.db.execute("update leads set cadence_state='paused' where id=%s", (lead_id,))  # dashboard pause
    env.tick(full=False)
    assert env.cell(number, "lead_status") == "Paused"
    env.db.execute("update leads set cadence_state='active' where id=%s", (lead_id,))  # resume
    env.tick(full=False)
    assert env.cell(number, "lead_status") == "In cadence"
    env.db.execute("update leads set needs_review=true,status='needs_attention' where id=%s", (lead_id,))
    env.tick(full=False)
    assert env.cell(number, "lead_status") == "Needs Review"
    env.db.execute("update leads set needs_review=false,status='in_progress' where id=%s", (lead_id,))
    env.tick(full=False)
    assert env.cell(number, "lead_status") == "In cadence"  # a resolved review no longer sticks


def test_the_stage_trigger_only_queues_sheet_leads_and_only_on_real_changes(env):
    number = env.start_row()
    env.tick()
    lead_id = env.cell(number, "lead_id")

    def queued():
        return env.db.execute("select count(*) n from integration_outbox where aggregate_id=%s "
                              "and event_type='sheet.stage_changed'", (lead_id,)).fetchone()["n"]

    before = queued()
    env.db.execute("update leads set email='x@example.test' where id=%s", (lead_id,))
    env.db.execute("update leads set cadence_state=cadence_state where id=%s", (lead_id,))
    assert queued() == before  # no stage change, nothing queued
    env.db.execute("update leads set cadence_state='paused' where id=%s", (lead_id,))
    assert queued() == before + 1
    env.db.execute("update leads set source_system='dashboard' where id=%s", (lead_id,))
    env.db.execute("update leads set cadence_state='active' where id=%s", (lead_id,))
    assert queued() == before + 1  # dashboard leads are not in the Sheet


# --- "Already booked" needs the same case (2026-10-15) -----------------------------------------


@pytest.fixture
def cases(env):
    for name, aliases, default in (("Physical Therapy", ["ortho", "orthopedic"], True),
                                   ("Physical Therapy - Pelvic", ["pelvic", "pelvic floor"], False)):
        env.db.execute("insert into case_types(practice_id,name,stride_appointment_type_id,"
                       "duration_minutes,is_default,aliases) values(%s,%s,1452,60,%s,%s)",
                       (env.practice_id, name, default, aliases))
    yield
    env.db.execute("delete from case_types where practice_id=%s", (env.practice_id,))


def _booked(env, **cells):
    number = env.start_row(**cells)
    env.tick()
    env.db.execute("update leads set status='booked',cadence_state='completed',"
                   "status_changed_at=now() where id=%s", (env.cell(number, "lead_id"),))
    return number


@pytest.mark.parametrize(("first", "again", "expected"), [
    ("Ortho", "Ortho", "Already booked"),
    ("Ortho", "orthopedic", "Already booked"),            # same case, another name for it
    ("Ortho", "Physical Therapy", "Already booked"),
    ("Pelvic", "Pelvic Floor", "Already booked"),
    ("Ortho", "Pelvic", "Cadence started"),               # different case: call them
    ("Pelvic", "Ortho", "Cadence started"),
    ("Ortho", "Knee", "Cadence started"),                 # unknown case: reach out, don't skip
])
def test_a_booked_patient_is_skipped_only_for_the_same_case(env, cases, first, again, expected):
    phone = _phone()
    _booked(env, phone=phone, case=first)
    number = env.start_row(phone=phone, case=again)
    env.tick()
    assert env.cell(number, "outcome_status") == expected
    leads = env.leads()
    assert len(leads) == (1 if expected == "Already booked" else 2)
    if expected == "Cadence started":
        assert leads[-1]["status"] == "in_progress" and leads[-1]["lead_type"] == again
        assert env.lead(leads[0]["id"])["status"] == "booked"  # the first booking is untouched


# --- Action: staff pick a command, it runs, the cell empties (owner, 2026-10-25) -----------------


def _requests(env, lead_id, action):
    return env.db.execute(
        "select count(*) as n from lead_action_requests where lead_id::text=%s and action=%s "
        "and status='completed'", (lead_id, action)).fetchone()["n"]


def _command(env, number, action):
    env.sheet.set(number, "action", action)
    env.tick()
    assert env.cell(number, "action") == ""
    return env.cell(number, "lead_id")


@pytest.mark.parametrize("after", ["continue", "restart"])
def test_an_unblock_never_blocks_again_and_do_not_contact_picked_again_does(env, after):
    from rpt_agent.services.number_block import unblock_number

    number = env.start_row()
    env.tick()
    lead_id = _command(env, number, "Do Not Contact")
    assert env.lead(lead_id)["status"] == "do_not_contact"
    with env.db.transaction():
        lead = env.db.execute("select * from leads where id=%s for update", (lead_id,)).fetchone()
        unblock_number(env.db, lead, after, "Admin")
    for _ in range(3):
        env.tick()
    text = "Cadence resumed in dashboard" if after == "continue" else "Cadence restarted"
    assert (env.cell(number, "action"), env.cell(number, "outcome_status")) == ("", text)
    assert env.lead(lead_id)["status"] != "do_not_contact"
    assert _requests(env, lead_id, "do_not_contact") == 1
    # Staff block it again: pick Do Not Contact again.
    _command(env, number, "Do Not Contact")
    assert env.cell(number, "outcome_status") == "Do Not Contact applied"
    assert env.lead(lead_id)["status"] == "do_not_contact"
    assert _requests(env, lead_id, "do_not_contact") == 2


def test_do_not_contact_picked_again_after_an_administration_unblock_blocks_the_number(env):
    # Owner, 2026-10-26: Administration releases the number and leaves the lead Do Not Contact.
    # Do Not Contact picked again used to say "applied" without blocking, so a new row was called.
    from rpt_agent.services.number_block import unblock_number_only

    number = env.start_row()
    env.tick()
    lead_id = _command(env, number, "Do Not Contact")
    phone = env.lead(lead_id)["phone_e164"]
    with env.db.transaction():
        unblock_number_only(env.db, phone)
    env.tick()
    blocks = "select count(*) as n from suppressed_numbers where phone_e164=%s"
    assert env.db.execute(blocks, (phone,)).fetchone()["n"] == 0
    for _ in range(2):
        _command(env, number, "Do Not Contact")
        assert env.cell(number, "outcome_status") == "Do Not Contact applied"
        assert env.db.execute(blocks, (phone,)).fetchone()["n"] == 1
    assert env.lead(lead_id)["status"] == "do_not_contact"
    other = env.start_row(phone=phone[2:], name="Ravi Rao")
    env.tick()
    assert env.cell(other, "outcome_status") == "Number blocked; needs review"


def _stride_cancel(env, lead_id):
    """What stride_leads does when the booked visit is cancelled in Stride."""
    from rpt_agent.services.review import record_review
    from rpt_agent.services.stride_leads import REASON_CANCELLED, REVIEW_CODES

    reason = REASON_CANCELLED
    with env.db.transaction():
        env.db.execute("update leads set status='needs_attention',needs_review=true,review_reason=%s,"
                       "review_flagged_at=now(),status_changed_at=now() where id=%s", (reason, lead_id))
        record_review(env.db, lead_id, REVIEW_CODES[reason], reason, source="stride")
        enqueue_sheet_update(env.db, lead_id=lead_id, event_type="stride_booking_cancelled",
                             source_key=uuid4().hex)


def test_a_stride_cancel_does_not_book_again_and_booked_picked_again_does(env):
    number = env.start_row()
    env.tick()
    lead_id = _command(env, number, "Booked")
    assert env.cell(number, "outcome_status") == "Booked"
    _stride_cancel(env, lead_id)
    for _ in range(3):
        env.tick()
    assert env.cell(number, "outcome_status") == "Needs review: appointment cancelled in Stride"
    lead = env.lead(lead_id)
    assert (lead["status"], lead["needs_review"]) == ("needs_attention", True)
    assert _requests(env, lead_id, "booked") == 1
    # Booked runs on a row in review, and closes the review.
    _command(env, number, "Booked")
    assert env.lead(lead_id)["status"] == "booked" and _requests(env, lead_id, "booked") == 2
    assert env.cell(number, "outcome_status") == "Booked"


def test_a_restart_picked_on_a_booked_row_is_refused(env):
    # Owner 2026-10-25: the Sheet never undoes a booking (n8n mode still allows it); the dashboard
    # is the place to reopen a booked lead. Action empties as usual.
    number = env.start_row()
    env.tick()
    lead_id = _command(env, number, "Booked")
    _command(env, number, "Restart cadence")
    assert env.cell(number, "outcome_status") == (
        "Error: this lead is booked. Reopen it on the dashboard if needed.")
    assert env.cell(number, "lead_status") == "Booked"
    assert env.lead(lead_id)["status"] == "booked"
    assert _requests(env, lead_id, "restart_cadence") == 0


def test_a_closed_lead_refuses_a_restart_and_the_number_stays_with_the_newer_row(env):
    # Tester 2026-10-23: a cleared "Closed: a newer row has this phone number" restarted the old lead.
    phone = _phone()
    first = env.start_row(phone=phone)
    env.tick()
    _command(env, first, "Restart cadence")
    second = env.start_row(phone=phone, name="Asha Rao")
    env.tick()
    env.tick()
    assert env.cell(second, "outcome_status") == "Cadence started"
    assert env.cell(first, "outcome_status") == "Closed: a newer row has this phone number"
    env.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                   "where lead_id=%s", (env.cell(first, "lead_id"),))  # not the cooldown
    _command(env, first, "Restart cadence")
    assert env.cell(first, "outcome_status") == (
        "Error: this lead is closed (a newer row has this phone number). Use the dashboard.")
    assert env.lead(env.cell(second, "lead_id"))["status"] == "in_progress"


def _action_writes(env):
    """Cells the worker wrote into the Action column."""
    from fake_sheet import HEADERS

    from rpt_agent.services.google_sheet import column_letter

    letter = column_letter(HEADERS.index("Action"))
    return [(cell, text) for batch in env.sheet.writes for cell, text in batch
            if re.fullmatch(letter + r"\d+", cell)]


def test_a_lead_booked_outside_the_sheet_writes_no_booked_into_action(env):
    number = env.start_row()
    env.tick()
    lead_id = _command(env, number, "Restart cadence")
    assert env.cell(number, "outcome_status") == "Cadence restarted"
    env.db.execute("update leads set status='booked',cadence_state='completed',status_changed_at=now() "
                   "where id=%s", (lead_id,))
    enqueue_sheet_update(env.db, lead_id=lead_id, event_type="lead_booked", source_key=uuid4().hex)
    env.tick()
    assert (env.cell(number, "outcome_status"), env.cell(number, "action")) == ("Booked", "")


def test_the_worker_only_ever_empties_the_action_column(env):
    from rpt_agent.services.number_block import unblock_number

    number = env.start_row()
    env.tick()
    lead_id = _command(env, number, "Do Not Contact")
    with env.db.transaction():
        lead = env.db.execute("select * from leads where id=%s for update", (lead_id,)).fetchone()
        unblock_number(env.db, lead, "restart", "Admin")
    env.tick()
    _command(env, number, "Booked")
    _stride_cancel(env, lead_id)
    env.tick()
    env.tick()
    writes = _action_writes(env)
    assert writes and {text for _, text in writes} == {""}


# --- rows from before 2026-10-25: an already handled command left in Action ------------------------


def _request(env, lead_id, action):
    return env.db.execute(
        "select request_id::text as r from lead_action_requests where lead_id::text=%s and action=%s "
        "order by created_at desc limit 1", (lead_id, action)).fetchone()["r"]


@pytest.mark.parametrize("full", [True, False])
def test_on_the_first_poll_old_commands_are_emptied_and_never_run(env, full):
    rows = {}
    for name in ("Booked Patient", "Restart Patient", "Dnc Patient", "Start Patient", "Cleared Patient"):
        rows[name] = env.start_row(name=name)
    env.tick()
    lead = {name: env.cell(number, "lead_id") for name, number in rows.items()}
    for name in ("Restart Patient", "Cleared Patient"):
        _command(env, rows[name], "Restart cadence")
    _command(env, rows["Dnc Patient"], "Do Not Contact")
    from rpt_agent.services.number_block import unblock_number

    with env.db.transaction():
        found = env.db.execute("select * from leads where id=%s for update",
                               (lead["Dnc Patient"],)).fetchone()
        unblock_number(env.db, found, "continue", "Admin")
    env.tick()
    # The rows as the code before 2026-10-25 left them.
    old_rows = {
        # Written by the backend before 2026-10-23 next to the Start's request.
        "Booked Patient": ("Booked", "Booked", _request(env, lead["Booked Patient"], "start_cadence")),
        # Staff's own commands, still in the cell, the row's last request.
        "Restart Patient": ("Restart cadence", ("Cadence restarted; Restart cadence in Action already "
                                                "ran (clear this cell to run it again)"),
                            _request(env, lead["Restart Patient"], "restart_cadence")),
        "Dnc Patient": ("Do Not Contact", "Cadence resumed in dashboard",
                        _request(env, lead["Dnc Patient"], "do_not_contact")),
        "Start Patient": ("Start cadence", "Cadence started",
                          _request(env, lead["Start Patient"], "start_cadence")),
    }
    env.db.execute("update leads set status='booked',cadence_state='completed' where id=%s",
                   (lead["Booked Patient"],))
    for name, (action, status, request_id) in old_rows.items():
        for key, value in {"action": action, "outcome_status": status,
                           "action_request_id": request_id}.items():
            env.sheet.set(rows[name], key, value)
    # Under the old rules a cleared Outcome Status ran the command again: it still does.
    env.sheet.set(rows["Cleared Patient"], "action", "Restart cadence")
    env.sheet.set(rows["Cleared Patient"], "outcome_status", "")
    env.sheet.set(rows["Cleared Patient"], "action_request_id",
                  _request(env, lead["Cleared Patient"], "restart_cadence"))
    env.db.execute("update lead_status_history set changed_at=changed_at-interval '11 minutes' "
                   "where lead_id=%s", (lead["Cleared Patient"],))
    before = env.db.execute("select count(*) as n from lead_action_requests where practice_id=%s",
                            (str(env.practice_id),)).fetchone()["n"]
    states = {name: env.lead(lead[name])["status"] for name in old_rows}
    env.tick(full=full)
    for name, (_, status, _) in old_rows.items():
        number = rows[name]
        assert env.cell(number, "action") == "" and env.cell(number, "action_request_id") == ""
        assert env.cell(number, "outcome_status") == status.split(";")[0]  # result kept, old words gone
        assert env.lead(lead[name])["status"] == states[name]
    after = env.db.execute("select count(*) as n from lead_action_requests where practice_id=%s",
                           (str(env.practice_id),)).fetchone()["n"]
    assert after == before + 1  # only the cleared Restart ran
    assert env.cell(rows["Cleared Patient"], "outcome_status") == "Cadence restarted"
    assert env.cell(rows["Cleared Patient"], "action") == ""
    # Emptied once: later polls change nothing, and the same commands picked again run.
    writes = len(env.sheet.writes)
    env.tick(full=full)
    assert len(env.sheet.writes) == writes
    _command(env, rows["Dnc Patient"], "Do Not Contact")
    assert env.lead(lead["Dnc Patient"])["status"] == "do_not_contact"
