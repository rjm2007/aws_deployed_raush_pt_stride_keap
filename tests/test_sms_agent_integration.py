"""SMS agent tools and loop on a real schema (TEST_DATABASE_URL: a throwaway local Postgres with the
migrations applied, never the .env database) with a fake Stride and a fake OpenAI client.

Live: find (offers kept under "sms:<lead>"), read-back card (writes nothing), book only after the
card + a reply (source sms, no second confirmation text), a failed booking goes to Needs Review "by
text", every report_outcome status with no outreach event, hand-off cards (paused lead; finished
lead keeps its status), reply_live end to end. Sandbox: real cache, nothing written, pretend booking.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row
from test_slot_booking_integration import FakeStride, stride_error

from rpt_agent.config import get_settings
from rpt_agent.db import get_pool
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import availability_sync, lead_review, slot_booking, sms_agent
from rpt_agent.services import sms_agent_texts as texts
from rpt_agent.services.availability_sync import run_sync
from rpt_agent.services.sms_agent import SandboxSetup, Turn, reply_live, reply_sandbox
from rpt_agent.services.sms_agent_context import load_live
from rpt_agent.services.sms_agent_tools import LiveTools, SandboxTools, call_tool

pytestmark = pytest.mark.integration

TODAY = date(2026, 6, 15)  # pinned demo "today" (Monday)
DAY1 = "2026-06-16"
LINK = "https://book.example/rpt"
# The identity step (owner, 2026-11-03): the patient types their date of birth before any card. The
# test lead's date on file is 1990-04-26.
DOB_TURN = Turn("patient", "my birthday is 4/26/1990", None)
TOOL_SPECS = [{"type": "function", "name": "book_visit", "description": "", "parameters": {}}]


@pytest.fixture
def env(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    for key, value in {
        "SUPABASE_DB_URL": url,
        "BOOKING_TODAY_OVERRIDE": TODAY.isoformat(),
        "BOOKING_MIN_NOTICE_MINUTES": "0",
        "SHEET_SYNC_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(availability_sync, "STRIDE_REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(slot_booking, "IN_PROGRESS_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(slot_booking, "IN_PROGRESS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(sms_agent, "load_prompt", lambda: "PROMPT")
    monkeypatch.setattr(sms_agent, "load_tools", lambda: TOOL_SPECS)
    get_settings.cache_clear()
    get_pool.cache_clear()
    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    slug = f"test-sms-{uuid4().hex[:10]}"
    practice = conn.execute(
        "insert into practices(name,slug,timezone) values('Synthetic Practice',%s,"
        "'America/Los_Angeles') returning id", (slug,),
    ).fetchone()["id"]
    conn.execute(
        "insert into practice_settings(practice_id,stride_location_timezone,stride_booking_enabled,"
        "booking_link_url) values(%s,'America/New_York',true,%s)", (practice, LINK),
    )
    location = conn.execute(
        "insert into booking_locations(practice_id,name,stride_location_id,timezone) "
        "values(%s,'Laguna Niguel',3170,'America/New_York') returning id", (practice,),
    ).fetchone()["id"]
    case_type = conn.execute(
        "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes,"
        "is_default) values(%s,'Physical Therapy',1452,60,true) returning id", (practice,),
    ).fetchone()["id"]
    for stride_id, name in ((5981, "Thao Nguyen"), (5982, "Alan Rome")):
        clinician = conn.execute(
            "insert into clinicians(practice_id,stride_user_id,display_name) values(%s,%s,%s) "
            "returning id", (practice, stride_id, name),
        ).fetchone()["id"]
        conn.execute(
            "insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
            "values(%s,%s,%s)", (clinician, location, case_type),
        )

    class Env:
        db = conn
        practice_id = practice
        practice_slug = slug
        stride = FakeStride()
        trace = WorkflowTrace("test_sms_agent", "test")

        def settings(self, **update):
            return get_settings().model_copy(update=update)

        def lead(self, *, status="in_progress", cadence="active"):
            return str(conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,"
                "phone_e164,date_of_birth,status,cadence_state,is_test,location,lead_type) "
                "values(%s,'dashboard','Rpt Tester','Rpt','Tester',%s,'1990-04-26',%s,%s,true,"
                "'Laguna Niguel','knee') returning id",
                (practice, f"+1555{uuid4().int % 10**7:07d}", status, cadence),
            ).fetchone()["id"])

        def open_and_sync(self):
            self.stride.open = {5981: {DAY1: ["09:00:00", "10:00:00"]}, 5982: {DAY1: ["11:00:00"]}}
            run_sync(self.trace, providers=self.stride, settings=self.settings(),
                     practice_id=practice)

        def live(self, lead_id, history, job_id=1, dob=True):
            """`dob`: the chat starts with the patient's date of birth (the card needs it)."""
            with get_pool().connection() as c:
                view = load_live(c, lead_id)
            return LiveTools(self.trace, lead_id=lead_id, job_id=job_id, lead=view.lead,
                             history=[DOB_TURN, *history] if dob else history,
                             settings=self.settings(),
                             practice_id=practice, providers=self.stride)

        def one(self, sql, *args):
            return conn.execute(sql, args).fetchone()

        def all(self, sql, *args):
            return conn.execute(sql, args).fetchall()

    environment = Env()
    yield environment
    conn.close()
    get_settings.cache_clear()
    get_pool.cache_clear()


def card_flow(env, lead_id):
    """find -> confirm (card) -> the patient's yes; returns the tools for the booking turn."""
    tools = env.live(lead_id, [Turn("patient", "tue morning?", None)])
    found = call_tool(tools, "find_times", {"when": "specific_date", "specific_date": DAY1})
    assert found.code == "OPENINGS", found.output
    card = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM",
                                              "therapist": "Thao Nguyen", "clinic": None})
    assert card.code == "CARD_SENT" and card.send_last
    history = [Turn("patient", "tue morning?", None), Turn("elliot", card.fixed_texts[0], None),
               Turn("patient", "YES", None)]
    return env.live(lead_id, history), card.fixed_texts[0]


BOOK = {"date": DAY1, "time": "9:00 AM", "therapist": "Thao Nguyen", "first_name": "Rpt",
        "last_name": "Tester", "date_of_birth": "1990-04-26", "clinic": None}


# --- live: find, card, book ------------------------------------------------------------------------

def test_find_times_remembers_offers_per_conversation(env):
    env.open_and_sync()
    lead_id = env.lead()
    tools = env.live(lead_id, [])
    result = call_tool(tools, "find_times", {"when": "earliest"})
    assert result.code == "OPENINGS" and "2026-06-16" in result.output
    keys = {row["call_id"] for row in env.all("select call_id from slot_offers where lead_id=%s",
                                               lead_id)}
    assert keys == {f"sms:{lead_id}"}
    assert slot_booking.offer_memory(f"sms:{lead_id}") == slot_booking.SMS_OFFER_MEMORY
    therapists = call_tool(tools, "list_therapists", {})
    assert therapists.code == "PROVIDERS" and "Alan Rome" in therapists.output


def test_card_is_deterministic_and_writes_nothing(env):
    env.open_and_sync()
    lead_id = env.lead()
    tools = env.live(lead_id, [])
    result = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM",
                                                "therapist": "Thao"})
    assert result.fixed_texts == [texts.confirm_card(date(2026, 6, 16), slot_booking.parse_clock(
        "9:00"), "Thao Nguyen", "Laguna Niguel")]
    assert env.one("select count(*) as n from slot_holds where lead_id=%s", lead_id)["n"] == 0
    taken = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "3:00 PM"})
    assert taken.code == "NOT_AVAILABLE" and not taken.fixed_texts
    assert "Other openings" in taken.output


