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
    LEGACY_WAIT_HINT,
    REVIEW_REFUSED,
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


@pytest.fixture(autouse=True)
def notes(monkeypatch):
    """Refusals the Sheet worker notes in a lead's Activity (no database here): (row, label, text)."""
    noted = []
    monkeypatch.setattr(sheet_direct, "note_refusal",
                        lambda row, label, text: noted.append((row.number, label, text)))
    return noted


@pytest.fixture
def old(monkeypatch):
    """Finished requests by id -> action, in place of lead_action_requests (no database here)."""
    stored = {}
    monkeypatch.setattr(sheet_direct, "_handled_actions", lambda rows: {
        r.number: stored[r.get("action_request_id")]
        for r in rows if r.get("action_request_id") in stored})
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
    # Owner 2026-10-26: Action must be chosen from the drop-down; another case is not a command.
    (" restart  CADENCE", "", {"lead_id": "x"}, None),
    ("START CADENCE", "", {}, None),
    # Owner 2026-10-25: Action empties once a command is handled, so a command in it is new, whatever
    # Outcome Status says (the same command picked again runs again).
    ("Start cadence", "Error: x", {}, True),
    ("Start cadence", "", {"lead_id": "x"}, True),           # turned down with a reason, not ignored
    ("Restart cadence", "Cadence restarted", {"lead_id": "x"}, True),
    ("Restart cadence", "Cadence completed", {"lead_id": "x"}, True),
    ("Restart cadence", "Error: restarted less than 10 minutes ago", {"lead_id": "x"}, True),
    ("Booked", "Booked", {"lead_id": "x"}, True),
    ("Do Not Contact", "Unblocked in dashboard", {"lead_id": "x"}, True),
    ("Restart cadence", "Booked", {"lead_id": "x"}, True),
    # Running now: recovery's.
    ("Restart cadence", "Processing: Restart cadence", {"lead_id": "x"}, False),
    ("Restart cadence", "Retrying 1/3: Restart cadence", {"lead_id": "x"}, False),
    # Review texts: Booked and Do Not Contact run (2026-10-22). A Restart is handled too (owner,
    # 2026-10-25): the backend refuses it with a reason, it no longer waits in Action.
    ("Restart cadence", "Needs review: name changed", {"lead_id": "x"}, True),
    ("Restart cadence", "Needs review: wrong number" + LEGACY_WAIT_HINT, {"lead_id": "x"}, True),
    ("Booked", "Needs review: text failed (landline)", {"lead_id": "x"}, True),
    ("Do Not Contact", "Needs review: wrong number", {"lead_id": "x"}, True),
    ("Booked", "Number blocked; needs review", {"lead_id": "x"}, True),
    ("Booked", "Needs review: wrong number" + LEGACY_WAIT_HINT, {"lead_id": "x"}, True),
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


