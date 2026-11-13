"""SMS agent inbound side on a real schema (migrations applied, including 054).

Needs TEST_DATABASE_URL (a disposable local database, never the .env one). The webhook handler and
the sms-agent-worker run for real against it; Twilio and the agent are fakes.
"""

from __future__ import annotations

import os
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

import rpt_agent.services as rpt_services
from rpt_agent import sms_agent_worker
from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.routes import twilio as twilio_routes
from rpt_agent.services import sms_agent_texts as real_texts
from rpt_agent.services import sms_inbound
from rpt_agent.services.provider_http import ProviderError
from rpt_agent.services.review import RESTARTED_REASON

pytestmark = pytest.mark.integration


@dataclass
class _Reply:
    texts: list[str] = field(default_factory=lambda: ["Hi from Elliot"])
    actions: list = field(default_factory=list)
    no_reply: bool = False
    error: str | None = None
    opted_out: bool = False


class _Twilio:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_sms(self, trace, to, body):
        self.sent.append((to, body))
        return f"SMai{uuid4().hex}"


@pytest.fixture
def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    slug = f"test-sms-{uuid4().hex[:10]}"
    for key, value in {"SUPABASE_DB_URL": url, "SMS_AGENT_ENABLED": "true",
                       "SMS_AGENT_TEST_LEADS_ONLY": "true", "SMS_AGENT_QUIET_SECONDS": "15",
                       "SMS_AGENT_DAILY_REPLY_CAP": "3", "STRIDE_PRACTICE_SLUG": slug}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_pool.cache_clear()
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic',%s,'America/Los_Angeles') "
        "returning id", (slug,)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id,booking_link_url) values(%s,%s)",
                 (practice, "https://book.example.test"))
    replies: list = []
    seen: dict = {}

    def reply_live(trace, *, lead_id, job_id, settings=None, client=None, providers=None):
        seen.setdefault("calls", []).append((lead_id, job_id))
        reply = replies.pop(0) if replies else _Reply()
        # A callable stands for what the agent's tools did to the database during the run.
        return reply(lead_id) if callable(reply) else reply

    fake_agent = types.ModuleType("rpt_agent.services.sms_agent")
    fake_agent.reply_live = reply_live
    monkeypatch.setitem(sys.modules, "rpt_agent.services.sms_agent", fake_agent)
    fake_texts = types.ModuleType("rpt_agent.services.sms_agent_texts")
    fake_texts.render = lambda conn, key, **values: f"<{key}> {values.get('booking_link')}"
    fake_texts.template_body = real_texts.template_body
    fake_texts.EMERGENCY_TEXT = real_texts.EMERGENCY_TEXT
    fake_texts.is_emergency_text = real_texts.is_emergency_text
    fake_texts.parse_card = real_texts.parse_card
    monkeypatch.setitem(sys.modules, "rpt_agent.services.sms_agent_texts", fake_texts)
    # `from .services import sms_agent_texts` reads the package attribute first: once any earlier
    # test imported the real module, only replacing it there swaps it.
    monkeypatch.setattr(rpt_services, "sms_agent_texts", fake_texts, raising=False)
    phones: list[str] = []

    class Env:
        db = conn
        practice_id = practice
        twilio = _Twilio()
        agent_replies = replies
        agent_calls = seen

        def phone(self):
            phone = f"+1555{uuid4().int % 10_000_000:07d}"
            phones.append(phone)
            return phone

        def lead(self, phone, *, is_test=True, **cols):
            values = {"practice_id": practice, "full_name": "Test Lead", "first_name": "Test",
                      "phone_e164": phone, "is_test": is_test, "status": "in_progress",
                      "cadence_state": "active"}
            values.update(cols)
            names = ",".join(values)
            marks = ",".join(["%s"] * len(values))
            return str(conn.execute(f"insert into leads({names}) values({marks}) returning id",
                                    tuple(values.values())).fetchone()["id"])

        def text(self, phone, body="hello", sid=None, num_media=0):
            twilio_routes._handle_inbound_sms(WorkflowTrace("t", "test"), phone, body,
                                              sid or f"SM{uuid4().hex}", num_media)

        def jobs(self, phone):
            return conn.execute("select * from sms_agent_jobs where phone_e164=%s order by id",
                                (phone,)).fetchall()

        def make_due(self, phone):
            conn.execute("update sms_agent_jobs set due_at=now()-interval '1 second' "
                         "where phone_e164=%s and status='pending'", (phone,))

        def tick(self):
            providers = types.SimpleNamespace(twilio=self.twilio)
            return sms_agent_worker.run_tick(providers, get_settings())

        def ai_rows(self, lead_id):
            return conn.execute("select * from sms_messages where lead_id=%s and sender='ai' "
                                "order by id", (lead_id,)).fetchall()

    yield Env()

    conn.execute("delete from sms_agent_jobs where phone_e164=any(%s)", (phones,))
    conn.execute("delete from suppressed_numbers where phone_e164=any(%s)", (phones,))
    lead_ids = [r["id"] for r in conn.execute("select id from leads where practice_id=%s",
                                               (practice,)).fetchall()]
    conn.execute("delete from lead_reviews where lead_id=any(%s)", (lead_ids,))
    conn.execute("delete from lead_status_history where lead_id=any(%s)", (lead_ids,))
    conn.execute("delete from sms_messages where lead_id=any(%s)", (lead_ids,))
    conn.execute("delete from integration_outbox where aggregate_id=any(%s)",
                 ([str(i) for i in lead_ids],))
    conn.execute("delete from leads where practice_id=%s", (practice,))
    conn.execute("delete from practice_settings where practice_id=%s", (practice,))
    conn.execute("delete from practices where id=%s", (practice,))
    conn.close()
    get_pool.cache_clear()
    get_settings.cache_clear()