def test_book_after_card_and_yes_books_with_source_sms_and_no_second_text(env):
    env.open_and_sync()
    lead_id = env.lead()
    tools, _card = card_flow(env, lead_id)
    result = call_tool(tools, "book_visit", BOOK)
    assert result.code == "BOOKED", result.output
    [text] = result.fixed_texts
    assert text.startswith("You're all set, Rpt! Tuesday, June 16 at 9:00 AM with Thao Nguyen at "
                           "our Laguna Niguel office (30100 Town Center Drive")
    appointment = env.one("select booking_source,state,call_id from appointments where lead_id=%s",
                          lead_id)
    assert appointment == {"booking_source": "sms", "state": "scheduled",
                           "call_id": f"sms:{lead_id}"}
    lead = env.one("select status,cadence_state from leads where id=%s", lead_id)
    assert lead == {"status": "booked", "cadence_state": "completed"}
    # The booked text is the confirmation: no separate confirmation SMS is queued.
    assert env.one("select count(*) as n from notification_log where lead_id=%s "
                   "and notification_type='sms_appointment_booked'", lead_id)["n"] == 0
    history = env.one("select source from lead_status_history where lead_id=%s and "
                      "to_status='booked'", lead_id)
    assert history["source"] == "sms_booking"
    # Once booked, a new card is refused.
    booked_tools = env.live(lead_id, [])
    assert call_tool(booked_tools, "confirm_visit", {"date": DAY1, "time": "10:00 AM"}).code == \
        "ALREADY_BOOKED"


def test_voice_booking_still_queues_the_confirmation_text(env):
    env.open_and_sync()
    lead_id = env.lead()
    result = slot_booking.book_slot(env.trace, lead_id=lead_id, call_id="call-1",
                                    arguments={**BOOK, "clinician_name": "Thao"},
                                    providers=env.stride, settings=env.settings())
    assert result.startswith("BOOKED")
    assert env.one("select count(*) as n from notification_log where lead_id=%s "
                   "and notification_type='sms_appointment_booked'", lead_id)["n"] == 1


def test_book_without_a_card_or_without_a_reply_is_refused(env):
    env.open_and_sync()
    lead_id = env.lead()
    for history in ([Turn("patient", "book tue 9", None)],
                    [Turn("elliot", texts.confirm_card(date(2026, 6, 16),
                                                       slot_booking.parse_clock("9:00"),
                                                       "Thao Nguyen", "Laguna Niguel"), None)]):
        result = call_tool(env.live(lead_id, history), "book_visit", BOOK)
        assert result.code == "NOT_CONFIRMED"
    # A different time than the card.
    tools, _ = card_flow(env, lead_id)
    assert call_tool(tools, "book_visit", {**BOOK, "time": "10:00 AM"}).code == "NOT_CONFIRMED"
    assert env.one("select count(*) as n from appointments where lead_id=%s", lead_id)["n"] == 0


def test_failed_booking_flags_the_lead_by_text_and_sends_the_front_desk_text(env):
    env.open_and_sync()
    lead_id = env.lead()
    tools, _ = card_flow(env, lead_id)
    env.stride.script["patients"] = [stride_error("server_error", "Stride 500")]
    result = call_tool(tools, "book_visit", BOOK)
    assert result.code == "FOLLOW_UP"
    assert result.fixed_texts and "front desk" in result.fixed_texts[0]
    review = env.one("select code,message,details,source from lead_reviews where lead_id=%s "
                     "and resolved_at is null", lead_id)
    assert review["code"] == "booking_failed" and review["details"]["by_text"] is True
    assert review["message"].startswith(slot_booking.NOT_MADE_BY_TEXT_REASON)
    # The prompt then hands off with booking_problem: no second card.
    handed = call_tool(tools, "hand_off", {"reason": "booking_problem"})
    assert handed.code == "HANDED_OFF"
    assert env.one("select count(*) as n from lead_reviews where lead_id=%s and resolved_at is null",
                   lead_id)["n"] == 1


def test_taken_time_offers_others(env):
    env.open_and_sync()
    lead_id = env.lead()
    tools, _ = card_flow(env, lead_id)
    env.stride.open = {5981: {DAY1: ["10:00:00"]}, 5982: {DAY1: ["11:00:00"]}}
    result = call_tool(tools, "book_visit", BOOK)
    assert result.code == "TAKEN" and not result.fixed_texts and "Other openings" in result.output


# --- live: outcomes and hand-offs ------------------------------------------------------------------

@pytest.mark.parametrize("status,arguments,lead_after", [
    ("declined", {}, {"status": "declined", "cadence_state": "terminated"}),
    ("callback_scheduled", {"callback_type": "relative", "delay_minutes": 60},
     {"status": "callback_scheduled", "cadence_state": "active"}),
    ("booking_link", {}, {"status": "booking_link_sent", "cadence_state": "active"}),
    ("wrong_person", {"notes": "not my number"}, {"status": "invalid_phone",
                                                  "cadence_state": "paused"}),
    ("case_change", {"notes": "spine"}, {"status": "needs_attention", "cadence_state": "paused"}),
])
def test_report_outcome_works_without_an_outreach_event(env, status, arguments, lead_after):
    lead_id = env.lead()
    tools = env.live(lead_id, [], job_id=42)
    result = call_tool(tools, "report_outcome", {"status": status, **arguments})
    assert result.code == "REPORTED", result.output
    lead = env.one("select status,cadence_state from leads where id=%s", lead_id)
    assert lead == lead_after
    sources = {row["source"] for row in env.all(
        "select source from lead_status_history where lead_id=%s", lead_id)}
    assert "sms agent" in sources
    receipt = env.one("select event_id from provider_events where event_id like %s",
                      f"sms:{lead_id}:42:%")
    assert receipt is not None
    if status == "booking_link":
        assert LINK in result.output
        # The agent's own text carries the link: no separate link text.
        assert env.one("select count(*) as n from notification_log where lead_id=%s",
                       lead_id)["n"] == 0
    if status in {"wrong_person", "case_change"}:
        review = env.one("select code,source from lead_reviews where lead_id=%s", lead_id)
        assert review["source"] == "text"
        assert review["code"] == ("wrong_person" if status == "wrong_person"
                                  else "case_change_asked")
    # The same job reporting the same status again is a duplicate; a case change already
    # reported is not reported again (gpt-5.5 eval, 2026-11-02).
    again = call_tool(tools, "report_outcome", {"status": status, **arguments}).code
    assert again == ("ALREADY_REPORTED" if status == "case_change" else "REPORTED")


def test_report_do_not_contact_by_text_is_an_sms_opt_out(env):
    lead_id = env.lead()
    result = call_tool(env.live(lead_id, []), "report_outcome", {"status": "do_not_contact"})
    assert result.code == "REPORTED"
    lead = env.one("select sms_opt_out,call_opt_out,status from leads where id=%s", lead_id)
    assert lead == {"sms_opt_out": True, "call_opt_out": False, "status": "in_progress"}


def test_report_no_contact_at_all_by_text_stops_calls_too(env):
    # "Don't contact me at all": both channels off = Do Not Contact, so the calls stop as well and
    # "We won't contact you again" is true.
    lead_id = env.lead()
    result = call_tool(env.live(lead_id, []), "report_outcome",
                       {"status": "do_not_contact", "notes": "asked for no contact at all"})
    assert result.code == "REPORTED" and result.opted_out
    assert "texts and calls stopped" in result.output
    lead = env.one("select sms_opt_out,call_opt_out,status,cadence_state from leads where id=%s",
                   lead_id)
    assert lead == {"sms_opt_out": True, "call_opt_out": True, "status": "do_not_contact",
                    "cadence_state": "terminated"}