def test_a_wrong_email_is_not_an_error_and_is_not_sent():
    # Owner, 2026-10-26: only a warning; the lead gets no email (so none goes to Keap).
    row = _row(name="Asha Rao", phone="5555550101", email="maria@gmail", date_of_birth="16/01/1990",
               location="Dana Point", case="Ortho", action="Start cadence")
    request, error = build_action(row, "start_cadence")
    assert error is None and request.lead.email is None


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
    # Refusals staff fix by picking the command again later say so (owner, 2026-10-25).
    (409, {"code": "restart_too_soon", "detail": "Wait", "retry_at": "2026-10-25T22:52:30+00:00"},
     ("Error: restarted less than 10 minutes ago. Pick Restart cadence again after 3:53 PM PT.",
      None, False)),
    (409, {"code": "restart_too_soon", "detail": "Wait"},  # stored before retry_at was sent
     ("Error: restarted less than 10 minutes ago. Pick Restart cadence again in 10 minutes.",
      None, False)),
    (409, {"code": "outreach_unresolved", "detail": "Wait for the current call or SMS result"},
     ("Error: a call or text is going out right now. Pick Restart cadence again in a minute.",
      None, False)),
    (409, {"code": "lead_phone_mismatch", "detail": "Lead ID and phone number do not match"},
     ("Error: Lead ID and phone number do not match", None, False)),
    # Restart guardrails (owner, 2026-10-25).
    (409, {"code": "restart_needs_review", "detail": "x"},
     ("Error: this lead needs review. Resolve it on the dashboard; you can restart it from there.",
      None, False)),
    (409, {"code": "restart_booked", "detail": "x"},
     ("Error: this lead is booked. To start again, cancel the visit in Stride, or add a new row.", None, False)),
    (409, {"code": "restart_closed", "detail": "x", "closed_reason": "handled by front desk"},
     ("Error: this lead is closed (handled by front desk). Use the dashboard.", None, False)),
    (409, {"code": "restart_closed", "detail": "x", "closed_reason": "a newer row has this phone number"},
     ("Error: this lead is closed (a newer row has this phone number). Use the dashboard.", None,
      False)),
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
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="",
                            outcome_status="Cadence restarted", lead_status="In cadence")
    snapshot = {"lead_id": "L1", "sheet": {
        "cadence_day": "Day 0", "call_outcome": "Booked", "message_outcome": None,
        "email_outcome": None, "transcript_link": "https://dash/leads/L1/conversations/calls",
        "action_status": "Booked", "action": "Booked", "needs_review": ""}}
    assert apply_snapshot(grid, snapshot)
    row = grid.rows[0]
    assert row.get("outcome_status") == "Booked" and row.get("lead_status") == "Booked"
    # Only staff fill Action (owner 2026-10-23/25): the lead booked, any way, writes no "Booked" there.
    assert row.get("action") == "" and row.get("cadence_day") == "Day 0"


def test_a_snapshot_for_a_lead_not_in_the_sheet_is_reported():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1")
    assert not apply_snapshot(grid, {"lead_id": "L2", "sheet": {}})


def test_a_snapshot_without_action_status_keeps_the_outcome_cell():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", outcome_status="Cadence started")
    apply_snapshot(grid, {"lead_id": "L1", "sheet": {"call_outcome": "No answer"}})
    assert grid.rows[0].get("outcome_status") == "Cadence started"
    assert grid.rows[0].get("call_outcome") == "No answer"


def test_a_declined_snapshot_replaces_cadence_started():
    """2026-10-27: a decline on the call reaches the Outcome Status cell in direct mode."""
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="",
                            outcome_status="Cadence started", lead_status="In cadence")
    apply_snapshot(grid, {"lead_id": "L1", "sheet": {
        "call_outcome": "Answered - declined", "action_status": "Declined on the call",
        "action": "", "lead_status": "Closed"}})
    row = grid.rows[0]
    assert row.get("outcome_status") == "Declined on the call"
    assert row.get("lead_status") == "Closed" and row.get("action") == ""


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


# --- Action empties once a command is handled (owner, 2026-10-25) ------------------------------


COMMANDS = {label: action for label, (_, action) in sheet_direct.COMMANDS.items()}


def _poll(sheet, monkeypatch, outcome=None, trace=None):
    """One claim + run + write, as the worker does, with the backend's answer faked as `outcome`."""
    from rpt_agent.services.google_sheet import flush

    ran = []

    def execute(request, request_id, trace):
        ran.append(request.action)
        return outcome

    monkeypatch.setattr(sheet_direct, "_execute", execute)
    trace = trace or WorkflowTrace("t", "test")
    grid = parse_grid(sheet.read_values(), 2)
    claims = sheet_direct.claim_commands(grid, trace)
    claimed = flush(sheet, grid)
    sheet_direct.run_claims(grid, claims, claimed.deferred_rows, sheet_direct.PollResult(), trace)
    flush(sheet, grid)
    return ran


LEAD_ID = "00000000-0000-4000-8000-000000000001"
ROW = {"name": "Asha Rao", "phone": "5555550101", "email": "asha@example.test",
       "date_of_birth": "16/01/1990", "location": "Dana Point", "case": "Ortho"}