def test_three_quick_texts_make_one_job_answered_once(db):
    phone = db.phone()
    lead = db.lead(phone)
    for body in ("hi", "is this the clinic", "can I book"):
        db.text(phone, body)
    jobs = db.jobs(phone)
    assert len(jobs) == 1 and jobs[0]["status"] == "pending"
    last = db.db.execute("select max(id) as id from sms_messages where lead_id=%s", (lead,)).fetchone()
    assert jobs[0]["last_inbound_id"] == last["id"]
    # Not due yet: the quiet window holds it.
    assert db.tick()["processed"] == 0
    db.make_due(phone)
    db.tick()
    assert db.agent_calls["calls"] == [(lead, jobs[0]["id"])]
    assert len(db.twilio.sent) == 1 and len(db.ai_rows(lead)) == 1
    job = db.jobs(phone)[0]
    assert (job["status"], job["outcome"]) == ("done", "replied")
    assert job["reply_message_id"] == db.ai_rows(lead)[0]["id"]
    inbound = db.db.execute("select sender from sms_messages where lead_id=%s and direction='inbound'",
                            (lead,)).fetchall()
    assert {r["sender"] for r in inbound} == {"patient"}


def test_a_new_text_pushes_the_pending_job_out(db):
    phone = db.phone()
    db.lead(phone)
    db.text(phone, "one")
    db.db.execute("update sms_agent_jobs set due_at=now()+interval '2 seconds' where phone_e164=%s",
                  (phone,))
    db.text(phone, "two")
    job = db.db.execute("select due_at-now() as wait from sms_agent_jobs where phone_e164=%s",
                        (phone,)).fetchone()
    assert job["wait"].total_seconds() > 10


def test_the_same_message_sid_twice_stores_and_queues_once(db):
    phone = db.phone()
    lead = db.lead(phone)
    db.text(phone, "hello", sid="SMdup" + phone)
    db.text(phone, "hello", sid="SMdup" + phone)
    rows = db.db.execute("select id from sms_messages where lead_id=%s", (lead,)).fetchall()
    assert len(rows) == 1 and len(db.jobs(phone)) == 1


def test_a_text_while_a_job_runs_starts_the_next_job_which_waits_for_it(db):
    phone = db.phone()
    db.lead(phone)
    db.text(phone, "one")
    db.make_due(phone)
    first = sms_agent_worker.claim_job()
    assert first and first["phone_e164"] == phone
    db.text(phone, "two")
    db.make_due(phone)
    jobs = db.jobs(phone)
    assert [j["status"] for j in jobs] == ["running", "pending"]
    # One phone never has two jobs running.
    assert sms_agent_worker.claim_job() is None