def test_case_change_by_text_opens_one_card(env):
    lead_id = env.lead()
    tools = env.live(lead_id, [Turn("patient", "it's my back now", None)])
    assert call_tool(tools, "report_outcome", {"status": "case_change", "notes": "spine"}).code \
        == "REPORTED"
    handed = call_tool(tools, "hand_off", {"reason": "different_case", "notes": "spine"})
    assert handed.code == "HANDED_OFF" and handed.fixed_texts
    cards = env.all("select code,source from lead_reviews where lead_id=%s and resolved_at is null",
                    lead_id)
    assert cards == [{"code": "case_change_asked", "source": "text"}]


def test_report_callback_without_time_is_not_recorded(env):
    lead_id = env.lead()
    result = call_tool(env.live(lead_id, []), "report_outcome", {"status": "callback_scheduled"})
    assert result.code == "NOT_RECORDED"
    assert env.one("select status from leads where id=%s", lead_id)["status"] == "in_progress"


def test_report_on_a_booked_lead_changes_nothing(env):
    lead_id = env.lead(status="booked", cadence="completed")
    result = call_tool(env.live(lead_id, []), "report_outcome", {"status": "declined"})
    assert result.code == "ALREADY_BOOKED"
    assert env.one("select status from leads where id=%s", lead_id)["status"] == "booked"