@pytest.mark.parametrize(("action", "lead_id", "outcome", "status", "lead_status"), [
    ("Start cadence", "", sheet_direct.Outcome("Cadence started", "In cadence", LEAD_ID),
     "Cadence started", "In cadence"),
    ("Restart cadence", LEAD_ID, sheet_direct.Outcome("Cadence restarted", "In cadence", LEAD_ID),
     "Cadence restarted", "In cadence"),
    ("Booked", LEAD_ID, sheet_direct.Outcome("Booked", "Booked", LEAD_ID), "Booked", "Booked"),
    ("Do Not Contact", LEAD_ID, sheet_direct.Outcome("Do Not Contact applied", "Do not outreach",
                                                     LEAD_ID), "Do Not Contact applied",
     "Do not outreach"),
])
def test_action_empties_after_the_command_runs(old, monkeypatch, action, lead_id, outcome, status,
                                               lead_status):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action=action, lead_id=lead_id, outcome_status="Cadence started")
    assert _poll(sheet, monkeypatch, outcome) == [COMMANDS[action]]
    assert sheet.cell(number, "action") == ""
    assert sheet.cell(number, "action_request_id") == ""
    assert (sheet.cell(number, "outcome_status"), sheet.cell(number, "lead_status")) == (
        status, lead_status)
    assert sheet.cell(number, "lead_id") == LEAD_ID
    assert _poll(sheet, monkeypatch, outcome) == []  # nothing left to run


@pytest.mark.parametrize(("body", "shown"), [
    ({"code": "outreach_unresolved", "detail": "Wait for the current call or SMS result"},
     "Error: a call or text is going out right now. Pick Restart cadence again in a minute."),
    ({"code": "restart_too_soon", "detail": "x", "retry_at": "2026-10-25T22:52:00+00:00"},
     "Error: restarted less than 10 minutes ago. Pick Restart cadence again after 3:52 PM PT."),
    ({"code": "lead_phone_mismatch", "detail": "Lead ID and phone number do not match"},
     "Error: Lead ID and phone number do not match"),
])
def test_action_empties_after_a_refusal_and_the_row_says_what_to_do(old, monkeypatch, body, shown):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action="Restart cadence", lead_id=LEAD_ID,
                       outcome_status="Cadence restarted", lead_status="In cadence")
    refusal = label_result(ActionExecution(409, {"lead_id": LEAD_ID, **body}))
    assert _poll(sheet, monkeypatch, refusal) == ["restart_cadence"]
    assert sheet.cell(number, "outcome_status") == shown
    assert sheet.cell(number, "action") == "" and sheet.cell(number, "action_request_id") == ""
    assert sheet.cell(number, "lead_status") == "In cadence"
    # Staff pick it again: it runs again (here: accepted).
    sheet.set(number, "action", "Restart cadence")
    done = sheet_direct.Outcome("Cadence restarted", "In cadence", LEAD_ID)
    assert _poll(sheet, monkeypatch, done) == ["restart_cadence"]
    assert sheet.cell(number, "outcome_status") == "Cadence restarted"


def test_a_patient_can_be_restarted_any_number_of_times_by_picking_restart_again(old, monkeypatch):
    sheet = FakeSheet()
    number = sheet.add(**ROW, lead_id=LEAD_ID, outcome_status="Cadence completed")
    done = sheet_direct.Outcome("Cadence restarted", "In cadence", LEAD_ID)
    for _ in range(3):
        sheet.set(number, "action", "Restart cadence")
        assert _poll(sheet, monkeypatch, done) == ["restart_cadence"]
        assert sheet.cell(number, "action") == ""
        assert sheet.cell(number, "outcome_status") == "Cadence restarted"


def test_a_command_still_retrying_keeps_its_action_and_request_id(old, monkeypatch):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action="Booked", lead_id=LEAD_ID, outcome_status="Cadence started")
    _poll(sheet, monkeypatch, sheet_direct.Outcome("", temporary=True))
    assert sheet.cell(number, "outcome_status") == "Retrying 1/3: Booked"
    assert sheet.cell(number, "action") == "Booked" and sheet.cell(number, "action_request_id")


