"""Direct Google Sheet sync: the n8n rules it reimplements (no database, no network)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fake_sheet import FakeSheet

from rpt_agent.observability import WorkflowTrace
from rpt_agent.services import sheet_direct
from rpt_agent.services.google_sheet import parse_grid
from rpt_agent.services.lead_actions import ActionExecution
from rpt_agent.services.sheet_direct import (
    REVIEW_FIRST,
    apply_snapshot,
    build_action,
    command_of,
    label_result,
    needs_processing,
    normalize_dob,
    normalize_phone,
    parse_started_at,
    profile_hash,
)

OLD = sheet_direct.OLD_COMMAND.replace("{label}", "{}")


class _Stored(dict):
    reviews: dict[str, str]


@pytest.fixture
def old(monkeypatch):
    """Stored requests by id -> action, in place of lead_action_requests (no database here)."""
    stored = _Stored()
    handled: set[str] = set()  # recorded without running (refused / dropped)

    def record(lead_id, action, code):
        request_id = str(uuid4())
        stored[request_id] = action
        handled.add(request_id)
        return request_id

    monkeypatch.setattr(sheet_direct, "_record_handled", record)
    monkeypatch.setattr(sheet_direct, "_ran_actions", lambda rows: {
        r.number: (stored[r.get("action_request_id")], r.get("action_request_id") not in handled)
        for r in rows if r.get("action_request_id") in stored})
    # No open review in the database unless a test says so (`reviews[lead id] = text`).
    stored_reviews: dict[str, str] = {}
    monkeypatch.setattr(sheet_direct, "_review_waiting", lambda lead_id: stored_reviews.get(lead_id))
    stored.reviews = stored_reviews
    return stored


def _row(**cells):
    sheet = FakeSheet()
    sheet.add(**cells)
    return parse_grid(sheet.read_values(), 2).rows[0]


@pytest.mark.parametrize(("raw", "expected"), [
    ("(555) 555-0101", "+15555550101"),
    ("+1 555 555 0101", "+15555550101"),
    ("15555550101", "+15555550101"),
    ("0015555550101", "+15555550101"),
    ("+91 98765 43210", "+919876543210"),
    ("00919876543210", "+919876543210"),
    ("9876543210", "+19876543210"),  # n8n rule: every bare 10-digit number is +1
    ("555-0101", None),
    ("call me", None),
    ("", None),
])
def test_phone_rules_match_the_n8n_intake(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("16/01/2007", "16/01/2007"),    # first part > 12: day first
    ("1/16/2007", "16/01/2007"),     # month first
    ("03/04/1980", "04/03/1980"),    # ambiguous: month first, US (clinic, 2026-10-21): March 4
    ("05/09/1990", "09/05/1990"),    # Emma's example: May 9
    ("1990-12-31", "31/12/1990"),
    ("1990-12-31T00:00:00", "31/12/1990"),
    ("32874", "01/01/1990"),         # a date cell read unformatted (serial number)
    ("Feb 17, 1974", "17/02/1974"),
    ("31-02", None),
    ("", None),
])
def test_date_of_birth_rules_match_the_n8n_intake(raw, expected):
    assert normalize_dob(raw) == expected


def test_started_at_reads_our_iso_text_and_n8n_pacific_date_serials():
    assert parse_started_at("2026-10-14T17:00:00Z") == datetime(2026, 10, 14, 17, tzinfo=UTC)
    # 46309.5 = 2026-10-14 12:00 wall clock in Los Angeles (PDT, UTC-7)
    assert parse_started_at("46309.5") == datetime(2026, 10, 14, 19, tzinfo=UTC)
    assert parse_started_at("") is None and parse_started_at("soon") is None


@pytest.mark.parametrize(("action", "status", "extra", "expected"), [
    ("Start cadence", "", {}, True),
    ("Start  cadence ", "", {}, True),                      # extra spaces forgiven
    ("booked", "", {"lead_id": "x"}, None),                  # exact text, as n8n: not a command
    ("Start cadence", "", {"lead_id": "x"}, False),          # already linked
    ("Start cadence", "Error: x", {"action_request_id": "r"}, False),
    ("Start cadence", "", {"action_request_id": "r"}, True),  # staff cleared status to re-run
    ("Start cadence", "Cadence started", {}, False),
    ("Restart cadence", "", {"lead_id": "x"}, True),
    ("Restart cadence", "Cadence restarted", {"lead_id": "x"}, False),
    # Not this row's last command (spent_rows): a Restart on a finished cadence runs.
    ("Restart cadence", "Cadence completed", {"lead_id": "x"}, True),
    ("Restart cadence", "Cadence started", {"lead_id": "x"}, True),
    ("Restart cadence", "Processing: Restart cadence", {"lead_id": "x"}, False),
    ("Restart cadence", "Retrying 1/3: Restart cadence", {"lead_id": "x"}, False),
    ("Restart cadence", "Phone number changed; needs review", {"lead_id": "x"}, False),
    ("Booked", "Booked", {"lead_id": "x"}, False),
    ("Booked", "Cadence started", {"lead_id": "x"}, True),
    ("Do Not Contact", "Do not contact applied", {"lead_id": "x"}, False),
    ("Do Not Contact", "Unblocked in dashboard", {"lead_id": "x"}, False),
    ("Restart cadence", "Unblocked in dashboard", {"lead_id": "x"}, True),
    # Review texts (2026-10-22): an open review holds Start / Restart back, but Booked and Do Not
    # Contact still run (QA: a Booked typed on a flagged row was ignored). After a resolution a
    # command staff pick runs.
    ("Restart cadence", "Needs review: name changed", {"lead_id": "x"}, False),
    ("Booked", "Needs review: text failed (landline)", {"lead_id": "x"}, True),
    ("Do Not Contact", "Needs review: wrong number", {"lead_id": "x"}, True),
    ("Booked", "Number blocked; needs review", {"lead_id": "x"}, True),
    ("Booked", "Needs review: wrong number" + REVIEW_FIRST, {"lead_id": "x"}, True),
    ("Restart cadence", "Needs review: wrong number" + REVIEW_FIRST, {"lead_id": "x"}, False),
    # The old n8n review texts may still sit next to the stale Booked n8n re-sent: still blocked.
    ("Booked", "Phone number changed; needs review", {"lead_id": "x"}, False),
    ("Booked", "Error: Lead ID is required", {"lead_id": "x"}, False),
    ("Start cadence", "Resolved: calls only", {"lead_id": "x"}, False),
    ("Start cadence", "Phone changed: add a new row", {"lead_id": "x"}, False),
    ("Booked", "Resolved: calls only", {"lead_id": "x"}, True),
    ("Call later", "", {}, None),
])
def test_which_rows_need_processing(action, status, extra, expected):
    row = _row(name="Asha Rao", action=action, outcome_status=status, **extra)
    command = command_of(row)
    if expected is None:
        assert command is None
    else:
        assert needs_processing(row, command) is expected


def test_a_start_row_is_validated_like_the_n8n_intake():
    row = _row(name="Asha Rao", phone="555", action="Start cadence", location="Dana Point")
    request, error = build_action(row, "start_cadence")
    assert request is None
    assert error == "invalid phone number; Date Of Birth is required; Case is required"


def test_a_valid_start_row_builds_the_same_request_as_the_api():
    row = _row(name=" Asha Rao ", phone="(555) 555-0101", email="ASHA@Example.Test",
               date_of_birth="16/01/2007", location="Dana Point", case="Ortho",
               action="Start cadence")
    request, error = build_action(row, "start_cadence")
    assert error is None
    lead = request.lead.model_dump(mode="python")
    assert lead["phone"] == "+15555550101" and lead["email"] == "asha@example.test"
    assert lead["date_of_birth"].isoformat() == "2007-01-16" and lead["title"] == "Ortho"


def test_an_impossible_date_gets_a_readable_error_not_request_rejected():
    row = _row(name="Asha Rao", phone="5555550101", date_of_birth="31/02/1990",
               location="Dana Point", case="Ortho", action="Start cadence")
    _, error = build_action(row, "start_cadence")
    assert error.startswith("Date Of Birth") and "calendar day" in error


@pytest.mark.parametrize(("status", "body", "expected"), [
    (201, {"result": "cadence_started", "lead_id": "L"}, ("Cadence started", "In cadence", False)),
    (200, {"result": "already_booked", "lead_id": None}, ("Already booked", "Booked", False)),
    (200, {"result": "do_not_contact_applied"}, ("Do Not Contact applied", "Do not outreach", False)),
    (200, {"result": "something_new"}, ("Completed", None, False)),
    (503, {"detail": "down"}, ("", None, True)),
    (409, {"detail": "Request is still processing"}, ("", None, True)),
    (409, {"code": "restart_too_soon", "detail": "Wait 10 minutes"}, ("Error: Wait 10 minutes", None, False)),
    (422, {"detail": [{"msg": "x"}]}, ("Error: request rejected", None, False)),
])
def test_backend_results_become_the_n8n_labels(status, body, expected):
    outcome = label_result(ActionExecution(status, body))
    assert (outcome.status, outcome.lead_status, outcome.temporary) == expected


def test_profile_fingerprint_ignores_the_columns_we_write():
    row = _row(name="Asha Rao", phone="5555550101", lead_id="L")
    before = profile_hash(row)
    row.cells.update(outcome_status="Cadence started", call_outcome="No answer", lead_status="x")
    assert profile_hash(row) == before
    row.cells["case"] = "Pelvic"
    assert profile_hash(row) != before


def _grid_with(**cells):
    sheet = FakeSheet()
    number = sheet.add(**cells)
    return sheet, number, parse_grid(sheet.read_values(), 2)


def test_a_snapshot_lands_on_the_row_with_its_lead_id_and_never_writes_the_action_cell():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="Restart cadence",
                                     outcome_status="Cadence restarted", lead_status="In cadence")
    snapshot = {"lead_id": "L1", "sheet": {
        "cadence_day": "Day 0", "call_outcome": "Booked", "message_outcome": None,
        "email_outcome": None, "transcript_link": "https://dash/leads/L1/conversations/calls",
        "action_status": "Booked", "action": "Booked", "needs_review": ""}}
    assert apply_snapshot(grid, snapshot)
    row = grid.rows[0]
    assert row.get("outcome_status") == "Booked" and row.get("lead_status") == "Booked"
    # Owner 2026-10-23: only staff write Action (case 1: the lead booked, any way).
    assert row.get("action") == "Restart cadence" and row.get("cadence_day") == "Day 0"


def test_a_snapshot_for_a_lead_not_in_the_sheet_is_reported():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1")
    assert not apply_snapshot(grid, {"lead_id": "L2", "sheet": {}})


def test_a_snapshot_without_action_status_keeps_the_outcome_cell():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", outcome_status="Cadence started")
    apply_snapshot(grid, {"lead_id": "L1", "sheet": {"call_outcome": "No answer"}})
    assert grid.rows[0].get("outcome_status") == "Cadence started"
    assert grid.rows[0].get("call_outcome") == "No answer"


def test_sheet_archive_days_zero_is_not_the_default(monkeypatch):
    from types import SimpleNamespace

    from rpt_agent import cli

    seen = {}

    class Backend:
        def __init__(self, settings):
            pass

        def read_values(self):
            return FakeSheet().read_values()

    monkeypatch.setattr("rpt_agent.services.google_sheet.GoogleSheetBackend", Backend)
    monkeypatch.setattr("rpt_agent.services.sheet_direct.archive_candidates",
                        lambda grid, days: seen.update(days=days) or [])
    cli.sheet_archive(SimpleNamespace(days=0, apply=False))
    assert seen["days"] == 0


def test_a_sheet_update_is_tried_three_times_then_given_up(monkeypatch):
    from contextlib import contextmanager

    from rpt_agent import sheet_sync_worker
    from rpt_agent.config import Settings

    statuses = []

    class Conn:
        def execute(self, sql, params=None):
            statuses.append("dead" if "status='dead'" in sql else "retry")

    @contextmanager
    def fake_transaction():
        yield Conn()

    monkeypatch.setattr(sheet_sync_worker, "transaction", fake_transaction)
    error = sheet_sync_worker.SheetDeliveryError("Lead ID not found in the Sheet", retryable=True)
    settings = Settings(_env_file=None)
    assert settings.sheet_max_attempts == 3
    # `attempts` is the count before this try: tries 1 and 2 retry, try 3 gives up.
    for before in (0, 1, 2):
        sheet_sync_worker._mark_failed([{"id": 1, "attempts": before}], error, settings)
    assert statuses == ["retry", "retry", "dead"]


def test_an_empty_gender_keeps_the_old_fingerprint_and_a_filled_one_changes_it():
    # Adding the Gender column (2026-10-20) must not make every existing row look edited.
    import hashlib

    row = _row(name="Asha Rao", phone="5555550101", lead_id="L")
    old_formula = hashlib.sha256("\x1f".join(
        " ".join(row.get(k).split()) for k in ("name", "phone", "email", "date_of_birth", "location", "case")
    ).encode()).hexdigest()
    assert profile_hash(row) == old_formula
    row.cells["gender"] = "F"
    assert profile_hash(row) != old_formula


@pytest.mark.parametrize(("given", "stored"), [
    ("M", "M"), ("male", "M"), (" F ", "F"), ("Female", "F"), ("Other", "O"), ("o", "O"), ("", None), (None, None),
])
def test_sheet_gender_is_stored_as_m_f_or_o(given, stored):
    from rpt_agent.routes.n8n import SheetLeadInput

    assert SheetLeadInput(phone="5555550101", gender=given).gender == stored


def test_an_unknown_sheet_gender_is_an_error_for_the_row():
    from pydantic import ValidationError

    from rpt_agent.routes.n8n import SheetLeadInput

    with pytest.raises(ValidationError, match="Gender must be M, F or Other"):
        SheetLeadInput(phone="5555550101", gender="X")


def test_a_restart_on_a_finished_cadence_runs_only_when_it_is_not_the_rows_last_command():
    row = _row(name="Asha Rao", action="Restart cadence", outcome_status="Cadence completed", lead_id="x")
    command = command_of(row)
    assert needs_processing(row, command) is True
    assert needs_processing(row, command, {row.number}) is False  # this Restart started that cadence


# --- a command typed on a row in review (QA 2026-10-22, row "QA Actionkeep") ------------------

RESOLVED = {"review_status": "Resolved: number confirmed", "review_action": "Start cadence",
            "lead_status": "In cadence"}


def test_a_booked_typed_on_a_flagged_row_is_not_overwritten_by_the_resolution():
    # Staff typed Booked on "Needs review: wrong number", then resolved "Number is right" before the
    # Sheet worker ran it: the resolution put "Start cadence" back and the lead was never booked.
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="Booked",
                            outcome_status="Needs review: wrong number")
    apply_snapshot(grid, {"lead_id": "L1", "sheet": RESOLVED})
    row = grid.rows[0]
    assert row.get("action") == "Booked"
    assert row.get("outcome_status") == "Needs review: wrong number"  # the Booked runs next
    assert needs_processing(row, command_of(row)) is True


def test_a_stride_cancel_snapshot_does_not_clear_a_booked_staff_typed():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="Booked",
                            outcome_status="Needs review: appointment cancelled in Stride")
    apply_snapshot(grid, {"lead_id": "L1", "sheet": {
        "action_status": "Needs review: appointment cancelled in Stride", "action": ""}})
    assert grid.rows[0].get("action") == "Booked"


def test_a_stride_cancel_keeps_an_old_booked_in_action_and_it_does_not_run_again(old):
    # Case 4: the Booked that ran stays in Action; the row says it is old (it used to be cleared).
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Booked", outcome_status="Booked",
                            action_request_id=str(uuid4()))
    row = grid.rows[0]
    old[row.get("action_request_id")] = "booked"
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {
        "action_status": "Needs review: appointment cancelled in Stride", "action": ""}})
    assert row.get("action") == "Booked"
    assert row.get("outcome_status") == ("Needs review: appointment cancelled in Stride"
                                         "; Booked in Action already ran (clear this cell to run it again)")
    assert needs_processing(row, command_of(row), sheet_direct.spent_rows([row])) is False


def test_a_restart_typed_on_a_flagged_row_waits_and_the_row_says_so():
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id="L1",
                            action="Restart cadence", outcome_status="Needs review: wrong number")
    claims = sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert claims == []
    row = grid.rows[0]
    assert row.get("outcome_status") == (
        "Needs review: wrong number; resolve it on the dashboard first, then the restart runs")
    assert row.get("action") == "Restart cadence"
    # Written once: the next poll finds nothing to do.
    assert not sheet_direct.needs_attention(row, sheet_direct.spent_rows([row]))
    # Resolved on the dashboard: the Restart stays and now runs.
    apply_snapshot(grid, {"lead_id": "L1", "sheet": RESOLVED})
    assert row.get("outcome_status") == "Resolved: number confirmed"
    assert row.get("action") == "Restart cadence"
    assert needs_processing(row, command_of(row)) is True


def test_a_restart_that_already_ran_does_not_wait_behind_a_new_review(old):
    # Case 7: a review flagged next to the Restart that ran. Action is not touched any more (it was
    # turned into "Start cadence"), and the old Restart neither waits nor runs after the resolution.
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD,
                            action="Restart cadence", outcome_status="Cadence restarted",
                            action_request_id=str(uuid4()))
    row = grid.rows[0]
    old[row.get("action_request_id")] = "restart_cadence"
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {"review_status": "Needs review: wrong number"}})
    assert row.get("action") == "Restart cadence"
    # No "clear this cell" note on a review text: a Restart there would only wait (tester 2026-10-23).
    assert row.get("outcome_status") == "Needs review: wrong number"
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    assert not row.get("outcome_status").endswith(REVIEW_FIRST)
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": RESOLVED})  # case 5: no "Start cadence"
    assert row.get("action") == "Restart cadence"
    assert row.get("outcome_status") == "Resolved: number confirmed" + OLD.format("Restart cadence")
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    # Staff run it again: clear Outcome Status.
    row.cells["outcome_status"] = ""
    [(claimed, _, _)] = sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert claimed is row and row.get("outcome_status") == "Processing: Restart cadence"


def test_a_restart_that_ended_in_a_review_keeps_its_action_and_is_not_parked(old):
    # "Number blocked; needs review" from the Restart itself: it ran (its request id is the row's).
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Restart cadence",
                            outcome_status="Processing: Restart cadence",
                            action_request_id=str(uuid4()))
    old[grid.rows[0].get("action_request_id")] = "restart_cadence"
    sheet_direct._write_outcome(grid, grid.rows[0], sheet_direct.Outcome(
        "Number blocked; needs review", "Needs Review", LEAD))
    assert grid.rows[0].get("action") == "Restart cadence"
    assert not sheet_direct.parked(grid.rows[0], sheet_direct.spent_rows(grid.rows))


HINTED = "Needs review: wrong number" + REVIEW_FIRST


@pytest.mark.parametrize(("sheet", "status"), [
    # A close on the dashboard is the newer decision: the waiting Restart is dropped, and said so.
    ({"review_status": "Closed: handled by front desk", "review_action": "Start cadence",
      "lead_status": "Closed"}, "Closed: handled by front desk; the Restart in Action was not run"),
    ({"review_status": "Closed: wrong number", "review_action": "Start cadence",
      "lead_status": "Closed"}, "Closed: wrong number; the Restart in Action was not run"),
    # Booked on the board or by Stride: the row reads Booked.
    ({"action_status": "Booked", "action": "Booked", "lead_status": "Booked"}, "Booked"),
    ({"action_status": "Do Not Contact applied", "lead_status": "Do not outreach"},
     "Do Not Contact applied"),
])
def test_a_waiting_restart_is_dropped_when_the_lead_ends(old, sheet, status):
    # Case 8: the Restart stays in Action (it was replaced), recorded as handled under a new
    # Action Request ID, so a later text ("Closed: a newer row ...") cannot set it off.
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Restart cadence",
                            outcome_status=HINTED, action_request_id=str(uuid4()))
    row = grid.rows[0]
    before = row.get("action_request_id")
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": sheet})
    assert (row.get("outcome_status"), row.get("action")) == (status, "Restart cadence")
    assert row.get("action_request_id") != before
    assert old[row.get("action_request_id")] == "restart_cadence"
    row.cells["outcome_status"] = "Closed: a newer row has this phone number"
    assert needs_processing(row, command_of(row), sheet_direct.spent_rows([row])) is False


def test_the_waiting_text_goes_when_staff_clear_the_restart():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="", outcome_status=HINTED)
    row = grid.rows[0]
    assert sheet_direct.needs_attention(row, set())  # a narrow poll loads the row
    sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert row.get("outcome_status") == "Needs review: wrong number"
    assert not sheet_direct.needs_attention(row, set())


def test_a_restart_the_backend_wrote_before_2026_10_23_is_recorded_not_parked(old):
    # The dashboard's restart used to write "Restart cadence" + "Cadence restarted" without a Sheet
    # request. "Cadence restarted" turns that Restart down; it is recorded on the first poll, so the
    # next review text neither parks it nor lets it run after the resolution.
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD,
                            action="Restart cadence", outcome_status="Cadence restarted")
    row = grid.rows[0]
    assert sheet_direct.needs_attention(row, sheet_direct.spent_rows([row]))  # a narrow poll loads it
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    assert old[row.get("action_request_id")] == "restart_cadence"
    assert row.get("outcome_status") == (
        "Cadence restarted; Restart cadence in Action not run (clear this cell to run it)")
    assert not sheet_direct.needs_attention(row, sheet_direct.spent_rows([row]))
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {"review_status": "Needs review: text failed"}})
    # It never ran (only recorded as turned down): no "already ran" words.
    assert row.get("outcome_status") == "Needs review: text failed"
    assert row.get("action") == "Restart cadence"
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": RESOLVED})
    assert needs_processing(row, command_of(row), sheet_direct.spent_rows([row])) is False


@pytest.mark.parametrize(("action", "status", "shown"), [
    ("Restart cadence", "Booked",                 # final
     "Booked; Restart cadence in Action not run (clear this cell to run it)"),
    ("Do Not Contact", "Unblocked in dashboard",
     "Unblocked in dashboard; Do Not Contact in Action not run (clear this cell to run it)"),
    ("Booked", "Phone number changed; needs review",  # an old n8n review text
     "Phone number changed; needs review; Booked in Action not run (clear this cell to run it)"),
])
def test_a_command_the_outcome_status_turns_down_is_recorded_once_and_said(old, action, status, shown):
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD, action=action,
                            outcome_status=status)
    row = grid.rows[0]
    sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    first = row.get("action_request_id")
    assert old[first] == command_of(row)[1] and row.get("outcome_status") == shown
    # The added words change nothing in the rules: still turned down, Lead Status unchanged.
    assert needs_processing(row, command_of(row)) is False
    sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert row.get("action_request_id") == first and row.get("outcome_status") == shown  # not again
    row.cells["outcome_status"] = "Cadence completed"  # a later system text does not set it off
    assert needs_processing(row, command_of(row), sheet_direct.spent_rows([row])) is False


def test_a_start_cadence_or_a_row_without_lead_id_is_never_recorded(old):
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Start cadence",
                            outcome_status="Booked")
    sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    _, _, other = _grid_with(name="Asha Rao", action="Booked", outcome_status="Booked")
    sheet_direct.claim_commands(other, WorkflowTrace("t", "test"))
    assert old == {}


@pytest.mark.parametrize("reason", ["cadence_restarted_after_unblock", "cadence_resumed_after_unblock",
                                    "number_unblocked"])
def test_an_unblock_next_to_the_old_do_not_contact_never_blocks_again(old, reason):
    # Cases 2 and 3: Action keeps the Do Not Contact staff typed; it is the row's last command.
    from rpt_agent.services.sheet_sync import DASHBOARD_OUTCOMES

    text = DASHBOARD_OUTCOMES[reason][0]
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD,
                            action="Do Not Contact", outcome_status="Do Not Contact applied",
                            action_request_id=str(uuid4()))
    row = grid.rows[0]
    old[row.get("action_request_id")] = "do_not_contact"
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {
        "action_status": text, "action": DASHBOARD_OUTCOMES[reason][2], "lead_status": "In cadence"}})
    assert row.get("action") == "Do Not Contact"
    assert row.get("outcome_status") == text + OLD.format("Do Not Contact")
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    # Staff block it again: clear Outcome Status.
    row.cells["outcome_status"] = ""
    [(claimed, _, _)] = sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert claimed is row


def test_a_review_resolved_with_restart_does_not_write_restart_into_action(old):
    # Case 6: "Cadence restarted" from the dashboard used to put "Restart cadence" in Action.
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD, action="Booked",
                            outcome_status="Booked", action_request_id=str(uuid4()))
    row = grid.rows[0]
    old[row.get("action_request_id")] = "booked"
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {
        "review_status": "Cadence restarted", "review_action": "Restart cadence",
        "lead_status": "In cadence"}})
    assert row.get("action") == "Booked"
    assert row.get("outcome_status") == "Cadence restarted" + OLD.format("Booked")
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []


def test_a_waiting_restart_that_the_resolution_restarted_is_done(old):
    # The review was resolved with "Restart cadence" while a typed Restart waited: that was the
    # restart. Recorded, so it does not run on top once the cadence completes.
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD,
                            action="Restart cadence", outcome_status=HINTED)
    row = grid.rows[0]
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {
        "review_status": "Cadence restarted", "lead_status": "In cadence"}})
    assert row.get("outcome_status") == "Cadence restarted"
    assert old[row.get("action_request_id")] == "restart_cadence"
    row.cells["outcome_status"] = "Cadence completed"
    assert needs_processing(row, command_of(row), sheet_direct.spent_rows([row])) is False


def test_clearing_outcome_status_on_a_row_in_review_does_not_start_the_restart(old):
    # Tester 2026-10-23: a cleared Outcome Status ran the Restart at once and closed the review
    # ("restarted" by Google Sheet; a name review's new name accepted with nobody choosing).
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD,
                            action="Restart cadence", outcome_status="")
    old.reviews[LEAD] = "Needs review: name changed"
    row = grid.rows[0]
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    assert row.get("outcome_status") == "Needs review: name changed" + REVIEW_FIRST
    assert sheet_direct.parked(row, sheet_direct.spent_rows([row]))
    # Resolved: it runs.
    del old.reviews[LEAD]
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": RESOLVED})
    [(claimed, _, _)] = sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert claimed is row


def test_an_old_restart_cleared_on_a_row_in_review_waits_for_the_review(old):
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD,
                            action="Restart cadence", outcome_status="",
                            action_request_id=str(uuid4()))
    row = grid.rows[0]
    old[row.get("action_request_id")] = "restart_cadence"  # it ran before the review
    old.reviews[LEAD] = "Needs review: name changed"
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    assert row.get("outcome_status") == "Needs review: name changed" + REVIEW_FIRST
    assert row.get("action_request_id") == ""  # no longer the old Restart: it now waits
    assert sheet_direct.parked(row, sheet_direct.spent_rows([row]))


@pytest.mark.parametrize("action", ["Booked", "Do Not Contact"])
def test_booked_or_do_not_contact_cleared_on_a_row_in_review_still_run(old, action):
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD, action=action,
                            outcome_status="")
    old.reviews[LEAD] = "Needs review: name changed"
    [(claimed, _, _)] = sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert claimed.get("outcome_status") == f"Processing: {action}"


@pytest.mark.parametrize("text", ["Closed: a newer row has this phone number",
                                  "Closed: handled by front desk", "Phone changed: add a new row"])
def test_no_run_it_again_note_on_a_closed_lead(old, text):
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Restart cadence",
                            outcome_status="Cadence restarted", action_request_id=str(uuid4()))
    row = grid.rows[0]
    old[row.get("action_request_id")] = "restart_cadence"
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {"action_status": text}})
    assert row.get("outcome_status") == text


# --- "Same person, keep old name": the Name cell goes back (owner, demo test 2026-10-23) ---------

KEPT = {"review_status": "Resolved: old name kept", "review_action": "Start cadence",
        "name": "QA Nameone", "name_replaces": "QA Namekeep"}
LEAD = "00000000-0000-4000-8000-000000000001"


def _kept_row(**cells):
    sheet, number, grid = _grid_with(**{"name": "QA Namekeep", "phone": "5555550101",
                                        "lead_id": LEAD, "action": "Start cadence",
                                        "outcome_status": "Needs review: name changed", **cells})
    return sheet, number, grid


def test_keep_old_name_writes_the_old_name_back_and_stores_its_fingerprint():
    from rpt_agent.services.google_sheet import flush

    sheet, number, grid = _kept_row()
    poll = sheet_direct.PollResult()
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": KEPT}, poll)
    result = flush(sheet, grid)
    assert result.deferred_rows == set() and result.skipped_rows == set()
    assert sheet.cell(number, "name") == "QA Nameone"
    assert sheet.cell(number, "outcome_status") == "Resolved: old name kept"
    # The fingerprint saved with the write is the row as it now reads: the profile sync sees it in sync.
    fresh = parse_grid(sheet.read_values(), 2).rows[0]
    before = parse_grid(_kept_row()[0].read_values(), 2).rows[0]
    assert poll.renamed[LEAD] == (profile_hash(before), profile_hash(fresh), number, "QA Nameone")


def test_keep_old_name_never_overwrites_a_third_name_already_in_the_row():
    _, _, grid = _kept_row(name="QA Namethree")
    poll = sheet_direct.PollResult()
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": KEPT}, poll)
    assert grid.rows[0].get("name") == "QA Namethree" and LEAD not in poll.renamed


def test_keep_old_name_skips_the_cell_when_staff_type_a_third_name_before_the_write():
    from rpt_agent.services.google_sheet import flush

    sheet, number, grid = _kept_row()
    poll = sheet_direct.PollResult()
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": KEPT}, poll)
    sheet.before_write = lambda: sheet.set(number, "name", "QA Namethree")
    result = flush(sheet, grid)
    # Their text stays; the rest of the row is written; no fingerprint for the skipped Name.
    assert sheet.cell(number, "name") == "QA Namethree"
    assert sheet.cell(number, "outcome_status") == "Resolved: old name kept"
    assert result.deferred_rows == set() and result.skipped_rows == {number}
    assert grid.rows[0].get("name") == "QA Namethree"


def test_use_new_name_leaves_the_name_cell_alone():
    _, _, grid = _kept_row()
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {"review_status": "Resolved: new name used"}})
    assert grid.rows[0].get("name") == "QA Namekeep" and not grid.expect


def test_a_landed_name_write_back_clears_the_kept_sheet_name(monkeypatch):
    # Tester 2026-10-23: the kept name stayed in leads.sheet_name_accepted, so the same new name
    # retyped later never opened a review.
    from contextlib import contextmanager

    calls = []

    class Cursor:
        def executemany(self, sql, params):
            calls.append((sql, list(params)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_transaction():
        yield Conn()

    monkeypatch.setattr(sheet_direct, "transaction", fake_transaction)
    poll = sheet_direct.PollResult()
    poll.renamed = {LEAD: ("before", "after", 3, "QA Nameone"),
                    "00000000-0000-4000-8000-000000000002": ("b", "a", 4, "Other")}
    poll.save_baselines({4})  # row 4 was not written
    cleared = [params for sql, params in calls if "sheet_name_accepted=null" in sql]
    assert cleared == [[(LEAD,)]]