def test_hand_off_opens_the_sms_front_desk_card_and_staff_can_resolve_it(env):
    lead_id = env.lead()
    tools = env.live(lead_id, [], job_id=7)
    result = call_tool(tools, "hand_off", {"reason": "insurance", "notes": "asked about Aetna"})
    assert result.code == "HANDED_OFF"
    assert result.fixed_texts == [texts.render(None, "front_desk")]
    lead = env.one("select status,cadence_state,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "needs_attention", "cadence_state": "paused", "needs_review": True}
    review = env.one("select code,source,details from lead_reviews where lead_id=%s", lead_id)
    assert review["code"] == lead_review.SMS_FRONT_DESK_CODE and review["source"] == "text"
    assert review["details"]["reason"] == "insurance"
    assert review["details"]["conversation"] == f"sms:{lead_id}:7"
    with get_pool().connection() as conn, conn.transaction():
        done = lead_review.resolve_review(conn, lead_id, "front_desk", note=None, actor_id="u1",
                                          actor_name="Staff")
    assert done["lead_status"] == "closed_no_response"


def test_hand_off_other_language_sends_english_only(env):
    lead_id = env.lead()
    result = call_tool(env.live(lead_id, []), "hand_off", {"reason": "other_language"})
    assert result.fixed_texts == [texts.render(None, "english_only")]


def test_hand_off_on_a_declined_lead_keeps_its_status(env):
    lead_id = env.lead(status="declined", cadence="terminated")
    call_tool(env.live(lead_id, []), "hand_off", {"reason": "wants_person"})
    lead = env.one("select status,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "declined", "needs_review": True}
    review = env.one("select details from lead_reviews where lead_id=%s", lead_id)
    assert review["details"]["finished"] is True
    with get_pool().connection() as conn, conn.transaction():
        lead_row = conn.execute("select * from leads where id=%s", (lead_id,)).fetchone()
        assert [o["choice"] for o in lead_review.options_preview(conn, lead_row)["options"]] == [
            "front_desk"]
        lead_review.resolve_review(conn, lead_id, "front_desk", note=None, actor_id="u1",
                                   actor_name="Staff")
    lead = env.one("select status,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "declined", "needs_review": False}


def test_emergency_sends_911_and_opens_a_card(env):
    lead_id = env.lead()
    result = call_tool(env.live(lead_id, []), "flag_emergency", {})
    assert result.fixed_texts == [texts.EMERGENCY_TEXT]
    review = env.one("select code,details from lead_reviews where lead_id=%s", lead_id)
    assert review["code"] == "sms_front_desk" and review["details"]["reason"] == "emergency"


# --- live: Elliot's own front-desk card does not stop booking (owner, 2026-11-02) -------------------

def open_reviews(env, lead_id):
    return [(row["code"], (row["details"] or {}).get("reason")) for row in env.all(
        "select code,details from lead_reviews where lead_id=%s and resolved_at is null "
        "order by id", lead_id)]


def test_front_desk_card_does_not_stop_booking_and_stays_open(env):
    from rpt_agent.services.sheet_sync import build_sheet_snapshot

    env.open_and_sync()
    lead_id = env.lead()
    call_tool(env.live(lead_id, [], job_id=3), "hand_off", {"reason": "insurance",
                                                            "notes": "Blue Shield?"})
    lead = env.one("select status,cadence_state,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "needs_attention", "cadence_state": "paused", "needs_review": True}
    with get_pool().connection() as conn:
        view = load_live(conn, lead_id).lead
    assert view.front_desk_only and not view.booking_paused
    assert view.review_reason == "insurance or costs"
    # The next text: find, card, yes, booked.
    tools, _ = card_flow(env, lead_id)
    result = call_tool(tools, "book_visit", BOOK)
    assert result.code == "BOOKED", result.output
    lead = env.one("select status,cadence_state,needs_review,review_reason from leads where id=%s",
                   lead_id)
    assert lead["status"] == "booked" and lead["cadence_state"] == "completed"
    # The card stays open: the front desk still calls about the insurance question.
    assert lead["needs_review"] is True and lead["review_reason"].startswith("Patient texted:")
    assert open_reviews(env, lead_id) == [("sms_front_desk", "insurance")]
    with get_pool().connection() as conn:
        snapshot = build_sheet_snapshot(conn, lead_id)
        lead_row = conn.execute("select * from leads where id=%s", (lead_id,)).fetchone()
        assert [o["choice"] for o in lead_review.options_preview(conn, lead_row)["options"]] == [
            "front_desk"]
    assert snapshot["sheet"]["action_status"] == lead_review.SHEET_BOOKED_FRONT_DESK
    # Staff handle it: the card closes, the lead stays booked.
    with get_pool().connection() as conn, conn.transaction():
        lead_review.resolve_review(conn, lead_id, "front_desk", note=None, actor_id="u1",
                                   actor_name="Staff")
    lead = env.one("select status,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "booked", "needs_review": False}


@pytest.mark.parametrize("opened", ["case_change", "different_case", "booking_problem",
                                    "emergency", "insurance_and_case_change"])
def test_other_open_reviews_still_stop_booking(env, opened):
    env.open_and_sync()
    lead_id = env.lead()
    first = env.live(lead_id, [], job_id=3)
    if opened in {"case_change", "insurance_and_case_change"}:
        if opened == "insurance_and_case_change":
            call_tool(first, "hand_off", {"reason": "insurance"})
        call_tool(first, "report_outcome", {"status": "case_change", "notes": "spine"})
    elif opened == "emergency":
        call_tool(first, "flag_emergency", {})
    else:
        call_tool(first, "hand_off", {"reason": opened})
    with get_pool().connection() as conn:
        assert load_live(conn, lead_id).lead.booking_paused
    tools = env.live(lead_id, [Turn("patient", "tue morning?", None)])
    card = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM",
                                              "therapist": "Thao Nguyen"})
    assert card.code == "REVIEW_OPEN" and not card.fixed_texts
    history = [Turn("elliot", texts.confirm_card(date(2026, 6, 16), slot_booking.parse_clock("9:00"),
                                                 "Thao Nguyen", "Laguna Niguel"), None),
               Turn("patient", "YES", None)]
    assert call_tool(env.live(lead_id, history), "book_visit", BOOK).code == "REVIEW_OPEN"
    assert env.one("select count(*) as n from appointments where lead_id=%s", lead_id)["n"] == 0


def test_a_booking_settles_a_booking_problem_card_but_keeps_the_others(env):
    # mark_booked (any booking: voice, text, Stride's file) keeps the case change and Elliot's
    # front-desk card, but a booking settles the front-desk card's own booking problem.
    lead_id = env.lead()
    with get_pool().connection() as conn, conn.transaction():
        from rpt_agent.services.lead_status import mark_booked
        from rpt_agent.services.sms_agent_tools import open_front_desk_card
        open_front_desk_card(conn, lead_id, "booking_problem", None, conversation="sms:x:1")
        open_front_desk_card(conn, lead_id, "wants_person", None, conversation="sms:x:1")
        mark_booked(conn, lead_id, "voice_booking")
    assert open_reviews(env, lead_id) == [("sms_front_desk", "wants_person")]
    assert env.one("select status,needs_review from leads where id=%s", lead_id) == {
        "status": "booked", "needs_review": True}
    other = env.lead()
    with get_pool().connection() as conn, conn.transaction():
        open_front_desk_card(conn, other, "booking_problem", None, conversation="sms:x:1")
        mark_booked(conn, other, "voice_booking")
    assert open_reviews(env, other) == []
    assert env.one("select needs_review from leads where id=%s", other)["needs_review"] is False


def test_studio_text_is_used_when_staff_edited_it(env):
    env.db.execute(
        "insert into message_templates(practice_id,key,channel,body) values(%s,'sms_agent.intro',"
        "'sms','Hey {first_name}! Book: {booking_link}')", (env.practice_id,))
    lead_id = env.lead()
    result = call_tool(env.live(lead_id, []), "send_intro", {})
    assert result.fixed_texts == [f"Hey Rpt! Book: {LINK}"]


# --- live: end to end ------------------------------------------------------------------------------

class FakeResponses:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **request):
        self.requests.append({**request, "input": list(request["input"])})
        return self.script.pop(0)


class FakeClient:
    def __init__(self, *script):
        self.responses = FakeResponses(script)


def test_reply_live_books_from_the_database_history(env):
    env.open_and_sync()
    lead_id = env.lead()
    card = texts.confirm_card(date(2026, 6, 16), slot_booking.parse_clock("9:00"), "Thao Nguyen",
                              "Laguna Niguel")
    for direction, sender, body in (("outbound", "cadence", "Day 0 text"),
                                    ("inbound", "patient", "can I do tue 9? bday 4/26/1990"),
                                    ("outbound", "ai", card),
                                    ("inbound", "patient", "yes please")):
        env.db.execute(
            "insert into sms_messages(lead_id,direction,sender,body,occurred_at,delivery_status) "
            "values(%s,%s,%s,%s,now(),%s)",
            (lead_id, direction, sender, body, "received" if direction == "inbound" else "sent"))
    # The yes to the card books in code before the model runs (2026-11-04); the model only adds
    # its words, and a book_visit of its own changes nothing.
    client = FakeClient(
        {"output": [{"type": "function_call", "name": "book_visit", "call_id": "c1",
                     "arguments": json.dumps({**BOOK, "first_name": None, "last_name": None,
                                              "date_of_birth": None})}]},
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": ""}]}]},
    )
    reply = reply_live(env.trace, lead_id=lead_id, job_id=5, settings=env.settings(),
                       client=client, providers=env.stride)
    assert reply.error is None and len(reply.texts) == 1
    assert reply.texts[0].startswith("You're all set, Rpt!")
    assert [a.result_code for a in reply.actions] == ["BOOKED"]
    request = client.responses.requests[0]
    roles = [item["role"] for item in request["input"]]
    assert roles == ["developer", "developer", "developer", "user", "assistant", "user",
                     "developer"]
    assert request["input"][-1]["content"].startswith("Code step: the patient said yes")
    assert "book_visit" not in [spec["name"] for spec in request["tools"]]
    assert env.one("select count(*) as n from appointments where lead_id=%s", lead_id)["n"] == 1
    phone = env.one("select phone_e164 from leads where id=%s", lead_id)["phone_e164"]
    assert phone not in json.dumps(request["input"], default=str)
    # The date on file is never shown to the model; the one typed in the chat is confirmed.
    context = request["input"][1]["content"]
    assert "1990" not in context and "Birthday on file: yes" in context
    assert "Date of birth confirmed in this chat: yes" in context
    assert env.one("select booking_source from appointments where lead_id=%s",
                   lead_id)["booking_source"] == "sms"


def test_reply_live_reads_no_text_after_its_jobs_last_inbound(env):
    # The worker moves the job to the newest text and commits, then reply_live reads the history.
    # A text that commits in between (one that waited on the worker's lead lock commits right then)
    # queued the next job, which answers it: this job must not answer it as well.
    lead_id = env.lead()
    phone = env.one("select phone_e164 from leads where id=%s", lead_id)["phone_e164"]
    ids = [env.one(
        "insert into sms_messages(lead_id,direction,sender,body,occurred_at,delivery_status) "
        "values(%s,'inbound','patient',%s,now(),'received') returning id", lead_id, body)["id"]
        for body in ("first question", "late question")]
    job = env.one("insert into sms_agent_jobs(lead_id,phone_e164,status,due_at,last_inbound_id) "
                  "values(%s,%s,'running',now(),%s) returning id", lead_id, phone, ids[0])["id"]
    client = FakeClient(
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Sure."}]}]})
    try:
        reply = reply_live(env.trace, lead_id=lead_id, job_id=job, settings=env.settings(),
                           client=client, providers=env.stride)
    finally:
        env.db.execute("delete from sms_agent_jobs where id=%s", (job,))
    assert reply.error is None
    sent = json.dumps(client.responses.requests[0]["input"], default=str)
    assert "first question" in sent and "late question" not in sent


def test_reply_live_never_asks_again_for_a_date_of_birth_given_long_ago(env):
    # Owner, 2026-11-03: once given in the chat, never asked again; the model sees 20 texts, the
    # birthday step reads further back (BIRTHDAY_HISTORY_LIMIT).
    lead_id = env.lead()
    rows = [("inbound", "patient", "my birthday is 4/26/1990")]
    rows += [("inbound", "patient", f"question {n}") if n % 2 == 0
             else ("outbound", "ai", f"answer {n}") for n in range(24)]
    for minutes, (direction, sender, body) in enumerate(rows):
        env.db.execute(
            "insert into sms_messages(lead_id,direction,sender,body,occurred_at,delivery_status) "
            "values(%s,%s,%s,%s,now()-%s::interval,%s)",
            (lead_id, direction, sender, body, f"{len(rows) - minutes} minutes",
             "received" if direction == "inbound" else "sent"))
    with get_pool().connection() as c:
        view = load_live(c, lead_id)
    assert len(view.history) == 20 and "4/26/1990" not in json.dumps([t.text for t in view.history])
    assert view.birthday_history[0].text == "my birthday is 4/26/1990"
    client = FakeClient(
        {"output": [{"type": "message", "content": [{"type": "output_text",
                                                     "text": "What's your date of birth?"}]}]},
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Sure."}]}]})
    reply = reply_live(env.trace, lead_id=lead_id, job_id=6, settings=env.settings(),
                       client=client, providers=env.stride)
    assert reply.texts == ["Sure."]
    context = client.responses.requests[0]["input"][1]["content"]
    assert "Date of birth confirmed in this chat: yes" in context and "1990" not in context


# --- sandbox ---------------------------------------------------------------------------------------

def counts(env):
    return {table: env.one(f"select count(*) as n from {table}")["n"]
            for table in ("slot_offers", "slot_holds", "appointments", "leads", "lead_reviews",
                          "lead_status_history", "notification_log", "provider_events")}


def test_sandbox_reads_the_real_cache_and_writes_nothing(env):
    env.open_and_sync()
    before = counts(env)
    setup = SandboxSetup(clinic="Laguna Niguel", service="Physical Therapy", first_name="Sam",
                         last_name="Test", date_of_birth=date(1990, 3, 14),
                         practice_slug=env.practice_slug)
    from rpt_agent.services.sms_agent import sandbox_lead
    tools = SandboxTools(env.trace, lead=sandbox_lead(setup),
                         history=[Turn("patient", "3/14/1990", None)],
                         practice_id=env.practice_id, now_local=None, settings=env.settings())
    found = call_tool(tools, "find_times", {"when": "earliest"})
    assert found.code == "OPENINGS" and "Thao Nguyen" in found.output
    card = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM", "therapist": "Thao"})
    assert card.code == "CARD_SENT"
    history = [Turn("elliot", card.fixed_texts[0], None), Turn("patient", "yes", None)]
    tools.history = history
    booked = call_tool(tools, "book_visit", {**BOOK, "date_of_birth": None})
    assert booked.code == "BOOKED" and booked.summary.startswith("Would book")
    assert booked.fixed_texts[0].startswith("You're all set, Sam!")
    for status in ("declined", "booking_link", "wrong_person", "case_change", "do_not_contact"):
        assert call_tool(tools, "report_outcome", {"status": status}).code == "REPORTED"
    assert call_tool(tools, "hand_off", {"reason": "insurance"}).code == "HANDED_OFF"
    # A time not in the cache: the TAKEN-style reply with real other times (a new reply: the
    # case change reported above stopped booking for the rest of that one).
    tools.paused_by_hand_off = False
    tools.history = [Turn("elliot", texts.confirm_card(date(2026, 6, 16),
                                                       slot_booking.parse_clock("15:00"),
                                                       "Thao Nguyen", "Laguna Niguel"), None),
                     Turn("patient", "yes", None)]
    taken = call_tool(tools, "book_visit", {**BOOK, "time": "3:00 PM"})
    assert taken.code == "TAKEN" and "Other openings" in taken.output
    assert counts(env) == before
    # The sandbox clock moves the calendar: from June 17 the 16th is gone.
    later = SandboxTools(env.trace, lead=sandbox_lead(setup), history=[],
                         practice_id=env.practice_id,
                         now_local=__import__("datetime").datetime(2026, 6, 17, 8, 0),
                         settings=env.settings())
    assert "2026-06-16" not in call_tool(later, "find_times", {"when": "earliest"}).output


def test_reply_sandbox_end_to_end_writes_nothing(env):
    env.open_and_sync()
    before = counts(env)
    client = FakeClient(
        {"output": [{"type": "function_call", "name": "find_times", "call_id": "c1",
                     "arguments": json.dumps({"when": "earliest"})}]},
        {"output": [{"type": "message", "content": [{"type": "output_text",
                                                      "text": "Here's what I have open: ..."}]}]},
    )
    setup = SandboxSetup(clinic="Laguna Niguel", first_name="Sam", practice_slug=env.practice_slug)
    reply = reply_sandbox(setup=setup, history=[Turn("patient", "earliest?", None)],
                          settings=env.settings(), client=client)
    assert reply.texts == ["Here's what I have open: ..."]
    assert [a.result_code for a in reply.actions] == ["OPENINGS"]
    assert counts(env) == before


# --- an outcome by text while a review is open (2026-11-02 review) ---------------------------------

def choices(env, lead_id):
    with get_pool().connection() as conn:
        lead_row = conn.execute("select * from leads where id=%s", (lead_id,)).fetchone()
        return [o["choice"] for o in lead_review.options_preview(conn, lead_row)["options"]]


def test_decline_by_text_closes_elliots_front_desk_card_and_the_lead_stays_declined(env):
    # In cadence, asks about insurance (card, paused), then "not interested": before, needs_review
    # stayed and staff's Continue cadence restarted outreach to someone who had declined.
    lead_id = env.lead()
    call_tool(env.live(lead_id, [], job_id=1), "hand_off", {"reason": "insurance"})
    result = call_tool(env.live(lead_id, [], job_id=2), "report_outcome", {"status": "declined"})
    assert result.code == "REPORTED" and result.action_code == "REPORTED_DECLINED"
    lead = env.one("select status,cadence_state,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "declined", "cadence_state": "terminated", "needs_review": False}
    review = env.one("select resolution,note from lead_reviews where lead_id=%s", lead_id)
    assert review["resolution"] == "closed" and "declined" in review["note"]
    assert env.one("select count(*) as n from outreach_events where lead_id=%s "
                   "and status='planned'", lead_id)["n"] == 0


@pytest.mark.parametrize("opened", ["case_change", "emergency"])
def test_decline_with_another_card_open_keeps_the_card_but_never_restarts_outreach(env, opened):
    lead_id = env.lead()
    first = env.live(lead_id, [], job_id=1)
    if opened == "case_change":
        call_tool(first, "report_outcome", {"status": "case_change", "notes": "spine"})
    else:
        call_tool(first, "flag_emergency", {})
    call_tool(env.live(lead_id, [], job_id=2), "report_outcome", {"status": "declined"})
    lead = env.one("select status,cadence_state,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "declined", "cadence_state": "terminated", "needs_review": True}
    offered = choices(env, lead_id)
    assert "continue" not in offered and "front_desk" in offered
    assert not set(offered) & (lead_review.RESUME_CHOICES | lead_review.RESTART_CHOICES)
    with (get_pool().connection() as conn, conn.transaction(),
          pytest.raises(lead_review.ReviewError)):
        lead_review.resolve_review(conn, lead_id, "continue", note=None, actor_id="u1",
                                   actor_name="Staff")
    with get_pool().connection() as conn, conn.transaction():
        lead_review.resolve_review(conn, lead_id, "front_desk", note=None, actor_id="u1",
                                   actor_name="Staff")
    lead = env.one("select status,cadence_state,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": "declined", "cadence_state": "terminated", "needs_review": False}


def test_wrong_person_by_text_closes_the_front_desk_card_and_keeps_its_own(env):
    lead_id = env.lead()
    call_tool(env.live(lead_id, [], job_id=1), "hand_off", {"reason": "wants_person"})
    call_tool(env.live(lead_id, [], job_id=2), "report_outcome",
              {"status": "wrong_person", "notes": "not my number"})
    assert open_reviews(env, lead_id) == [("wrong_person", None)]
    lead = env.one("select status,needs_review,review_reason from leads where id=%s", lead_id)
    assert lead["status"] == "invalid_phone" and lead["needs_review"] is True
    assert lead["review_reason"] == "not my number"


def test_no_contact_at_all_by_text_closes_every_card(env):
    lead_id = env.lead()
    call_tool(env.live(lead_id, [], job_id=1), "hand_off", {"reason": "insurance"})
    call_tool(env.live(lead_id, [], job_id=2), "report_outcome",
              {"status": "do_not_contact", "notes": "no contact at all"})
    assert open_reviews(env, lead_id) == []
    assert env.one("select status,needs_review from leads where id=%s", lead_id) == {
        "status": "do_not_contact", "needs_review": False}


@pytest.mark.parametrize("status,arguments,status_after", [
    ("callback_scheduled", {"callback_type": "relative", "delay_minutes": 60}, "callback_scheduled"),
    ("booking_link", {}, "booking_link_sent"),
])
def test_callback_or_link_by_text_keeps_the_card_and_records_the_outcome(
        env, status, arguments, status_after):
    lead_id = env.lead()
    call_tool(env.live(lead_id, [], job_id=1), "hand_off", {"reason": "insurance"})
    result = call_tool(env.live(lead_id, [], job_id=2), "report_outcome",
                       {"status": status, **arguments})
    assert result.code == "REPORTED", result.output
    assert open_reviews(env, lead_id) == [("sms_front_desk", "insurance")]
    lead = env.one("select status,needs_review from leads where id=%s", lead_id)
    assert lead == {"status": status_after, "needs_review": True}


def test_reply_sandbox_refuses_a_booking_after_a_case_change_earlier_in_the_chat(env):
    # Turn 1 reported a case change; turn 2 the patient says yes to a card: live refuses, so the
    # test chat must too (prior_actions from the route).
    env.open_and_sync()
    card_text = texts.confirm_card(date(2026, 6, 16), slot_booking.parse_clock("9:00"),
                                   "Thao Nguyen", "Laguna Niguel")
    client = FakeClient(
        {"output": [{"type": "function_call", "name": "book_visit", "call_id": "c1",
                     "arguments": json.dumps({**BOOK, "date_of_birth": None})}]},
        {"output": [{"type": "message", "content": [{"type": "output_text",
                                                      "text": "Our front desk will reach out."}]}]},
    )
    setup = SandboxSetup(clinic="Laguna Niguel", first_name="Sam", last_name="Test",
                         date_of_birth=date(1990, 3, 14), practice_slug=env.practice_slug)
    reply = reply_sandbox(setup=setup, history=[Turn("elliot", card_text, None),
                                                Turn("patient", "yes", None)],
                          settings=env.settings(), client=client,
                          prior_actions=[{"kind": "report", "result_code": "REPORTED_CASE_CHANGE"}])
    assert [a.result_code for a in reply.actions] == ["REVIEW_OPEN"]


# --- repeats, service, therapist names (gpt-5.5 eval, 2026-11-02) ----------------------------------

def test_live_hand_off_case_change_and_emergency_are_not_repeated_on_a_later_reply(env):
    lead_id = env.lead()
    assert call_tool(env.live(lead_id, [], job_id=1), "hand_off",
                     {"reason": "insurance"}).code == "HANDED_OFF"
    again = call_tool(env.live(lead_id, [], job_id=2), "hand_off", {"reason": "insurance"})
    assert again.code == "ALREADY_HANDED_OFF" and again.fixed_texts == []
    assert env.one("select count(*) as n from lead_reviews where lead_id=%s", lead_id)["n"] == 1
    # Another reason is something new for the front desk.
    other = call_tool(env.live(lead_id, [], job_id=2), "hand_off", {"reason": "wants_person"})
    assert other.code == "HANDED_OFF" and other.fixed_texts
    # A case change, then on the next reply both its report and the hand-off are repeats.
    tools = env.live(lead_id, [], job_id=3)
    assert call_tool(tools, "report_outcome", {"status": "case_change", "notes": "shoulder"}).code \
        == "REPORTED"
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).fixed_texts  # same reply
    tools = env.live(lead_id, [], job_id=4)
    assert call_tool(tools, "report_outcome", {"status": "case_change"}).code == "ALREADY_REPORTED"
    assert call_tool(tools, "hand_off", {"reason": "different_case"}).code == "ALREADY_HANDED_OFF"
    cards = env.one("select count(*) as n from lead_reviews where lead_id=%s and code=%s",
                    lead_id, "case_change_asked")["n"]
    assert cards == 1
    # The emergency text goes once an hour.
    assert call_tool(env.live(lead_id, [], job_id=5), "flag_emergency", {}).fixed_texts == [
        texts.EMERGENCY_TEXT]
    repeat = call_tool(env.live(lead_id, [], job_id=6), "flag_emergency", {})
    assert repeat.code == "ALREADY_SENT" and repeat.fixed_texts == []
    env.db.execute("update lead_reviews set flagged_at=now()-interval '2 hours' where lead_id=%s "
                   "and details->>'reason'='emergency'", (lead_id,))
    assert call_tool(env.live(lead_id, [], job_id=7), "flag_emergency", {}).code == "EMERGENCY_SENT"


def test_a_hand_off_or_case_change_after_its_card_was_resolved_is_a_new_request(env):
    # Review, 2026-11-02: a card staff already resolved (or Elliot settled) counted as a repeat for
    # 24 h, so the patient asking again opened nothing and got no front desk text.
    lead_id = env.lead()
    assert call_tool(env.live(lead_id, [], job_id=1), "hand_off",
                     {"reason": "insurance"}).code == "HANDED_OFF"
    tools = env.live(lead_id, [], job_id=2)
    assert call_tool(tools, "report_outcome", {"status": "case_change", "notes": "back"}).code \
        == "REPORTED"
    env.db.execute("update lead_reviews set resolved_at=now(),resolved_by='staff' where lead_id=%s",
                   (lead_id,))
    again = call_tool(env.live(lead_id, [], job_id=3), "hand_off", {"reason": "insurance"})
    assert again.code == "HANDED_OFF" and again.fixed_texts
    assert call_tool(env.live(lead_id, [], job_id=4), "report_outcome",
                     {"status": "case_change", "notes": "back"}).code == "REPORTED"
    open_cards = env.one("select count(*) as n from lead_reviews where lead_id=%s "
                         "and resolved_at is null", lead_id)["n"]
    assert open_cards == 2


def test_a_wrong_surname_reads_back_the_real_therapist(env):
    env.open_and_sync()
    lead_id = env.lead()
    tools = env.live(lead_id, [Turn("patient", "tue 9 with thao", None)])
    card = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM",
                                              "therapist": "Thao Smith"})
    assert card.code == "CARD_SENT" and "with Thao Nguyen\n" in card.fixed_texts[0]
    assert call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM",
                                              "therapist": "Maria Lopez"}).code == "NO_SUCH_THERAPIST"
    found = call_tool(tools, "find_times", {"when": "specific_date", "specific_date": DAY1,
                                            "therapist": "Thao Smith"})
    assert found.code == "OPENINGS" and "Thao" in found.output


def add_pelvic(env):
    case_type = env.one(
        "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes,"
        "is_default,aliases) values(%s,'Pelvic Health',1453,60,false,array['pelvic floor']) "
        "returning id", env.practice_id)["id"]
    env.db.execute(
        "insert into clinician_assignments(clinician_id,booking_location_id,case_type_id) "
        "select c.id,bl.id,%s from clinicians c join booking_locations bl "
        "on bl.practice_id=c.practice_id where c.practice_id=%s and c.display_name='Thao Nguyen'",
        (case_type, env.practice_id))


def test_a_new_persons_service_reaches_the_card_and_the_booking(env):
    # np-03: a new person asked for pelvic floor and got a Physical Therapy card.
    add_pelvic(env)
    env.open_and_sync()
    before = counts(env)
    setup = SandboxSetup(who="new", clinic="Laguna Niguel", first_name="Brooke",
                         last_name="Tanner", date_of_birth=date(1993, 2, 9),
                         practice_slug=env.practice_slug)
    from rpt_agent.services.sms_agent import sandbox_lead
    tools = SandboxTools(env.trace, lead=sandbox_lead(setup),
                         history=[Turn("patient", "Feb 9, 1993", None)],
                         practice_id=env.practice_id, now_local=None, settings=env.settings())
    found = call_tool(tools, "find_times", {"when": "earliest", "service": "pelvic floor"})
    assert found.code == "OPENINGS" and "Alan" not in found.output
    card = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM",
                                              "service": "pelvic floor"})
    assert card.code == "CARD_SENT"
    assert "with Thao Nguyen\nPelvic Health\nLaguna Niguel office" in card.fixed_texts[0]
    tools.history = [Turn("elliot", card.fixed_texts[0], None), Turn("patient", "yes", None)]
    # The service must be the card's.
    refused = call_tool(tools, "book_visit", {**BOOK, "service": "Physical Therapy"})
    assert refused.code == "NOT_CONFIRMED" and "service" in refused.output
    booked = call_tool(tools, "book_visit", {**BOOK, "service": "Pelvic Health"})
    assert booked.code == "BOOKED", booked.output
    assert call_tool(tools, "book_visit", {**BOOK}).code == "BOOKED"  # the card's service
    # A default-service card has no service line.
    tools.history = [Turn("patient", "Feb 9, 1993", None)]
    plain = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "11:00 AM"})
    assert plain.code == "CARD_SENT" and "Physical Therapy" not in plain.fixed_texts[0]
    assert counts(env) == before