def test_an_invalid_row_empties_action_and_runs_when_picked_again(old, monkeypatch):
    sheet = FakeSheet()
    number = sheet.add(**{**ROW, "date_of_birth": ""}, action="Start cadence")
    assert _poll(sheet, monkeypatch) == []
    assert sheet.cell(number, "outcome_status") == "Error: Date Of Birth is required"
    assert sheet.cell(number, "action") == ""
    sheet.set(number, "date_of_birth", "16/01/1990")
    sheet.set(number, "action", "Start cadence")
    started = sheet_direct.Outcome("Cadence started", "In cadence", LEAD_ID)
    assert _poll(sheet, monkeypatch, started) == ["start_cadence"]


def test_start_cadence_on_a_linked_row_is_turned_down_with_a_reason(old, monkeypatch):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action="Start cadence", lead_id=LEAD_ID, outcome_status="Booked",
                       lead_status="Booked")
    assert _poll(sheet, monkeypatch) == []
    assert sheet.cell(number, "outcome_status") == (
        "Error: this row already has a lead. Pick Restart cadence to start the calls again.")
    assert sheet.cell(number, "action") == "" and sheet.cell(number, "lead_status") == "Booked"


def test_a_command_staff_pick_while_the_result_is_written_is_kept_and_runs(old, monkeypatch):
    from rpt_agent.services.google_sheet import flush

    sheet = FakeSheet()
    number = sheet.add(**ROW, action="Restart cadence", lead_id=LEAD_ID,
                       outcome_status="Cadence completed")
    monkeypatch.setattr(sheet_direct, "_execute", lambda request, request_id, trace:
                        sheet_direct.Outcome("Cadence restarted", "In cadence", LEAD_ID))
    trace = WorkflowTrace("t", "test")
    grid = parse_grid(sheet.read_values(), 2)
    claims = sheet_direct.claim_commands(grid, trace)
    claimed = flush(sheet, grid)
    sheet_direct.run_claims(grid, claims, claimed.deferred_rows, sheet_direct.PollResult(), trace)
    # Between our read and our write, staff pick Booked.
    sheet.before_write = lambda: sheet.set(number, "action", "Booked")
    result = flush(sheet, grid)
    assert sheet.cell(number, "action") == "Booked"  # theirs, not wiped
    assert sheet.cell(number, "outcome_status") == "Cadence restarted"
    assert sheet.cell(number, "action_request_id") == ""
    assert result.skipped_cells == {(number, "action")}
    # The next poll takes it as a new command, not an old one.
    booked = sheet_direct.Outcome("Booked", "Booked", LEAD_ID)
    assert _poll(sheet, monkeypatch, booked) == ["booked"]
    assert sheet.cell(number, "action") == ""


# --- rows from before 2026-10-25: a handled command still in Action is emptied, never run ---------