def test_stop_is_stored_and_opts_out_with_no_job(db):
    phone = db.phone()
    lead = db.lead(phone)
    db.text(phone, "STOP")
    assert db.jobs(phone) == []
    assert db.db.execute("select sms_opt_out from leads where id=%s", (lead,)).fetchone()["sms_opt_out"]


def test_switched_off_stores_only(db, monkeypatch):
    monkeypatch.setenv("SMS_AGENT_ENABLED", "false")
    get_settings.cache_clear()
    phone = db.phone()
    lead = db.lead(phone)
    db.text(phone, "hello")
    assert db.jobs(phone) == []
    assert db.db.execute("select count(*) as n from sms_messages where lead_id=%s",
                         (lead,)).fetchone()["n"] == 1


def test_the_text_goes_to_the_active_lead_not_the_newest(db):
    phone = db.phone()
    active = db.lead(phone)
    # A newer lead on the number was closed when the older one was restarted.
    db.lead(phone, status="closed_no_response", cadence_state="terminated",
            status_reason=RESTARTED_REASON)
    db.text(phone, "hello")
    row = db.db.execute("select lead_id from sms_messages where provider_message_id like 'SM%%' "
                        "and lead_id in (select id from leads where phone_e164=%s)", (phone,)).fetchone()
    assert str(row["lead_id"]) == active
    assert str(db.jobs(phone)[0]["lead_id"]) == active


def test_a_booked_lead_beats_an_old_closed_one(db):
    phone = db.phone()
    booked = db.lead(phone, status="booked", cadence_state="completed")
    db.lead(phone, status="declined", cadence_state="terminated")
    assert sms_inbound.active_lead_id(db.db, phone) == booked


def test_a_test_lead_has_no_daily_cap(db):
    phone = db.phone()
    db.lead(phone)
    for _ in range(5):  # cap 3 in this fixture
        db.text(phone, "hello")
        db.make_due(phone)
        db.tick()
    assert len(db.twilio.sent) == 5


def test_a_non_test_lead_gets_nothing_while_test_leads_only(db):
    phone = db.phone()
    lead = db.lead(phone, is_test=False)
    db.text(phone, "hello")
    db.make_due(phone)
    db.tick()
    assert db.twilio.sent == [] and db.ai_rows(lead) == []
    assert db.jobs(phone)[0]["outcome"] == "not_test_lead"


def test_staff_texting_keeps_the_ai_quiet(db):
    phone = db.phone()
    lead = db.lead(phone)
    db.db.execute("insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,"
                  "sender) values(%s,'outbound','staff here',now()-interval '2 hours','sent','staff')",
                  (lead,))
    db.text(phone, "hello")
    db.make_due(phone)
    db.tick()
    assert db.twilio.sent == [] and db.jobs(phone)[0]["outcome"] == "staff_took_over"


def test_a_call_in_flight_puts_the_job_back_for_a_minute(db):
    phone = db.phone()
    lead = db.lead(phone)
    db.db.execute("insert into outreach_events(lead_id,channel,status,executed_at) "
                  "values(%s,'call','attempted',now())", (lead,))
    db.text(phone, "hello")
    db.make_due(phone)
    db.tick()
    job = db.jobs(phone)[0]
    assert db.twilio.sent == []
    assert (job["status"], job["outcome"]) == ("pending", "call_in_progress")
    wait = db.db.execute("select due_at-now() as w from sms_agent_jobs where id=%s",
                         (job["id"],)).fetchone()["w"].total_seconds()
    assert 50 < wait <= 60
    # The call ends: the next pass answers.
    db.db.execute("update outreach_events set status='delivered' where lead_id=%s", (lead,))
    db.make_due(phone)
    db.tick()
    assert len(db.twilio.sent) == 1