def test_another_service_for_a_referral_is_a_case_change_live(env):
    add_pelvic(env)
    env.open_and_sync()
    lead_id = env.lead()
    tools = env.live(lead_id, [Turn("patient", "pelvic floor tue?", None)])
    for tool, arguments in (("find_times", {"when": "earliest"}),
                            ("confirm_visit", {"date": DAY1, "time": "9:00 AM"})):
        result = call_tool(tools, tool, {**arguments, "service": "pelvic floor"})
        assert result.code == "CASE_CHANGE" and not result.fixed_texts, result.output
    # Its own service by another word is fine.
    found = call_tool(tools, "find_times", {"when": "earliest", "service": "physical therapy"})
    assert found.code == "OPENINGS"
    tools, _ = card_flow(env, lead_id)
    assert call_tool(tools, "book_visit", {**BOOK, "service": "Pelvic Health"}).code == "CASE_CHANGE"
    assert call_tool(tools, "book_visit", {**BOOK, "service": "Physical Therapy"}).code == "BOOKED"


# --- round 2 (gpt-5.5 eval, 2026-11-02) -------------------------------------------------------------

def test_no_therapist_at_the_clinic_names_the_clinics_that_offer_it(env):
    # np-03: pelvic at Dana Point had no therapist and the patient only got the front desk.
    add_pelvic(env)
    env.db.execute("insert into booking_locations(practice_id,name,stride_location_id,timezone) "
                   "values(%s,'Dana Point',3171,'America/New_York')", (env.practice_id,))
    env.open_and_sync()
    offered = ("Offered at: Laguna Niguel. Ask if one of those works.")
    lead_id = env.lead()
    live = call_tool(env.live(lead_id, [Turn("patient", "dana point?", None)]), "find_times",
                     {"when": "earliest", "clinic": "Dana Point"})
    assert live.output == ("NO_CLINICIANS: no Physical Therapy therapist at Dana Point. " + offered)
    from rpt_agent.services.sms_agent import sandbox_lead
    new = sandbox_lead(SandboxSetup(who="new", clinic="Dana Point", practice_slug=env.practice_slug))
    tools = SandboxTools(env.trace, lead=new, history=[], practice_id=env.practice_id,
                         now_local=None, settings=env.settings())
    sandbox = call_tool(tools, "find_times", {"when": "earliest", "service": "pelvic floor"})
    assert sandbox.output == ("NO_CLINICIANS: no Pelvic Health therapist at Dana Point. " + offered)