@pytest.mark.parametrize(("action", "status", "last", "is_old"), [
    # The backend wrote these before 2026-10-23; the row's request is the Start.
    ("Booked", "Booked", "start_cadence", True),
    ("Restart cadence", "Cadence restarted", "start_cadence", True),
    ("Start cadence", "Cadence resumed in dashboard", "do_not_contact", True),
    # Staff's own command, the row's last one (it ran, or the 2026-10-23 rules turned it down).
    ("Do Not Contact", "Unblocked in dashboard", "do_not_contact", True),
    ("Restart cadence", "Cadence completed", "restart_cadence", True),
    ("Restart cadence", "Needs review: wrong number", "restart_cadence", True),
    ("Booked", "Needs review: appointment cancelled in Stride", "booked", True),
    ("Restart cadence", "Booked; Restart cadence in Action not run (clear this cell to run it)",
     "restart_cadence", True),
    ("Booked", "Error: Lead ID is required", "start_cadence", True),
    ("Start cadence", "Cadence started", "start_cadence", True),
    # Picked under the old rules and not run yet: runs as it would have.
    ("Restart cadence", "Cadence completed", "start_cadence", False),
    ("Restart cadence", "", "restart_cadence", False),  # Outcome Status cleared to run it again
    ("Booked", "Needs review: wrong number", "start_cadence", False),
    # A Restart that waited behind a review (2026-10-22..25 rules): emptied, never run (2026-10-25).
    ("Restart cadence", "Needs review: wrong number" + LEGACY_WAIT_HINT, "start_cadence", True),
    ("Restart cadence", "Processing: Restart cadence", "restart_cadence", False),  # running
])
def test_old_commands_are_the_ones_already_handled(old, action, status, last, is_old):
    row = _row(name="Asha Rao", phone="5555550101", lead_id=LEAD_ID, action=action,
               outcome_status=status, action_request_id=str(uuid4()))
    old[row.get("action_request_id")] = last
    assert (row.number in sheet_direct.old_commands([row])) is is_old


def test_a_command_without_a_request_id_is_never_old(old):
    # Since 2026-10-25 a handled row has no Action Request ID: a command there was picked again.
    row = _row(name="Asha Rao", lead_id=LEAD_ID, action="Booked", outcome_status="Booked")
    assert sheet_direct.old_commands([row]) == set()


@pytest.mark.parametrize(("action", "status", "last", "shown"), [
    ("Booked", "Booked", "start_cadence", "Booked"),
    ("Restart cadence", "Cadence restarted", "restart_cadence", "Cadence restarted"),
    ("Do Not Contact", ("Unblocked in dashboard; Do Not Contact in Action already ran (clear this "
                        "cell to run it again)"), "do_not_contact", "Unblocked in dashboard"),
    ("Restart cadence", "Booked; Restart cadence in Action not run (clear this cell to run it)",
     "restart_cadence", "Booked"),
    ("Start cadence", "Cadence started", "start_cadence", "Cadence started"),
])
def test_on_the_first_poll_an_old_command_is_emptied_not_run(old, monkeypatch, action, status, last,
                                                             shown):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action=action, lead_id=LEAD_ID, outcome_status=status,
                       lead_status="In cadence", action_request_id=(rid := str(uuid4())))
    old[rid] = last
    scanned = parse_grid(sheet.read_values(), 2).rows
    assert sheet_direct.needs_attention(scanned[0], sheet_direct.old_commands(scanned))  # loaded
    assert _poll(sheet, monkeypatch) == []
    assert sheet.cell(number, "action") == "" and sheet.cell(number, "action_request_id") == ""
    # The result stays; the old "clear this cell" words go (they no longer apply).
    assert sheet.cell(number, "outcome_status") == shown
    assert sheet.cell(number, "lead_status") == "In cadence"
    # Picked again later: a new command, it runs.
    sheet.set(number, "action", action)
    if action != "Start cadence":
        assert _poll(sheet, monkeypatch, sheet_direct.Outcome("Done", None, LEAD_ID)) == [
            COMMANDS[action]]


def test_an_old_command_staff_replace_before_it_is_emptied_runs(old, monkeypatch):
    from rpt_agent.services.google_sheet import flush

    sheet = FakeSheet()
    number = sheet.add(**ROW, action="Booked", lead_id=LEAD_ID, outcome_status="Booked",
                       action_request_id=(rid := str(uuid4())))
    old[rid] = "booked"
    grid = parse_grid(sheet.read_values(), 2)
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    sheet.before_write = lambda: sheet.set(number, "action", "Restart cadence")
    flush(sheet, grid)
    assert sheet.cell(number, "action") == "Restart cadence"
    assert sheet.cell(number, "action_request_id") == ""
    done = sheet_direct.Outcome("Cadence restarted", "In cadence", LEAD_ID)
    assert _poll(sheet, monkeypatch, done) == ["restart_cadence"]


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


