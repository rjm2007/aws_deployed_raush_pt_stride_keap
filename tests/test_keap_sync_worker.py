"""Booking vs keap-worker lock (demo incident 2026-10-26), without a database.

New keap_sync rows must be committed in their own short transaction before the planning
transaction: an uncommitted insert that points at a lead holds a key-share lock on that lead, and
the hourly full plan held it for minutes, so a voice booking (mark_booked, FOR UPDATE) waited 58 s
on the lead. mark_booked now locks FOR NO KEY UPDATE, which such inserts do not block.
The same scenario on a real Postgres is in test_keap_sync_integration.py.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import rpt_agent.keap_sync_worker as worker
from rpt_agent.services import keap_desired
from rpt_agent.services.lead_status import mark_booked


class _Rows:
    def fetchone(self):
        return {"id": 7}

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, log, number):
        self.log, self.number = log, number

    def execute(self, sql, params=None):
        return _Rows()


def _fake_world(monkeypatch):
    log: list[tuple] = []
    opened = [0]

    @contextmanager
    def fake_transaction():
        opened[0] += 1
        number = opened[0]
        log.append(("begin", number))
        yield _Conn(log, number)
        log.append(("commit", number))

    def attach_people(conn, practice_id):
        log.append(("attach", conn.number))
        return {"leads_added": 2, "patients_added": 1, "merged": 1, "duplicates": 0}

    def plan_changes(conn, practice_id, *, full=False, limit=None, attach=True):
        log.append(("plan", conn.number, attach))
        return {"recomputed": 3, "queued": 1, "skipped": 0, "failed": 0, "frozen": 0}

    settings = SimpleNamespace(
        keap_sync_enabled=True, stride_practice_slug="rausch", keap_sync_batch_size=5,
        keap_max_requests_per_day=100,
    )
    monkeypatch.setattr(worker, "transaction", fake_transaction)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker.keap_oauth, "connection_status", lambda _p: {"status": "active"})
    monkeypatch.setattr(keap_desired, "attach_people", attach_people)
    monkeypatch.setattr(keap_desired, "plan_changes", plan_changes)
    worker._state.update(last_full=None, client=None, stats_day=None)
    client = SimpleNamespace(limiter=SimpleNamespace(used_today=0), stats={})
    return log, client


def test_new_keap_rows_are_committed_before_the_planning_transaction(monkeypatch):
    log, client = _fake_world(monkeypatch)
    worker.run_keap_sync_tick(client)
    attach = next(e for e in log if e[0] == "attach")
    plan = next(e for e in log if e[0] == "plan")
    assert attach[1] != plan[1]
    # attach's transaction commits before the planning one begins, and planning does not attach again
    assert log.index(("commit", attach[1])) < log.index(("begin", plan[1]))
    assert plan[2] is False
    assert [e[0] for e in log].count("attach") == 1


def test_tick_still_reports_the_attach_counts_as_plan_counts(monkeypatch):
    _log, client = _fake_world(monkeypatch)
    counts = worker.run_keap_sync_tick(client)
    assert counts == {
        "plan_recomputed": 3, "plan_queued": 1, "plan_skipped": 0, "plan_failed": 0,
        "plan_frozen": 0, "plan_leads_added": 2, "plan_patients_added": 1, "plan_merged": 1,
        "plan_duplicates": 0,
    }
    # same key order as when plan_changes attached itself
    assert list(counts)[:6] == ["plan_recomputed", "plan_queued", "plan_skipped", "plan_failed",
                                "plan_frozen", "plan_leads_added"]


def test_a_failed_attach_stops_the_tick_before_planning(monkeypatch):
    log, client = _fake_world(monkeypatch)

    def broken(conn, practice_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(keap_desired, "attach_people", broken)
    # The tick raises as before: main() logs it and the next tick retries.
    with pytest.raises(RuntimeError, match="db down"):
        worker.run_keap_sync_tick(client)
    assert not any(e[0] == "plan" for e in log)


def test_plan_changes_without_attach_never_attaches(monkeypatch):
    def must_not_run(conn, practice_id):
        raise AssertionError("attach_people ran")

    class NoSettings:
        def execute(self, sql, params=None):
            return SimpleNamespace(fetchone=lambda: None)

    monkeypatch.setattr(keap_desired, "attach_people", must_not_run)
    counts = keap_desired.plan_changes(NoSettings(), 1, attach=False, limit=10)
    assert "leads_added" not in counts


def test_mark_booked_locks_the_lead_for_no_key_update():
    queries = []

    class Conn:
        def execute(self, sql, params=None):
            queries.append(" ".join(sql.split()))
            if "from lead_reviews" in sql:  # no case change card open
                return SimpleNamespace(fetchone=lambda: None)
            return SimpleNamespace(fetchone=lambda: {"status": "in_progress"})

    mark_booked(Conn(), "11111111-2222-3333-4444-555555555555", "voice_booking")
    lock = queries[0]
    assert lock.startswith("select status from leads") and lock.endswith("for no key update")
    assert not any(q.endswith(" for update") for q in queries)
    # nothing in mark_booked changes a key column of leads (id, practice_id, source_system,
    # external_referral_id), so its own UPDATE never needs the stronger lock either
    for q in queries:
        if q.startswith("update leads set"):
            assignments = q.split(" set ", 1)[1].split(" where ", 1)[0]
            columns = {part.split("=", 1)[0].strip() for part in assignments.split(",")}
            assert "status" in columns
            assert not columns & {"id", "practice_id", "source_system", "external_referral_id"}