def test_a_lead_with_no_first_name_books_only_under_a_name_it_wrote(env):
    # x-04: booked with the last name as the first name, "You're all set, Quill!".
    env.open_and_sync()
    lead_id = env.lead()
    # The full name is only the last name: no first name on file at all.
    env.db.execute("update leads set first_name=null,full_name='Tester' where id=%s", (lead_id,))
    with get_pool().connection() as c:
        view = load_live(c, lead_id).lead
    assert (view.first_name, view.last_name) == ("", "Tester")
    pick = {"date": DAY1, "time": "9:00 AM", "therapist": "Thao Nguyen"}
    # Eval 2026-11-03 (x-04): the first name is asked BEFORE the card, never after the yes.
    tools = env.live(lead_id, [Turn("patient", "tue morning?", None)])
    asked = call_tool(tools, "confirm_visit", pick)
    assert asked.code == "MISSING_NAME" and not asked.fixed_texts
    assert "first name only" in asked.output
    tools = env.live(lead_id, [Turn("patient", "it's Robin, tue morning?", None)])
    card = call_tool(tools, "confirm_visit", {**pick, "first_name": "Robin"})
    assert card.code == "CARD_SENT" and "No first name is on file" in card.output
    # book_visit keeps its own guard: a yes with no first name in the chat is still refused.
    yes = [Turn("patient", "tue morning?", None), Turn("elliot", card.fixed_texts[0], None),
           Turn("patient", "YES", None)]
    refused = call_tool(env.live(lead_id, yes), "book_visit", {**BOOK, "first_name": "Tester"})
    assert refused.code == "MISSING_NAME"
    assert env.one("select status from leads where id=%s", lead_id)["status"] == "in_progress"
    named = [Turn("patient", "it's Robin, tue morning?", None), *yes[1:]]
    booked = call_tool(env.live(lead_id, named), "book_visit", {**BOOK, "first_name": "Robin"})
    assert booked.code == "BOOKED", booked.output
    assert booked.fixed_texts[0].startswith("You're all set, Robin!")