def test_a_snapshot_empties_an_old_booked_left_before_2026_10_25(old):
    # A Stride cancel next to the Booked that ran before Action emptied itself: it is not run again.
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Booked", outcome_status="Booked",
                            action_request_id=(rid := str(uuid4())))
    old[rid] = "booked"
    row = grid.rows[0]
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": {
        "action_status": "Needs review: appointment cancelled in Stride", "action": ""}})
    assert row.get("action") == "" and row.get("action_request_id") == ""
    assert row.get("outcome_status") == "Needs review: appointment cancelled in Stride"


def test_a_restart_typed_on_a_flagged_row_is_sent_and_its_refusal_empties_action(old, monkeypatch):
    # Owner 2026-10-25: no more waiting in Action. The backend refuses it (the lead is in review,
    # read from the database under the lead lock), the row says so, and Action empties.
    sheet = FakeSheet()
    number = sheet.add(**ROW, lead_id=LEAD, action="Restart cadence",
                       outcome_status="Needs review: wrong number", lead_status="Needs Review")
    refused = label_result(ActionExecution(409, {"lead_id": LEAD, "code": "restart_needs_review",
                                                 "detail": "x"}))
    assert _poll(sheet, monkeypatch, refused) == ["restart_cadence"]
    assert sheet.cell(number, "outcome_status") == REVIEW_REFUSED
    assert sheet.cell(number, "action") == "" and sheet.cell(number, "action_request_id") == ""
    assert sheet.cell(number, "lead_status") == "Needs Review"
    # The resolution's text then lands as usual; nothing runs by itself.
    grid = parse_grid(sheet.read_values(), 2)
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": RESOLVED})
    assert grid.rows[0].get("outcome_status") == "Resolved: number confirmed"
    assert grid.rows[0].get("action") == ""


def test_a_restart_that_ended_in_a_review_empties_its_action(old):
    # "Number blocked; needs review" from the Restart itself: it ran, Action empties.
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Restart cadence",
                            outcome_status="Processing: Restart cadence",
                            action_request_id=str(uuid4()))
    sheet_direct._write_outcome(grid, grid.rows[0], sheet_direct.Outcome(
        "Number blocked; needs review", "Needs Review", LEAD))
    assert grid.rows[0].get("action") == ""


HINTED = "Needs review: wrong number" + LEGACY_WAIT_HINT


@pytest.mark.parametrize("request_id", ["", "old"])
def test_a_restart_left_waiting_by_the_old_rules_is_refused_never_run(old, monkeypatch, notes,
                                                                      request_id):
    # One-time safety (owner, 2026-10-25): a Restart parked behind a review before the change is
    # emptied with the refusal text and never run, even if the review was resolved since.
    sheet = FakeSheet()
    rid = str(uuid4()) if request_id else ""
    if rid:
        old[rid] = "restart_cadence"
    number = sheet.add(**ROW, lead_id=LEAD, action="Restart cadence", outcome_status=HINTED,
                       action_request_id=rid)
    scanned = parse_grid(sheet.read_values(), 2).rows
    assert sheet_direct.needs_attention(scanned[0], sheet_direct.old_commands(scanned))
    assert _poll(sheet, monkeypatch, sheet_direct.Outcome("Cadence restarted", "In cadence",
                                                         LEAD)) == []
    assert sheet.cell(number, "outcome_status") == REVIEW_REFUSED
    assert sheet.cell(number, "action") == "" and sheet.cell(number, "action_request_id") == ""
    assert notes == [(number, "Restart cadence", REVIEW_REFUSED)]  # in the lead's Activity
    assert _poll(sheet, monkeypatch) == []


def test_the_waiting_text_goes_when_staff_clear_the_restart():
    _, _, grid = _grid_with(name="Asha Rao", lead_id="L1", action="", outcome_status=HINTED)
    row = grid.rows[0]
    assert sheet_direct.needs_attention(row, set())  # a narrow poll loads the row
    sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert row.get("outcome_status") == "Needs review: wrong number"
    assert not sheet_direct.needs_attention(row, set())