def test_the_daily_cap_sends_one_closing_text_and_flags_staff_once(db, monkeypatch):
    # Emma's test 2026-11-11: replies are counted (not text parts), the limit ends with one closing
    # text and the card, then nothing more that day. Real patients only.
    monkeypatch.setenv("SMS_AGENT_TEST_LEADS_ONLY", "false")
    get_settings.cache_clear()
    phone = db.phone()
    lead = db.lead(phone, is_test=False)
    for _ in range(3):  # SMS_AGENT_DAILY_REPLY_CAP=3 in this fixture
        db.text(phone, "hello")
        db.make_due(phone)
        db.tick()
    assert len(db.twilio.sent) == 3
    for _ in range(2):
        db.text(phone, "hello again")
        db.make_due(phone)
        db.tick()
    assert len(db.twilio.sent) == 4 and db.twilio.sent[-1][1].startswith("<reply_limit>")
    assert [j["outcome"] for j in db.jobs(phone)][-2:] == ["reply_cap", "reply_cap"]
    reviews = db.db.execute("select code from lead_reviews where lead_id=%s", (lead,)).fetchall()
    assert [r["code"] for r in reviews] == [sms_inbound.REVIEW_REPLY_CAP]
    assert db.db.execute("select needs_review from leads where id=%s", (lead,)).fetchone()["needs_review"]


def test_a_photo_only_gets_the_text_only_line(db):
    phone = db.phone()
    lead = db.lead(phone)
    db.text(phone, "", num_media=1)
    db.make_due(phone)
    db.tick()
    assert db.agent_calls.get("calls") is None
    assert db.twilio.sent[0][1].startswith("<text_only>")
    assert db.jobs(phone)[0]["outcome"] == "fixed_text" and len(db.ai_rows(lead)) == 1


def test_an_unknown_number_gets_one_fixed_text_a_day(db):
    phone = db.phone()
    for _ in range(2):
        db.text(phone, "hi who is this")
        db.make_due(phone)
        db.tick()
    assert db.agent_calls.get("calls") is None
    assert db.twilio.sent == [(phone, "<unknown_number> https://book.example.test")]
    assert [j["outcome"] for j in db.jobs(phone)] == ["unknown_number", "unknown_number_recent"]


def test_an_agent_error_flags_the_lead_and_sends_nothing(db):
    phone = db.phone()
    lead = db.lead(phone)
    db.agent_replies.append(_Reply(texts=[], error="timeout"))
    db.text(phone, "hello")
    db.make_due(phone)
    db.tick()
    assert db.twilio.sent == []
    job = db.jobs(phone)[0]
    assert (job["status"], job["outcome"]) == ("failed", "agent_error")
    review = db.db.execute("select code from lead_reviews where lead_id=%s", (lead,)).fetchone()
    assert review["code"] == sms_inbound.REVIEW_AGENT_FAILED


def test_a_suppressed_number_gets_nothing(db):
    phone = db.phone()
    db.lead(phone)
    db.db.execute("insert into suppressed_numbers(phone_e164,reason) values(%s,'test')", (phone,))
    db.text(phone, "hello")
    db.make_due(phone)
    db.tick()
    assert db.twilio.sent == [] and db.jobs(phone)[0]["outcome"] == "suppressed"


def test_a_job_cut_off_while_running_is_closed_never_rerun(db):
    phone = db.phone()
    db.lead(phone)
    db.text(phone, "hello")
    db.db.execute("update sms_agent_jobs set status='running',claimed_at=now()-interval '11 minutes' "
                  "where phone_e164=%s", (phone,))
    db.tick()
    job = db.jobs(phone)[0]
    assert (job["status"], job["outcome"]) == ("failed", "worker_stopped") and db.twilio.sent == []


def test_switched_off_closes_queued_jobs(db):
    phone = db.phone()
    db.lead(phone)
    db.text(phone, "hello")
    sms_agent_worker.skip_pending_when_off()
    assert db.jobs(phone)[0]["outcome"] == "agent_disabled"


def test_a_text_that_arrives_while_a_job_runs_is_answered_once(db):
    # Job A is claimed; "two" arrives and queues job B before A reads the history. A answers both
    # texts, so B finds nothing new: never two replies to the same text.
    phone = db.phone()
    lead = db.lead(phone)
    db.text(phone, "one")
    db.make_due(phone)
    first = sms_agent_worker.claim_job()
    db.text(phone, "two")
    two = db.db.execute("select max(id) as id from sms_messages where lead_id=%s",
                        (lead,)).fetchone()["id"]
    sms_agent_worker.process_job(WorkflowTrace("t", "test"), first,
                                 types.SimpleNamespace(twilio=db.twilio), get_settings())
    assert db.jobs(phone)[0]["last_inbound_id"] == two
    db.make_due(phone)
    db.tick()
    assert len(db.twilio.sent) == 1 and len(db.agent_calls["calls"]) == 1
    assert [(j["status"], j["outcome"]) for j in db.jobs(phone)] == [
        ("done", "replied"), ("skipped", "nothing_to_answer")]