def say(value):
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": value}]}]}


def tool(name, **arguments):
    return {"output": [{"type": "function_call", "name": name, "call_id": f"c_{name}",
                        "arguments": json.dumps(arguments)}]}


def test_sandbox_missing_name_then_first_name_sends_the_card_again_and_books(env):
    # x-04 (round 3), end to end in the test chat: yes to the card -> MISSING_NAME -> "What's your
    # first name?" -> "Dana" -> the card again -> yes -> booked under Dana; nothing written.
    env.open_and_sync()
    before = counts(env)
    setup = SandboxSetup(clinic="Laguna Niguel", first_name="", last_name="Quill",
                         date_of_birth=date(1992, 7, 8), practice_slug=env.practice_slug)
    card = texts.confirm_card(date(2026, 6, 16), slot_booking.parse_clock("9:00"), "Thao Nguyen",
                              "Laguna Niguel")
    pick = {"date": DAY1, "time": "9:00 AM", "therapist": "Thao Nguyen", "clinic": None}
    chat = [Turn("patient", "tue 9am, born 7/8/1992", None), Turn("elliot", card, None),
            Turn("patient", "yes", None)]
    asked = "What's your first name?"
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=FakeClient(
        tool("book_visit", **pick, first_name="Quill", last_name="Quill"), say(asked)))
    assert [a.result_code for a in reply.actions] == ["MISSING_NAME"] and reply.texts == [asked]

    # The name: book_visit at once is refused (the card is not the latest text); the card goes
    # out again.
    chat += [Turn("elliot", asked, None), Turn("patient", "Dana", None)]
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=FakeClient(
        tool("book_visit", **pick, first_name="Dana"), tool("confirm_visit", **pick),
        say("NO_REPLY")))
    assert [a.result_code for a in reply.actions] == ["NOT_CONFIRMED", "CARD_SENT"]
    assert reply.texts == [card]

    chat += [Turn("elliot", card, None), Turn("patient", "yes", None)]
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=FakeClient(
        tool("book_visit", **pick, first_name="Dana", last_name="Quill"), say("NO_REPLY")))
    assert [a.result_code for a in reply.actions] == ["BOOKED"]
    assert reply.texts[0].startswith("You're all set, Dana!")
    assert counts(env) == before


def test_sandbox_promise_of_the_front_desk_without_a_hand_off_is_not_sent(env):
    # x-04 (round 3): "our front desk will finish setting up your visit" after the patient gave a
    # name, with no hand_off: re-run with the note; the card goes out instead.
    env.open_and_sync()
    setup = SandboxSetup(clinic="Laguna Niguel", first_name="", last_name="Quill",
                         date_of_birth=date(1992, 7, 8), practice_slug=env.practice_slug)
    pick = {"date": DAY1, "time": "9:00 AM", "therapist": "Thao Nguyen", "clinic": None}
    chat = [Turn("patient", "7/8/1992", None), Turn("elliot", "What's your first name?", None),
            Turn("patient", "Dana", None)]
    client = FakeClient(
        say("I couldn't finish booking that by text, so our front desk will finish setting up "
            "your visit."),
        tool("confirm_visit", **pick), say("NO_REPLY"))
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=client)
    assert [a.result_code for a in reply.actions] == ["CARD_SENT"]
    assert reply.texts[0].startswith(texts.CARD_FIRST_LINE) and len(reply.texts) == 1
    assert any(item.get("content") == sms_agent.FRONT_DESK_NOTE
               for item in client.responses.requests[1]["input"] if isinstance(item, dict))