@pytest.mark.parametrize("sheet", [
    {"review_status": "Closed: handled by front desk", "lead_status": "Closed"},
    {"action_status": "Booked", "action": "Booked", "lead_status": "Booked"},
    {"review_status": "Cadence restarted", "lead_status": "In cadence"},
])
def test_a_snapshot_never_empties_or_fills_action(sheet):
    # The parked-Restart machinery is gone (2026-10-25): only a command run on the row empties it.
    _, _, grid = _grid_with(name="Asha Rao", lead_id=LEAD, action="Do Not Contact",
                            outcome_status="Processing: Do Not Contact",
                            action_request_id=str(uuid4()))
    apply_snapshot(grid, {"lead_id": LEAD, "sheet": sheet})
    assert grid.rows[0].get("action") == "Do Not Contact"


def test_a_start_on_a_linked_row_is_noted_in_the_leads_activity(old, monkeypatch, notes):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action="Start cadence", lead_id=LEAD, outcome_status="Booked")
    assert _poll(sheet, monkeypatch) == []
    assert notes == [(number, "Start cadence", sheet_direct.ALREADY_LINKED)]


@pytest.mark.parametrize("action", ["Booked", "Do Not Contact"])
def test_booked_or_do_not_contact_on_a_lead_in_review_still_run(old, action):
    _, _, grid = _grid_with(name="Asha Rao", phone="5555550101", lead_id=LEAD, action=action,
                            outcome_status="Needs review: name changed")
    [(claimed, _, _)] = sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"))
    assert claimed.get("outcome_status") == f"Processing: {action}"


# --- "Same person, keep old name": the Name cell goes back (owner, demo test 2026-10-23) ---------

KEPT = {"review_status": "Resolved: old name kept", "review_action": "Start cadence",
        "name": "QA Nameone", "name_replaces": "QA Namekeep"}
LEAD = "00000000-0000-4000-8000-000000000001"


def _kept_row(**cells):
    sheet, number, grid = _grid_with(**{"name": "QA Namekeep", "phone": "5555550101",
                                        "lead_id": LEAD, "action": "",
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


# --- unknown text in Action (owner, 2026-10-26) ------------------------------------------------

UNKNOWN = "Error: action must be chosen from the drop-down."


@pytest.mark.parametrize("lead_id", ["", LEAD])
@pytest.mark.parametrize("text", ["restart", "please call", "Start", "booked it", "booked",
                                  "restart cadence", "START CADENCE", "do not contact"])
def test_unknown_action_text_says_so_and_empties_and_nothing_runs(old, monkeypatch, notes, lead_id,
                                                                  text):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action=text, lead_id=lead_id, outcome_status="Cadence started",
                       action_request_id=str(uuid4()) if lead_id else "")
    assert sheet_direct.needs_attention(parse_grid(sheet.read_values(), 2).rows[0], set())
    assert _poll(sheet, monkeypatch, sheet_direct.Outcome("Done", None, LEAD)) == []
    assert sheet.cell(number, "outcome_status") == UNKNOWN
    assert sheet.cell(number, "action") == "" and sheet.cell(number, "action_request_id") == ""
    assert sheet.cell(number, "lead_id") == lead_id
    assert notes == [(number, f'"{text}"', UNKNOWN)]  # in the Activity of a linked lead only
    assert _poll(sheet, monkeypatch) == []


@pytest.mark.parametrize(("text", "action"), [("Restart cadence", "restart_cadence"),
                                              ("  Booked ", "booked"),
                                              ("Do Not  Contact", "do_not_contact")])
def test_a_drop_down_command_with_extra_spaces_runs(old, monkeypatch, text, action):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action=text, lead_id=LEAD)
    assert _poll(sheet, monkeypatch, sheet_direct.Outcome("Done", None, LEAD)) == [action]
    assert sheet.cell(number, "outcome_status") == "Done" and sheet.cell(number, "action") == ""


