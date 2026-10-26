"""End-of-call report vs voice booking: lock the lead before the outreach event (2026-10-26).

The booking's last step (slot_booking._finish_booking) locks the lead (mark_booked) and then
updates this call's outreach event. apply_call_outcome and the no-outcome fallback locked the
event first, then the lead: opposite order, so a report arriving as the booking finished could
deadlock and Postgres could abort the booking's save. The real-Postgres scenario is in
test_slot_booking_integration.py.
"""

from contextlib import contextmanager

from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import delivery, lead_status


class _Conn:
    def __init__(self, log, rows):
        self.log, self.rows, self.last = log, rows, ""

    def execute(self, sql, params=None):
        self.log.append(sql)
        self.last = sql
        return self

    def fetchone(self):
        for marker, row in self.rows.items():
            if marker in self.last:
                return row
        return None


def _locks(log):
    order = []
    for sql in log:
        text = sql.lower()
        if "from leads" in text and "for update" in text or text.startswith("update leads"):
            order.append("lead")
        elif ("from outreach_events" in text and "for update" in text) or text.startswith(
                "update outreach_events"):
            order.append("event")
    return order


def test_apply_call_outcome_locks_the_lead_before_the_event(monkeypatch):
    log = []
    rows = {
        "from leads": {"id": "lead-1", "status": "in_progress", "phone_e164": "+15550000000"},
        "from outreach_events": {"id": 9, "lead_id": "lead-1", "channel": "call",
                                 "status": "delivered", "outcome": "no_answer",
                                 "vapi_call_id": None},
    }

    @contextmanager
    def fake_transaction():
        yield _Conn(log, rows)

    monkeypatch.setattr(lead_status, "transaction", fake_transaction)
    reply = lead_status.apply_call_outcome(WorkflowTrace("t", "test"), lead_id="lead-1",
                                           event_id=9, outcome="no_answer", source="webhook")
    assert reply == "already recorded"
    assert _locks(log)[:2] == ["lead", "event"]


def test_no_outcome_fallback_locks_the_lead_before_the_event(monkeypatch):
    log = []

    @contextmanager
    def fake_transaction():
        yield _Conn(log, {"returning id": {"id": "lead-1"}})

    monkeypatch.setattr(delivery, "transaction", fake_transaction)
    monkeypatch.setattr(delivery, "flag_lead_for_review", lambda conn, *a, **k: conn.execute(
        "update leads set needs_review=true where id=%s"))
    delivery._settle_from_structured_output(WorkflowTrace("t", "test"), {"artifact": {}},
                                            lead_id="lead-1", event_id=9, call_id="c")
    assert _locks(log)[:2] == ["lead", "event"]