def test_sandbox_tomorrow_search_names_the_day_on_the_calendar(env):
    # book-12 (round 3): the pinned calendar's tomorrow (Tue Jun 16) is named in the reply.
    env.open_and_sync()
    setup = SandboxSetup(clinic="Laguna Niguel", first_name="Sam", practice_slug=env.practice_slug)
    from rpt_agent.services.sms_agent import sandbox_lead
    tools = SandboxTools(env.trace, lead=sandbox_lead(setup),
                         history=[Turn("patient", "tmrw aftrnoon?", None)],
                         practice_id=env.practice_id, now_local=None, settings=env.settings())
    found = call_tool(tools, "find_times", {"when": "today", "time_of_day": "afternoon"})
    assert found.code == "NOTHING_IN_RANGE", found.output
    assert found.output.startswith("NOTHING_IN_RANGE: Searched tomorrow, Tue Jun 16 (the patient "
                                   "said tomorrow).")
    morning = call_tool(tools, "find_times", {"when": "tomorrow"})
    assert morning.output.startswith("OPENINGS: Searched tomorrow, Tue Jun 16. Name that day")


# --- birthday step (owner, 2026-11-03) ---------------------------------------------------------------
#
# Test chat: "is your birthday November 1?" -> card -> "yes" -> "send your date of birth with the
# year" -> "nov 1, 2026" -> the same card -> "yes" -> the same question, never booked: the booking
# engine compared the birthday with the demo's pinned calendar day. The patient now types their date
# of birth, code checks it, and a second failed booking for the same reason hands off.

def test_sandbox_birthday_step_end_to_end_on_the_pinned_calendar(env):
    env.open_and_sync()
    before = counts(env)
    setup = SandboxSetup(clinic="Laguna Niguel", first_name="Sam", last_name="Test",
                         date_of_birth=date(1990, 3, 14), practice_slug=env.practice_slug)
    pick = {"date": DAY1, "time": "9:00 AM", "therapist": "Thao Nguyen", "clinic": None}
    asked = "To confirm it's you, what's your date of birth?"
    chat = [Turn("patient", "tue 9am", None), Turn("elliot", asked, None),
            Turn("patient", "nov 1, 2026", None)]
    client = FakeClient(tool("check_birthday", date_of_birth="nov 1, 2026"),
                        tool("confirm_visit", **pick), say("Could you check it and send it again?"))
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=client)
    assert [a.result_code for a in reply.actions] == ["NOT_POSSIBLE", "NEED_BIRTH_DATE"]
    assert reply.texts == ["Could you check it and send it again?"]
    assert "Date of birth confirmed in this chat: no" in client.responses.requests[0]["input"][1][
        "content"]

    chat += [Turn("elliot", reply.texts[0], None), Turn("patient", "3/14/1990", None)]
    client = FakeClient(tool("check_birthday", date_of_birth="3/14/1990"),
                        tool("confirm_visit", **pick), say("NO_REPLY"))
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=client)
    assert [a.result_code for a in reply.actions] == ["MATCH", "CARD_SENT"]
    [card] = reply.texts
    context = client.responses.requests[0]["input"][1]["content"]
    assert "Date of birth confirmed in this chat: yes" in context and "1990" not in context

    chat += [Turn("elliot", card, None), Turn("patient", "yes", None)]
    client = FakeClient(tool("book_visit", **pick, date_of_birth="2026-11-01"), say("NO_REPLY"))
    reply = reply_sandbox(setup=setup, history=chat, settings=env.settings(), client=client)
    # The chat's confirmed date is used, whatever the model passes.
    assert [a.result_code for a in reply.actions] == ["BOOKED"]
    assert reply.texts[0].startswith("You're all set, Sam!")
    assert counts(env) == before


def test_booking_engine_accepts_a_birthday_after_the_pinned_calendar_day(env):
    # The demo calendar is pinned to Jun 15 2026 (BOOKING_TODAY_OVERRIDE); a date of birth after it
    # but before the real today is real and books (voice and text use the same check).
    env.open_and_sync()
    lead_id = env.lead()
    tools = env.live(lead_id, [Turn("patient", "tue morning?", None)])
    assert call_tool(tools, "find_times", {"when": "specific_date",
                                           "specific_date": DAY1}).code == "OPENINGS"
    real_today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    dob = real_today - timedelta(days=10)
    assert dob > TODAY
    output = slot_booking.book_slot(
        env.trace, lead_id=lead_id, call_id=f"sms:{lead_id}", settings=env.settings(),
        providers=env.stride, source="sms",
        arguments={"date": DAY1, "time": "9:00 AM", "clinician_name": "Thao Nguyen",
                   "location": "Laguna Niguel", "first_name": "Rpt", "last_name": "Tester",
                   "date_of_birth": dob.isoformat()})
    assert output.startswith("BOOKED"), output
    assert env.one("select patient_dob from appointments where lead_id=%s",
                   lead_id)["patient_dob"] == dob


def test_live_a_second_failed_booking_for_the_same_reason_hands_off(env):
    env.open_and_sync()
    lead_id = env.lead()
    # An earlier reply of this chat was refused NOT_CONFIRMED (the worker stores tool + code).
    env.db.execute("insert into sms_agent_actions(lead_id,tool,result_code) "
                   "values(%s,'book','NOT_CONFIRMED')", (lead_id,))
    tools = env.live(lead_id, [Turn("patient", "just book it", None)])
    result = call_tool(tools, "book_visit", BOOK)
    assert result.code == "HANDED_OFF" and result.action_code == "HANDED_OFF_PAUSED"
    assert result.fixed_texts and "front desk" in result.fixed_texts[0]
    review = env.one("select code,details from lead_reviews where lead_id=%s "
                     "and resolved_at is null", lead_id)
    assert review["code"] == lead_review.SMS_FRONT_DESK_CODE
    assert review["details"]["reason"] == "booking_problem"
    # Another lead's failures do not count.
    other = env.lead()
    assert call_tool(env.live(other, [Turn("patient", "book", None)]), "book_visit",
                     BOOK).code == "NOT_CONFIRMED"


def test_live_card_is_not_sent_twice_and_books_with_the_patients_double_checked_date(env):
    env.open_and_sync()
    lead_id = env.lead()   # 1990-04-26 on file
    typed = [Turn("patient", "tue 9am, born 5/5/1985", None)]
    first = call_tool(env.live(lead_id, typed, dob=False), "confirm_visit",
                      {"date": DAY1, "time": "9:00 AM", "therapist": "Thao"})
    assert first.code == "NEED_BIRTH_DATE" and "double-check" in first.output
    checked = [*typed, Turn("elliot", "Could you double-check that date of birth for me?", None),
               Turn("patient", "yes it's 5/5/1985", None)]
    tools = env.live(lead_id, checked, dob=False)
    assert call_tool(tools, "check_birthday", {"date_of_birth": "5/5/1985"}).code == \
        "CONFIRMED_DIFFERENT"
    card = call_tool(tools, "confirm_visit", {"date": DAY1, "time": "9:00 AM", "therapist": "Thao"})
    assert card.code == "CARD_SENT"
    again = call_tool(env.live(lead_id, [*checked, Turn("elliot", card.fixed_texts[0], None)],
                               dob=False),
                      "confirm_visit", {"date": DAY1, "time": "9:00 AM", "therapist": "Thao"})
    assert again.code == "CARD_ALREADY_SENT" and not again.fixed_texts
    yes = [*checked, Turn("elliot", card.fixed_texts[0], None), Turn("patient", "yes", None)]
    booked = call_tool(env.live(lead_id, yes, dob=False), "book_visit",
                       {**BOOK, "date_of_birth": "1990-04-26"})
    assert booked.code == "BOOKED", booked.output
    assert "differs from our records" in booked.summary
    assert env.one("select patient_dob from appointments where lead_id=%s",
                   lead_id)["patient_dob"] == date(1985, 5, 5)
