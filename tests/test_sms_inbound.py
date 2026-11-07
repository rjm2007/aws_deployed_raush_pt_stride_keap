"""SMS agent inbound side without a database (docs/SMS_AGENT.md "Flow", "Should we reply").

Routing (keyword vs agent), every should_reply rule and its order, and the worker's handling of
an agent error, a send failure and a stop that arrives while the agent is thinking. The SQL itself
runs on a real schema in test_sms_inbound_integration.py.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

import rpt_agent.services as rpt_services
from rpt_agent import sms_agent_worker
from rpt_agent.config import Settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderError
from rpt_agent.routes import twilio as twilio_routes
from rpt_agent.services import sms_agent_texts as real_texts
from rpt_agent.services import sms_inbound
from rpt_agent.services.sms_inbound import Decision, ReplyFacts, inbound_command, should_reply


def _settings(**overrides) -> Settings:
    values = {"sms_agent_enabled": True, "sms_agent_test_leads_only": True,
              "sms_agent_daily_reply_cap": 10}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _lead(**overrides):
    lead = {"id": "lead-1", "status": "in_progress", "cadence_state": "active",
            "sms_opt_out": False, "sms_disabled": False, "is_test": True}
    lead.update(overrides)
    return lead


def _facts(**overrides) -> ReplyFacts:
    values = {"lead": _lead()}
    values.update(overrides)
    return ReplyFacts(**values)


# --- keywords ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("body", "command"), [
    ("STOP", "stop"), (" stop ", "stop"), ("Unsubscribe", "stop"), ("quit", "stop"),
    ("START", "start"), ("unstop", "start"), ("HELP", "help"), ("info", "help"), ("Call", "call"),
    ("yes", None), ("YES", None), ("stop texting me please", None), ("call me tomorrow", None),
    ("", None), ("Can I book Tuesday?", None),
])
def test_inbound_command_only_matches_a_whole_keyword(body, command):
    # YES is how a patient confirms a booking: never a keyword here.
    assert inbound_command(body) == command


# --- should_reply: each rule ------------------------------------------------------------------


def test_agent_answers_a_plain_text_from_a_test_lead():
    assert should_reply(_facts(), _settings()) == Decision("agent", "agent")


def test_switched_off_stores_only():
    assert should_reply(_facts(), _settings(sms_agent_enabled=False)).reason == "agent_disabled"


@pytest.mark.parametrize(("facts", "reason"), [
    ({"suppressed": True}, "suppressed"),
    ({"lead": _lead(status="do_not_contact")}, "do_not_contact"),
    ({"lead": _lead(sms_opt_out=True)}, "sms_opt_out"),
    ({"lead": _lead(sms_disabled=True)}, "texts_off"),
    ({"lead": _lead(is_test=False)}, "not_test_lead"),
    ({"lead": _lead(status="invalid_phone")}, "wrong_person"),
    ({"staff_texted_recently": True}, "staff_took_over"),
    ({"has_new_text": False}, "nothing_to_answer"),
])
def test_each_rule_that_sends_nothing(facts, reason):
    decision = should_reply(_facts(**facts), _settings())
    assert (decision.action, decision.reason, decision.flag_review) == ("skip", reason, False)


def test_real_patients_get_the_agent_once_test_leads_only_is_off():
    decision = should_reply(_facts(lead=_lead(is_test=False)),
                            _settings(sms_agent_test_leads_only=False, sms_agent_phi_approved=True))
    assert decision.action == "agent"


@pytest.mark.parametrize("status", ["declined", "closed_no_response", "booking_link_sent", "booked",
                                    "needs_attention"])
def test_declined_closed_link_sent_and_booked_leads_still_get_the_agent(status):
    # Owner, 2026-11-02: a patient texting back after a decline is interest.
    assert should_reply(_facts(lead=_lead(status=status, cadence_state="completed")),
                        _settings()).action == "agent"


def test_a_call_in_flight_retries_later():
    assert should_reply(_facts(call_in_flight=True), _settings()) == Decision("retry", "call_in_progress")


def test_the_daily_cap_sends_nothing_and_flags_once():
    first = should_reply(_facts(ai_replies_today=10), _settings())
    again = should_reply(_facts(ai_replies_today=12, cap_flagged_today=True), _settings())
    assert (first.action, first.reason, first.flag_review) == ("skip", "reply_cap", True)
    assert (again.action, again.flag_review) == ("skip", False)
    assert should_reply(_facts(ai_replies_today=9), _settings()).action == "agent"


def test_a_photo_with_no_words_gets_the_text_only_line():
    decision = should_reply(_facts(media_only=True), _settings())
    assert (decision.action, decision.text_key) == ("fixed_text", "text_only")


def test_a_number_that_is_not_a_lead_gets_the_fixed_text_once_a_day():
    first = should_reply(ReplyFacts(lead=None), _settings())
    assert (first.action, first.text_key, first.reason) == ("fixed_text", "unknown_number",
                                                           "unknown_number")
    assert should_reply(ReplyFacts(lead=None, unknown_number_recent=True),
                        _settings()).reason == "unknown_number_recent"
    # Never the AI, and a suppressed number gets nothing at all.
    assert should_reply(ReplyFacts(lead=None, suppressed=True), _settings()).reason == "suppressed"


# --- should_reply: the order ------------------------------------------------------------------


def test_opt_out_beats_everything_after_it():
    facts = _facts(lead=_lead(sms_opt_out=True, is_test=False), staff_texted_recently=True,
                   call_in_flight=True, ai_replies_today=50, media_only=True)
    assert should_reply(facts, _settings()).reason == "sms_opt_out"


def test_staff_quiet_beats_a_call_in_flight_and_the_cap():
    facts = _facts(staff_texted_recently=True, call_in_flight=True, ai_replies_today=50)
    assert should_reply(facts, _settings()).reason == "staff_took_over"


def test_a_call_in_flight_beats_the_cap_and_a_photo():
    facts = _facts(call_in_flight=True, ai_replies_today=50, media_only=True)
    assert should_reply(facts, _settings()).action == "retry"


def test_the_cap_beats_the_text_only_line():
    assert should_reply(_facts(ai_replies_today=10, media_only=True), _settings()).reason == "reply_cap"


def test_hard_stop_is_what_still_blocks_a_reply_already_written():
    assert sms_inbound.hard_stop(_facts()) is None
    assert sms_inbound.hard_stop(_facts(call_in_flight=True, ai_replies_today=99)) is None
    assert sms_inbound.hard_stop(_facts(lead=_lead(sms_opt_out=True))) == "sms_opt_out"
    assert sms_inbound.hard_stop(_facts(staff_texted_recently=True)) == "staff_took_over"
    assert sms_inbound.hard_stop(_facts(suppressed=True)) == "suppressed"


# --- the webhook ------------------------------------------------------------------------------


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _InboundConn:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if "insert into sms_messages" in sql:
            return _Result(None if self.duplicate else {"id": 41})
        return _Result(None)


@pytest.fixture
def inbound(monkeypatch):
    state = {"conn": _InboundConn(), "queued": [], "opt_outs": [],
             "settings": _settings()}

    @contextmanager
    def fake_transaction():
        yield state["conn"]

    monkeypatch.setattr(twilio_routes, "transaction", fake_transaction)
    monkeypatch.setattr(twilio_routes, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(twilio_routes.sms_inbound, "active_lead_id", lambda conn, phone: "lead-1")
    monkeypatch.setattr(twilio_routes.sms_inbound, "queue_reply",
                        lambda conn, phone, lead_id, inbound_id, settings=None:
                        state["queued"].append((phone, lead_id, inbound_id)) or 7)
    monkeypatch.setattr(twilio_routes, "explicit_opt_out",
                        lambda trace, phone, channel, source: state["opt_outs"].append(channel))
    monkeypatch.setattr(twilio_routes, "enqueue_sheet_update", lambda conn, **kw: None)
    return state


def _stop_sql(state):
    return [sql for sql, _ in state["conn"].sql
            if "suppressed_numbers" in sql or "update sms_agent_jobs" in sql]


def _receive(body, sid="SM1", num_media=0):
    twilio_routes._handle_inbound_sms(WorkflowTrace("t", "test"), "+15550001111", body, sid, num_media)


def test_a_text_is_stored_as_the_patients_and_queued_for_the_agent(inbound):
    _receive("Do you have anything Tuesday?")
    insert = next(sql for sql, _ in inbound["conn"].sql if "insert into sms_messages" in sql)
    assert "'patient'" in insert and "on conflict(provider_message_id) do nothing" in insert
    assert inbound["queued"] == [("+15550001111", "lead-1", 41)]


def test_stop_opts_out_and_never_reaches_the_agent(inbound):
    _receive("STOP")
    assert inbound["opt_outs"] == ["sms"] and inbound["queued"] == []
    # A reply queued before the STOP is closed; the lead's own opt-out covers the number.
    stop_sql = _stop_sql(inbound)
    assert len(stop_sql) == 1 and "status='skipped'" in stop_sql[0]


def test_stop_from_a_number_that_is_no_lead_suppresses_it(inbound, monkeypatch):
    # explicit_opt_out marks lead rows only: with no lead, the queued "hi" job's unknown-number
    # text (and tomorrow's) would still go out after the STOP.
    monkeypatch.setattr(twilio_routes.sms_inbound, "active_lead_id", lambda conn, phone: None)
    _receive("STOP")
    stop_sql = _stop_sql(inbound)
    assert any("insert into suppressed_numbers" in sql for sql in stop_sql)
    assert any("update sms_agent_jobs" in sql and "status='pending'" in sql for sql in stop_sql)


@pytest.mark.parametrize("body", ["START", "HELP"])
def test_start_and_help_stay_with_twilio(inbound, body):
    _receive(body)
    assert inbound["queued"] == [] and inbound["opt_outs"] == []


def test_call_sets_a_callback_and_is_not_queued(inbound):
    _receive("CALL")
    assert inbound["queued"] == []
    assert any("callback_scheduled" in sql for sql, _ in inbound["conn"].sql)


def test_switched_off_stores_the_text_and_queues_nothing(inbound):
    inbound["settings"] = _settings(sms_agent_enabled=False)
    _receive("hello")
    assert any("insert into sms_messages" in sql for sql, _ in inbound["conn"].sql)
    assert inbound["queued"] == []


def test_the_same_message_sid_twice_queues_once(inbound):
    # Twilio retries a slow webhook with the same MessageSid: the second insert does nothing.
    _receive("hello", sid="SM9")
    inbound["conn"].duplicate = True
    _receive("hello", sid="SM9")
    assert len(inbound["queued"]) == 1


def test_a_photo_with_no_words_is_queued_and_an_empty_text_is_not(inbound):
    _receive("", num_media=1)
    assert len(inbound["queued"]) == 1
    _receive("", sid="SM2", num_media=0)
    assert len(inbound["queued"]) == 1


# --- the worker -------------------------------------------------------------------------------


@dataclass
class _Action:
    kind: str
    summary: str = ""
    result_code: str = ""


@dataclass
class _Reply:
    texts: list[str] = field(default_factory=list)
    actions: list = field(default_factory=list)
    no_reply: bool = False
    error: str | None = None
    opted_out: bool = False


class _WorkerConn:
    def __init__(self, log, lead_row=None):
        self.log = log
        self.lead_row = lead_row

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        if "insert into sms_messages" in sql:
            return _Result({"id": 100 + len(self.log)})
        if "sms_opt_out,sms_disabled from leads" in sql:
            return _Result(self.lead_row)
        return _Result(None)


class _Twilio:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent: list[str] = []

    def send_sms(self, trace, to, body):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(body)
        return f"SMai{len(self.sent)}"


@pytest.fixture
def worker(monkeypatch):
    state = {"log": [], "facts": [_facts()], "reply": _Reply(texts=["Hi!"]), "flags": [],
             "twilio": _Twilio(), "cards": [], "activity": [],
             # The lead as the failure card sees it (after the agent ran).
             "lead_row": {"status": "in_progress", "cadence_state": "active",
                          "sms_opt_out": False, "sms_disabled": False}}

    @contextmanager
    def fake_transaction():
        yield _WorkerConn(state["log"], state["lead_row"])

    def facts(conn, job, lead_id, settings):
        return state["facts"][0] if len(state["facts"]) == 1 else state["facts"].pop(0)

    def reply_live(trace, *, lead_id, job_id, settings=None, client=None, providers=None):
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    monkeypatch.setattr(sms_agent_worker, "transaction", fake_transaction)
    monkeypatch.setattr(sms_inbound, "active_lead_id", lambda conn, phone: "lead-1")
    monkeypatch.setattr(sms_inbound, "load_facts", facts)
    monkeypatch.setattr(sms_agent_worker, "flag_lead_for_review",
                        lambda conn, lead_id, reason, **kw: state["flags"].append(kw["code"]))
    monkeypatch.setattr(sms_agent_worker, "record_review",
                        lambda conn, lead_id, code, message, **kw:
                        state["cards"].append((code, kw["details"])) or 1)
    monkeypatch.setattr(sms_agent_worker, "record_status",
                        lambda conn, lead_id, old, new, source, reason:
                        state["activity"].append((old, new, reason)))
    monkeypatch.setattr(sms_agent_worker, "SEND_RETRY_DELAY_SECONDS", 0)
    fake_agent = types.ModuleType("rpt_agent.services.sms_agent")
    fake_agent.reply_live = reply_live
    monkeypatch.setitem(sys.modules, "rpt_agent.services.sms_agent", fake_agent)
    fake_texts = types.ModuleType("rpt_agent.services.sms_agent_texts")
    fake_texts.render = lambda conn, key, **values: f"<{key}>"
    # The worker tells Elliot's own words from the fixed texts by their templates (the drafts here).
    fake_texts.template_body = real_texts.template_body
    fake_texts.EMERGENCY_TEXT = real_texts.EMERGENCY_TEXT
    fake_texts.is_emergency_text = real_texts.is_emergency_text
    fake_texts.parse_card = real_texts.parse_card
    monkeypatch.setitem(sys.modules, "rpt_agent.services.sms_agent_texts", fake_texts)
    # `from .services import sms_agent_texts` reads the package attribute first: once any earlier
    # test imported the real module, only replacing it there swaps it.
    monkeypatch.setattr(rpt_services, "sms_agent_texts", fake_texts, raising=False)
    return state


def _run(state, settings=None):
    providers = types.SimpleNamespace(twilio=state["twilio"])
    job = {"id": 5, "lead_id": "lead-1", "phone_e164": "+15550001111", "last_inbound_id": 41}
    return sms_agent_worker.process_job(WorkflowTrace("t", "test"), job, providers,
                                        settings or _settings())


def _finished(state):
    return [params for sql, params in state["log"] if "finished_at=now()" in sql and "status=%s" in sql]


def test_worker_sends_the_agents_texts_as_ai_and_records_the_actions(worker):
    worker["reply"] = _Reply(texts=["Here's what I have open:", "Which one works?"],
                             actions=[_Action("find_times", result_code="OPENINGS")])
    assert _run(worker) == "replied"
    assert worker["twilio"].sent == ["Here's what I have open:", "Which one works?"]
    inserts = [(sql, p) for sql, p in worker["log"] if "insert into sms_messages" in sql]
    assert len(inserts) == 2 and all("'ai'" in sql for sql, _ in inserts)
    assert [p[2] for _, p in inserts] == ["SMai1", "SMai2"]
    actions = [p for sql, p in worker["log"] if "insert into sms_agent_actions" in sql]
    assert actions == [(5, "lead-1", "find_times", "OPENINGS")]
    assert _finished(worker)[-1][:2] == ("done", "replied")


def test_worker_skips_without_calling_the_agent(worker):
    worker["facts"] = [_facts(staff_texted_recently=True)]
    worker["reply"] = AssertionError("the agent must not run")
    assert _run(worker) == "staff_took_over"
    assert worker["twilio"].sent == [] and _finished(worker)[-1][:2] == ("skipped", "staff_took_over")


def test_worker_reply_cap_flags_staff(worker):
    worker["facts"] = [_facts(ai_replies_today=10)]
    assert _run(worker) == "reply_cap"
    assert worker["flags"] == [sms_inbound.REVIEW_REPLY_CAP] and worker["twilio"].sent == []


def test_worker_puts_the_job_back_while_a_call_is_live(worker):
    worker["facts"] = [_facts(call_in_flight=True)]
    assert _run(worker) == "call_in_progress"
    assert worker["twilio"].sent == []
    assert any("status='pending'" in sql and "due_at=now()+make_interval" in sql
               for sql, _ in worker["log"])


def test_an_agent_error_sends_nothing_fails_the_job_and_flags_staff(worker):
    worker["reply"] = _Reply(error="timeout")
    assert _run(worker) == "agent_error"
    assert worker["twilio"].sent == [] and worker["flags"] == [sms_inbound.REVIEW_AGENT_FAILED]
    assert _finished(worker)[-1][:2] == ("failed", "agent_error")


def test_an_agent_exception_is_handled_the_same_way(worker):
    worker["reply"] = RuntimeError("boom")
    assert _run(worker) == "agent_error"
    assert worker["twilio"].sent == [] and worker["flags"] == [sms_inbound.REVIEW_AGENT_FAILED]


def test_no_reply_needed_sends_nothing(worker):
    worker["reply"] = _Reply(no_reply=True)
    assert _run(worker) == "no_reply" and worker["twilio"].sent == []


def test_a_photo_gets_the_text_only_line_without_the_agent(worker):
    worker["facts"] = [_facts(media_only=True)]
    worker["reply"] = AssertionError("the agent must not run")
    assert _run(worker) == "fixed_text"
    assert worker["twilio"].sent == ["<text_only>"]


def test_an_opt_out_while_the_agent_thinks_stops_the_send(worker):
    worker["facts"] = [_facts(), _facts(lead=_lead(sms_opt_out=True))]
    assert _run(worker) == "sms_opt_out"
    assert worker["twilio"].sent == []
    assert _finished(worker)[-1][:2] == ("skipped", "stopped_before_send:sms_opt_out")


@pytest.mark.parametrize("stopped", [_lead(sms_opt_out=True), _lead(status="do_not_contact")])
def test_elliots_own_opt_out_still_sends_its_one_reply(worker, stopped):
    # "Stop texting me": Elliot opted the patient out in this reply; "We won't text you again."
    # still goes out (like the STOP keyword's own answer). Nothing after that (should_reply).
    worker["facts"] = [_facts(), _facts(lead=stopped)]
    worker["reply"] = _Reply(texts=["Understood. We won't text you again."], opted_out=True)
    assert _run(worker) == "replied"
    assert worker["twilio"].sent == ["Understood. We won't text you again."]


def test_elliots_opt_out_does_not_override_staff_taking_over(worker):
    worker["facts"] = [_facts(), _facts(staff_texted_recently=True)]
    worker["reply"] = _Reply(texts=["Understood."], opted_out=True)
    assert _run(worker) == "staff_took_over" and worker["twilio"].sent == []


def test_a_clear_send_refusal_is_retried_once(worker):
    worker["twilio"] = _Twilio([ProviderError("twilio", "http_503", "busy", retryable=True)])
    assert _run(worker) == "replied" and worker["twilio"].sent == ["Hi!"]


def test_a_send_failure_after_the_retry_fails_the_job_no_storm(worker):
    errors = [ProviderError("twilio", "http_503", "busy", retryable=True) for _ in range(5)]
    worker["twilio"] = _Twilio(errors)
    assert _run(worker) == "send_failed"
    assert len(worker["twilio"].failures) == 3  # first try + one retry, nothing more
    assert worker["flags"] == [sms_inbound.REVIEW_AGENT_FAILED]
    assert _finished(worker)[-1][:2] == ("failed", "send_failed")


def test_an_ambiguous_send_is_never_retried(worker):
    worker["twilio"] = _Twilio([ProviderError("twilio", "timeout", "maybe", ambiguous=True),
                                ProviderError("twilio", "x", "x")])
    assert _run(worker) == "send_failed" and len(worker["twilio"].failures) == 1


# --- a booking is always confirmed; failure cards reach staff; opt-out sends only its line -----


def _booked_text(first_name="Sam"):
    text = real_texts.fill(real_texts.DRAFTS["booked"], {
        "first_name": first_name, "day": "Tuesday, November 3", "time": "9:00 AM",
        "therapist": "Alan", "clinic": "Laguna Niguel",
        "address": real_texts.address_for("Laguna Niguel")})
    return text.replace("set, !", "set!")


def _front_desk_text():
    return real_texts.fill(real_texts.DRAFTS["front_desk"], {"front_desk_phone": "(949) 555-0100"})


_BOOKED = _Action("book", result_code="BOOKED")


@pytest.mark.parametrize(("stopped", "stop"), [
    (_facts(staff_texted_recently=True), "staff_took_over"),
    (_facts(lead=_lead(sms_disabled=True)), "texts_off"),
    (_facts(lead=_lead(sms_opt_out=True)), "sms_opt_out"),
])
@pytest.mark.parametrize("first_name", ["Sam", ""])
def test_a_booking_made_in_the_reply_is_always_confirmed(worker, stopped, stop, first_name):
    # Staff texted, or chose calls only, while the model was still writing after book_visit: the
    # booking stands, so its confirmation goes out; only Elliot's extra words are dropped.
    booked = _booked_text(first_name)
    worker["facts"] = [_facts(), stopped]
    worker["reply"] = _Reply(texts=[booked, "Anything else I can help with?"], actions=[_BOOKED])
    assert _run(worker) == f"booked_text_only:{stop}"
    assert worker["twilio"].sent == [booked]
    assert _finished(worker)[-1][:2] == ("done", f"booked_text_only:{stop}")


def test_without_a_booking_a_stop_still_drops_the_whole_reply(worker):
    worker["facts"] = [_facts(), _facts(staff_texted_recently=True)]
    worker["reply"] = _Reply(texts=[_booked_text()], actions=[_Action("book", result_code="TAKEN")])
    assert _run(worker) == "staff_took_over" and worker["twilio"].sent == []


def test_a_failed_booked_text_on_a_booked_lead_still_opens_a_card(worker):
    # flag_lead_for_review leaves a booked lead alone; the card is written without touching it.
    worker["lead_row"] = {"status": "booked", "cadence_state": "completed", "sms_opt_out": False,
                          "sms_disabled": False}
    worker["reply"] = _Reply(texts=[_booked_text()], actions=[_BOOKED])
    worker["twilio"] = _Twilio([ProviderError("twilio", "21610", "blocked")])
    assert _run(worker) == "send_failed"
    assert worker["flags"] == []
    (code, details), = worker["cards"]
    assert code == sms_inbound.REVIEW_AGENT_FAILED
    assert details["finished"] is True and details["booked_text_unsent"] is True
    # The lead row is not paused: only needs_review is set, status and cadence stay.
    update = next(sql for sql, _ in worker["log"] if sql.startswith("update leads set needs_review"))
    assert "status" not in update and "cadence_state" not in update


@pytest.mark.parametrize("lead_row", [
    {"status": "booked", "cadence_state": "completed"},
    {"status": "declined", "cadence_state": "terminated"},
    {"status": "closed_no_response", "cadence_state": "terminated"},
    {"status": "in_progress", "cadence_state": "completed"},
])
def test_an_agent_error_on_a_finished_lead_reaches_staff(worker, lead_row):
    worker["lead_row"] = {**lead_row, "sms_opt_out": False, "sms_disabled": False}
    worker["reply"] = _Reply(error="timeout")
    assert _run(worker) == "agent_error"
    assert worker["flags"] == []
    assert [(code, details["finished"]) for code, details in worker["cards"]] == [
        (sms_inbound.REVIEW_AGENT_FAILED, True)]


def test_the_reply_cap_on_a_booked_lead_reaches_staff(worker):
    worker["lead_row"] = {"status": "booked", "cadence_state": "completed", "sms_opt_out": False,
                          "sms_disabled": False}
    worker["facts"] = [_facts(ai_replies_today=10)]
    assert _run(worker) == "reply_cap"
    assert [code for code, _ in worker["cards"]] == [sms_inbound.REVIEW_REPLY_CAP]


@pytest.mark.parametrize("opted", [{"sms_opt_out": True}, {"status": "do_not_contact"}])
def test_an_agent_failure_after_an_opt_out_asks_nobody_to_text_them(worker, opted):
    # "Stop texting me" opted the patient out, then the next model round timed out: no card that
    # pauses their calls and asks staff to text them; an Activity line only.
    worker["lead_row"] = {"status": "in_progress", "cadence_state": "active",
                          "sms_opt_out": False, "sms_disabled": False, **opted}
    worker["reply"] = _Reply(error="timeout", opted_out=True)
    assert _run(worker) == "agent_error"
    assert worker["flags"] == [] and worker["cards"] == []
    assert len(worker["activity"]) == 1 and "no review" in worker["activity"][0][2]


def test_after_an_opt_out_only_elliots_own_line_goes_out(worker):
    # The model also handed off (front desk text) and sent the intro in the same run; after the
    # opt-out only "We won't text you again." may reach the number.
    worker["facts"] = [_facts(), _facts(lead=_lead(sms_opt_out=True))]
    intro = real_texts.fill(real_texts.DRAFTS["intro"], {
        "first_name": "Sam", "booking_link": "https://book.example.test",
        "front_desk_phone": "(949) 555-0100"})
    worker["reply"] = _Reply(texts=[_front_desk_text(), intro, real_texts.EMERGENCY_TEXT,
                                    "Understood. We won't text you again."],
                             actions=[_Action("hand_off", result_code="HANDED_OFF"),
                                      _Action("report", result_code="REPORTED")],
                             opted_out=True)
    assert _run(worker) == "replied"
    assert worker["twilio"].sent == ["Understood. We won't text you again."]


def test_an_opt_out_with_an_unrecognised_booked_text_still_drops_the_other_texts(worker):
    # A Studio edit made the booked text unrecognisable, so the whole reply counts as the
    # confirmation; after an opt-out in the same reply the intro and hand-off text must still go.
    worker["facts"] = [_facts(), _facts(lead=_lead(sms_opt_out=True))]
    intro = real_texts.fill(real_texts.DRAFTS["intro"], {
        "first_name": "Sam", "booking_link": "https://book.example.test",
        "front_desk_phone": "(949) 555-0100"})
    edited_booked = "All set for Tuesday at 9:00 AM in Laguna Niguel."
    worker["reply"] = _Reply(texts=[edited_booked, _front_desk_text(), intro,
                                    "Understood. We won't text you again."],
                             actions=[_BOOKED, _Action("report", result_code="REPORTED")],
                             opted_out=True)
    assert _run(worker) == "replied"
    assert worker["twilio"].sent == [edited_booked, "Understood. We won't text you again."]