def test_unknown_text_waits_for_the_row_to_settle():
    sheet = FakeSheet()
    sheet.add(**ROW, action="Restart cad")  # still typing
    settle = sheet_direct.Settle(60)

    def claim(now):
        grid = parse_grid(sheet.read_values(), 2)
        sheet_direct.claim_commands(grid, WorkflowTrace("t", "test"), settle=settle, now=now)
        return grid.rows[0].get("outcome_status")

    assert claim(0) == "" and claim(59) == ""
    assert claim(61) == UNKNOWN


def test_a_command_staff_pick_over_unknown_text_before_the_write_is_kept(old, monkeypatch):
    from rpt_agent.services.google_sheet import flush

    sheet = FakeSheet()
    number = sheet.add(**ROW, action="restart", lead_id=LEAD)
    grid = parse_grid(sheet.read_values(), 2)
    assert sheet_direct.claim_commands(grid, WorkflowTrace("t", "test")) == []
    sheet.before_write = lambda: sheet.set(number, "action", "Restart cadence")
    flush(sheet, grid)
    assert sheet.cell(number, "action") == "Restart cadence"
    done = sheet_direct.Outcome("Cadence restarted", "In cadence", LEAD)
    assert _poll(sheet, monkeypatch, done) == ["restart_cadence"]


def test_an_empty_action_is_left_alone(old, monkeypatch):
    sheet = FakeSheet()
    number = sheet.add(**ROW, action="  ", lead_id=LEAD, outcome_status="Cadence started")
    assert _poll(sheet, monkeypatch) == []
    assert sheet.cell(number, "outcome_status") == "Cadence started" and sheet.writes == []


def test_a_start_row_with_a_wrong_email_starts_with_a_warning(old, monkeypatch):
    sheet = FakeSheet()
    number = sheet.add(**{**ROW, "email": "maria@gmail"}, action="Start cadence")
    started = sheet_direct.Outcome("Cadence started", "In cadence", LEAD_ID)
    assert _poll(sheet, monkeypatch, started) == ["start_cadence"]
    assert sheet.cell(number, "outcome_status") == (
        "Cadence started; email looks wrong, please fix it")
    assert sheet.cell(number, "lead_status") == "In cadence"


@pytest.mark.parametrize(("retry_at", "clock"), [
    ("2026-10-26T10:14:50+00:00", "3:15 AM PT"),  # restarted 3:04:50 -> next full minute
    ("2026-10-26T10:15:00+00:00", "3:15 AM PT"),  # exact minute stays
    ("2026-10-26T10:14:00.000001+00:00", "3:15 AM PT"),
    ("2026-10-26T10:59:30+00:00", "4:00 AM PT"),
])
def test_restart_too_soon_time_rounds_up_to_the_next_minute(retry_at, clock):
    # Picking Restart at the shown time must not be refused again (owner, 2026-10-26).
    text = sheet_direct._refusal_text({"code": "restart_too_soon", "retry_at": retry_at})
    assert text == ("Error: restarted less than 10 minutes ago. "
                    f"Pick Restart cadence again after {clock}.")


@pytest.mark.parametrize(("retry_at", "clock"), [
    ("2026-10-26T22:05:20+00:00", "3:06 PM PT"),  # call due 3:00:20 PM + 5 minutes -> rounded up
    ("2026-10-26T22:05:00+00:00", "3:05 PM PT"),
    ("2026-10-26T21:59:59.5+00:00", "3:00 PM PT"),
])
def test_a_restart_refused_for_a_due_call_says_when_to_pick_it_again(retry_at, clock):
    # Owner 2026-10-26: a call or text due within 5 minutes (30 s for test leads) refuses a Restart.
    body = {"code": "outreach_due_soon", "retry_at": retry_at}
    assert sheet_direct._refusal_text(body) == (
        f"Error: a call or text is due in the next few minutes. Pick Restart cadence again after {clock}.")
    outcome = sheet_direct.label_result(ActionExecution(409, {**body, "detail": "x", "lead_id": "L1"}))
    assert outcome.status == sheet_direct._refusal_text(body)