def test_stop_from_a_number_that_is_no_lead_stops_the_unknown_number_text(db):
    phone = db.phone()
    db.text(phone, "hi who is this")
    db.text(phone, "STOP")
    assert db.jobs(phone)[0]["outcome"] == "opted_out"
    assert db.db.execute("select 1 from suppressed_numbers where phone_e164=%s",
                         (phone,)).fetchone()
    db.text(phone, "hello?")  # tomorrow: still nothing
    db.make_due(phone)
    db.tick()
    assert db.twilio.sent == [] and db.jobs(phone)[-1]["outcome"] == "suppressed"


def test_an_agent_error_on_a_booked_lead_opens_a_card_and_keeps_the_booking(db):
    phone = db.phone()
    lead = db.lead(phone, status="booked", cadence_state="completed")
    db.agent_replies.append(_Reply(texts=[], error="timeout"))
    db.text(phone, "what time is my visit?")
    db.make_due(phone)
    db.tick()
    row = db.db.execute("select status,cadence_state,needs_review from leads where id=%s",
                        (lead,)).fetchone()
    assert (row["status"], row["cadence_state"], row["needs_review"]) == ("booked", "completed", True)
    review = db.db.execute("select code,details from lead_reviews where lead_id=%s and "
                           "resolved_at is null", (lead,)).fetchone()
    assert review["code"] == sms_inbound.REVIEW_AGENT_FAILED and review["details"]["finished"]


def _options_and_resolve(lead_id, choice):
    """The card's choices, then staff pick `choice` (the dashboard's two calls)."""
    from rpt_agent.services import lead_review

    with get_pool().connection() as conn, conn.transaction():
        lead = conn.execute("select * from leads where id=%s", (lead_id,)).fetchone()
        offered = [o["choice"] for o in lead_review.options_preview(conn, lead)["options"]]
        try:
            lead_review.resolve_review(conn, lead_id, choice, note=None, actor_id="u1",
                                       actor_name="Staff")
        except lead_review.ReviewError as exc:
            return offered, exc.status_code
    return offered, None


def test_a_failure_card_on_a_booked_lead_never_un_books_it(db):
    # The card on a finished lead (details.finished) used to offer its group's "Continue cadence",
    # which set a booked patient back to In cadence with calls running again.
    phone = db.phone()
    lead = db.lead(phone, status="booked", cadence_state="completed")
    db.agent_replies.append(_Reply(texts=[], error="timeout"))
    db.text(phone, "what time is my visit?")
    db.make_due(phone)
    db.tick()
    offered, refused = _options_and_resolve(lead, "continue")
    assert offered == ["front_desk"] and refused == 409
    assert _options_and_resolve(lead, "front_desk") == (["front_desk"], None)
    row = db.db.execute("select status,cadence_state,needs_review from leads where id=%s",
                        (lead,)).fetchone()
    assert (row["status"], row["cadence_state"], row["needs_review"]) == ("booked", "completed", False)


def test_front_desk_on_a_finished_failure_card_keeps_a_link_sent_lead(db):
    # booking_link_sent with its cadence completed is no final status, but the card is `finished`:
    # Front desk will handle closes the card, never moves the lead to Closed.
    phone = db.phone()
    lead = db.lead(phone, status="booking_link_sent", cadence_state="completed")
    db.agent_replies.append(_Reply(texts=[], error="timeout"))
    db.text(phone, "hi")
    db.make_due(phone)
    db.tick()
    assert _options_and_resolve(lead, "front_desk") == (["front_desk"], None)
    row = db.db.execute("select status,cadence_state,needs_review from leads where id=%s",
                        (lead,)).fetchone()
    assert (row["status"], row["cadence_state"], row["needs_review"]) == (
        "booking_link_sent", "completed", False)


def test_an_unsent_booked_text_opens_a_card_even_with_texts_off(db):
    # The example in the review: the booking went through, the "booked" text did not. Staff turned
    # texts off (calls only) while Elliot was booking: the card must still tell them to call.
    phone = db.phone()
    lead = db.lead(phone)
    booked = real_texts.fill(real_texts.DRAFTS["booked"], {
        "first_name": "Test", "day": "Tuesday, November 3", "time": "9:00 AM", "therapist": "Alan",
        "clinic": "Laguna Niguel", "address": real_texts.address_for("Laguna Niguel")})

    def booked_then_calls_only(lead_id):
        db.db.execute("update leads set status='booked',cadence_state='completed',"
                      "sms_disabled=true where id=%s", (lead_id,))
        return _Reply(texts=[booked], actions=[types.SimpleNamespace(kind="book",
                                                                      result_code="BOOKED")])

    def refuse(trace, to, body):
        raise ProviderError("twilio", "21610", "refused")

    db.twilio.send_sms = refuse
    db.agent_replies.append(booked_then_calls_only)
    db.text(phone, "YES")
    db.make_due(phone)
    db.tick()
    assert db.jobs(phone)[0]["outcome"] == "send_failed"
    review = db.db.execute("select code,message,details from lead_reviews where lead_id=%s and "
                           "resolved_at is null", (lead,)).fetchone()
    assert review["code"] == sms_inbound.REVIEW_AGENT_FAILED
    assert review["message"] == "SMS agent's booking confirmation text was not sent"
    assert review["details"]["booked_text_unsent"] and review["details"]["finished"]
    row = db.db.execute("select status,needs_review from leads where id=%s", (lead,)).fetchone()
    assert (row["status"], row["needs_review"]) == ("booked", True)


def test_a_booked_text_goes_out_after_staff_step_in(db):
    phone = db.phone()
    lead = db.lead(phone)
    booked = real_texts.fill(real_texts.DRAFTS["booked"], {
        "first_name": "Test", "day": "Tuesday, November 3", "time": "9:00 AM", "therapist": "Alan",
        "clinic": "Laguna Niguel", "address": real_texts.address_for("Laguna Niguel")})

    def booked_while_staff_texted(lead_id):
        # book_visit booked (lead Booked); staff sent a manual text before the model finished.
        db.db.execute("update leads set status='booked',cadence_state='completed' where id=%s",
                      (lead_id,))
        db.db.execute("insert into sms_messages(lead_id,direction,body,occurred_at,"
                      "delivery_status,sender) values(%s,'outbound','staff',now(),'sent','staff')",
                      (lead_id,))
        return _Reply(texts=[booked, "Anything else?"],
                      actions=[types.SimpleNamespace(kind="book", result_code="BOOKED")])

    db.agent_replies.append(booked_while_staff_texted)
    db.text(phone, "YES")
    db.make_due(phone)
    db.tick()
    assert [body for _, body in db.twilio.sent] == [booked]
    assert db.jobs(phone)[0]["outcome"] == "booked_text_only:staff_took_over"
    assert len(db.ai_rows(lead)) == 1


def test_an_agent_error_after_a_text_opt_out_pauses_nothing(db):
    phone = db.phone()
    lead = db.lead(phone)

    def opted_out_then_timed_out(lead_id):
        db.db.execute("update leads set sms_opt_out=true where id=%s", (lead_id,))
        return _Reply(texts=[], error="timeout", opted_out=True)

    db.agent_replies.append(opted_out_then_timed_out)
    db.text(phone, "stop texting me")
    db.make_due(phone)
    db.tick()
    row = db.db.execute("select status,cadence_state,needs_review from leads where id=%s",
                        (lead,)).fetchone()
    assert (row["status"], row["cadence_state"], row["needs_review"]) == (
        "in_progress", "active", False)
    assert db.db.execute("select count(*) as n from lead_reviews where lead_id=%s",
                         (lead,)).fetchone()["n"] == 0
    assert db.db.execute("select count(*) as n from lead_status_history where lead_id=%s "
                         "and reason like '%%no review%%'", (lead,)).fetchone()["n"] == 1


def test_every_sms_messages_insert_names_its_sender():
    # Migration 054: outbound null = cadence or system, never the AI. Every writer says who it is.
    src = Path(__file__).resolve().parents[1] / "src" / "rpt_agent"
    missing = []
    for path in src.rglob("*.py"):
        text = path.read_text()
        for match in re.finditer(r"insert into sms_messages\(([^)]*)\)", text.replace('"\n', "")):
            columns = re.sub(r'["\s]', "", match.group(1))
            if "sender" not in columns.split(","):
                missing.append(f"{path.name}: {columns}")
    assert missing == []
