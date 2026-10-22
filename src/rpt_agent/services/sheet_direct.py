"""Google Sheet <-> backend, run by the sheet-worker itself (replaces the four n8n workflows).

One poll = one read of the whole tab, then at most two batched writes:
1. Commands (n8n "lead intake"): a row whose Action is one of the four commands and whose
   Outcome Status says it still needs doing is claimed ("Processing: X" + a new Action Request
   ID), executed through the same `execute_lead_action` the n8n endpoint used, and labelled.
2. Stuck commands (n8n "recovery"): a row left at "Processing: X" / "Retrying n/3: X" for
   SHEET_STUCK_SECONDS is re-run with its existing Action Request ID, so a command the backend
   already committed is replayed, never applied twice.
3. Profile edits (n8n "lead-sync"): a linked row whose Name / Phone / Email / Date Of Birth /
   Location / Case fingerprint changed since the last poll is sent to `sync_sheet_lead`.
4. Results (n8n "webhook-flow"): outbox snapshots are written into the lead's row, found by
   Lead ID.

Labels, the "does this row need processing" rules and the phone / date-of-birth rules are the
n8n ones (spec: context/PROJECT_CONTEXT.md, 2026-10-14 entry), so staff see the same Sheet.
Our own writes never trigger anything: commands key off Outcome Status, which we always set to
a final or in-progress label, and the profile fingerprint ignores the columns we write.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ..db import transaction
from ..observability import WorkflowTrace
from .google_sheet import COLUMNS, SheetBackend, SheetGrid, SheetRow, as_text
from .lead_actions import (
    ActionExecution,
    _replay_stored_response,
    _request_hash,
    execute_lead_action,
    sync_sheet_lead,
)
from .sheet_sync import dashboard_call_link

SHEET_TZ = ZoneInfo("America/Los_Angeles")

# Sheet Action text -> (label, backend action). Exact, as n8n matched it (only extra spaces are
# forgiven): a row n8n ignored, say "booked" in lower case, must not suddenly run at cutover.
COMMANDS = {
    "Start cadence": ("Start cadence", "start_cadence"),
    "Restart cadence": ("Restart cadence", "restart_cadence"),
    "Do Not Contact": ("Do Not Contact", "do_not_contact"),
    "Booked": ("Booked", "booked"),
}
# Backend result -> (Outcome Status, Lead Status)
RESULT_LABELS = {
    "cadence_started": ("Cadence started", "In cadence"),
    "already_started": ("Already in Cadence", "In cadence"),
    "cadence_restarted": ("Cadence restarted", "In cadence"),
    "do_not_contact_applied": ("Do Not Contact applied", "Do not outreach"),
    "booked_applied": ("Booked", "Booked"),
    "already_booked": ("Already booked", "Booked"),
    # Lead created (or restarted) but its number is blocked: nothing goes out until an admin unblocks.
    "number_blocked_needs_review": ("Number blocked; needs review", "Needs Review"),
}
REVIEW_LABELS = {
    "name_changed_needs_review": "Needs review: name changed",
}
# A changed phone number closes the lead at once (owner, 2026-10-22): Outcome Status, Lead Status.
# Action is set to "Start cadence" with it, as for a dashboard resume (sheet_sync.DASHBOARD_OUTCOMES):
# a stale "Restart cadence" or "Booked" next to a status that is not final would run again.
PHONE_CHANGED_LABELS = {"phone_changed_closed": ("Phone changed: add a new row", "Closed")}
FINAL_STATUSES = {"booked", "already booked", "do not contact applied", "completed",
                  "lead already exists"}
# On a row in review, Booked and Do Not Contact still run: they end the lead whatever the review was
# about, and the backend closes the review as "booked" / "do_not_contact" (as the dashboard's Booked
# move and Do not contact switch do). QA 2026-10-22: a Booked typed on a "Needs review: wrong number"
# row was ignored without a word. Start / Restart would skip the decision the review asks for.
REVIEW_ENDING = {"booked", "do_not_contact"}
# Added to the review text when staff pick Restart cadence on a row in review. The Restart is kept
# and runs once the review is resolved on the dashboard (the Resolved text is not final).
REVIEW_FIRST = "; resolve it on the dashboard first, then the restart runs"
# A dashboard decision that ends the lead (close, front desk, booking link, Booked, Do not contact) is
# newer than the parked Restart and wins (tester 2026-10-22: "Front desk will handle", then the
# Restart ran two minutes later and calls started again). The row says the Restart was dropped.
NOT_RUN = "; the Restart in Action was not run"
ENDED_LEAD_STATUSES = {"Closed", "Booked", "Do not outreach"}
RETRYING = re.compile(r"^retrying ([12])/3: ", re.IGNORECASE)
PROFILE = ("name", "phone", "email", "date_of_birth", "location", "case")
FIELD_COLUMNS = {"full_name": "Name", "phone": "Phone Number", "email": "Email", "gender": "Gender",
                 "date_of_birth": "Date Of Birth", "location": "Location", "title": "Case",
                 "lead_type": "Case"}
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# --- parsing (the n8n intake rules) --------------------------------------------------------


def normalize_phone(value: Any) -> str | None:
    """+91 only for 12 digits starting 91 (or 14 starting 0091); every 10-digit number is +1."""
    text = str(value if value is not None else "").strip()
    if not text or not re.fullmatch(r"\+?[\d\s().-]+", text):
        return None
    digits = re.sub(r"\D", "", text)
    if digits.startswith("0091") and len(digits) == 14:
        return "+91" + digits[4:]
    if digits.startswith("91") and len(digits) == 12:
        return "+91" + digits[2:]
    if digits.startswith("001") and len(digits) == 13:
        digits = digits[3:]
    elif digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return "+1" + digits if len(digits) == 10 else None


def _serial_date(text: str) -> date | None:
    try:
        serial = float(text)
    except ValueError:
        return None
    if not math.isfinite(serial) or not 1 <= serial <= 100000:
        return None
    return date(1899, 12, 30) + timedelta(days=math.floor(serial))


def normalize_dob(value: Any) -> str | None:
    """DD/MM/YYYY, from what staff type or a date cell (read as a serial number).

    Staff type US dates, month first (clinic, 2026-10-21: "05/09/1990" is May 9). Day first only when
    the first part cannot be a month ("16/01/2007"). Until then the n8n rule read day first.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if m := re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text):
        return f"{int(m[3]):02d}/{int(m[2]):02d}/{m[1]}"
    if m := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text):
        first, second = int(m[1]), int(m[2])
        day, month = (first, second) if second <= 12 < first else (second, first)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{day:02d}/{month:02d}/{m[3]}"
    if found := _serial_date(text):
        return found.strftime("%d/%m/%Y")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return None


def parse_started_at(value: Any) -> datetime | None:
    """Action Started At: our ISO text, or n8n's LA wall-clock time read as a date serial."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        serial = float(text)
    except ValueError:
        serial = None
    if serial is not None:
        if not math.isfinite(serial) or serial < 1:
            return None
        wall = datetime(1899, 12, 30, tzinfo=SHEET_TZ) + timedelta(days=serial)
        return wall.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHEET_TZ).astimezone(UTC)


def command_of(row: SheetRow) -> tuple[str, str] | None:
    return COMMANDS.get(" ".join(row.get("action").split()))


def open_review_text(status: str) -> bool:
    """An Outcome Status this backend writes for an open review (lead_review.sheet_open_status).

    Older n8n texts ("Phone number changed; needs review") are left out on purpose: those rows
    can still hold the stale Booked that n8n re-sent (2026-10-13), so they keep blocking everything.
    """
    text = status.strip().casefold()
    return text.startswith(("needs review", "number blocked; needs review"))


def _restart_ran(rows) -> set[int]:
    """Rows whose Action Request ID is a stored Restart cadence: that Restart already ran here."""
    ids = {row.number: row.get("action_request_id") for row in rows}
    wanted = [rid for rid in ids.values() if _is_uuid(rid)]
    if not wanted:
        return set()
    with transaction() as conn:
        restarts = {r["request_id"] for r in conn.execute(
            # Compared as text: the live databases store request_id as text, the migrations as
            # uuid; a uuid comparison failed every Sheet poll on the demo (2026-10-21).
            "select request_id::text as request_id from lead_action_requests "
            "where request_id::text=any(%s::text[]) and action='restart_cadence'", (wanted,))}
    return {number for number, rid in ids.items() if rid in restarts}


def restart_reruns(rows) -> set[int]:
    """Rows where staff chose Restart cadence on a cadence that has ended ("Cadence completed").

    Runs only when the cadence that ended was not itself started by this Restart (the row's last
    request was a Start, or none is stored): otherwise every finished restart would restart itself
    again, forever. One lookup per poll, only for these rows. Owner request 2026-10-20: staff had to
    clear Outcome Status before such a Restart did anything.
    """
    candidates = [row for row in rows if (command_of(row) or ("", ""))[1] == "restart_cadence"
                  and row.get("outcome_status").strip().casefold() == "cadence completed"]
    if not candidates:
        return set()
    ran = _restart_ran(candidates)
    return {row.number for row in candidates if row.number not in ran}


def parked(row: SheetRow) -> bool:
    """Restart cadence on a row in review: staff typed it, and it waits until the review is resolved.

    Typed, because the backend never leaves a Restart next to a review text it writes: a flag turns
    a Restart already run into "Start cadence" (`_show_review`, `_write_outcome`), as a resolution
    does. Not the row's Action Request ID: after a Restart ran once it still names that Restart, and
    a Restart typed on the next review was taken for the old one and lost (tester 2026-10-22).
    """
    return ((command_of(row) or ("", ""))[1] == "restart_cadence"
            and open_review_text(row.get("outcome_status")))


def _hinted(row: SheetRow) -> bool:
    return row.get("outcome_status").casefold().endswith(REVIEW_FIRST.casefold())


def _hint_due(row: SheetRow) -> bool:
    """A parked Restart whose row does not say yet that it waits for the review."""
    return parked(row) and not _hinted(row)


def _hint_stale(row: SheetRow) -> bool:
    """The row still says a Restart waits, but staff took it out of Action."""
    return _hinted(row) and not parked(row)


def _with_hint(status: str, is_parked: bool) -> str:
    """A review text written over a parked Restart keeps saying the Restart waits."""
    if is_parked and open_review_text(status) and not status.casefold().endswith(
            REVIEW_FIRST.casefold()):
        return status + REVIEW_FIRST
    return status


def needs_processing(row: SheetRow, command: tuple[str, str], reruns: set[int] | None = None) -> bool:
    """The n8n "Action Needs Processing?" rules (live 2026-10-13), plus one fix.

    Fix: a Start cadence row that failed before it got a Lead ID can be re-run by clearing
    Outcome Status (n8n also demanded clearing Action Request ID, so staff got stuck);
    `_start_row_already_done` makes that re-run safe.
    Review (2026-10-22): Booked and Do Not Contact run on a row in review (REVIEW_ENDING); Start and
    Restart do not (a Restart is `parked` until the review is resolved).
    """
    _, action = command
    status = row.get("outcome_status").casefold()
    if action == "start_cadence" and (row.get("lead_id") or (status and row.get("action_request_id"))):
        return False
    if not status:
        return True
    if status.endswith(" in dashboard"):
        return action != "do_not_contact"
    if status.startswith(("processing:", "retrying ", "error:")):
        return False
    if "needs review" in status:
        return action in REVIEW_ENDING and open_review_text(status)
    if status in FINAL_STATUSES:
        return False
    if action == "start_cadence" and status in {"cadence started", "already in cadence",
                                                "cadence completed"}:
        return False
    if action == "restart_cadence" and status == "cadence completed":
        return row.number in (reruns or set())
    return not (action == "restart_cadence" and status == "cadence restarted")


def _readable(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_context=False):
        names = [str(p) for p in item.get("loc", ()) if isinstance(p, str) and p != "lead"]
        column = FIELD_COLUMNS.get(names[-1], names[-1]) if names else ""
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{column} {message}".strip() if column else message)
    return "; ".join(dict.fromkeys(parts)) or "request rejected"


def build_action(row: SheetRow, action: str) -> tuple[Any | None, str | None]:
    """(LeadActionRequest, None) or (None, 'reason') — the n8n intake checks, then the API's."""
    from ..routes.n8n import LeadActionRequest  # local: routes import services

    errors = []
    phone = normalize_phone(row.get("phone"))
    dob = normalize_dob(row.get("date_of_birth"))
    if not phone:
        errors.append("invalid phone number")
    lead_id = row.get("lead_id") or None
    if action != "start_cadence" and not lead_id:
        errors.append("Lead ID is required")
    if action == "start_cadence":
        for key, column in (("name", "Name"), ("date_of_birth", "Date Of Birth"),
                            ("location", "Location"), ("case", "Case")):
            if not row.get(key):
                errors.append(f"{column} is required")
        if row.get("date_of_birth") and not dob:
            errors.append("Date Of Birth must be MM/DD/YYYY")
    email = row.get("email").lower()
    if email and not EMAIL.match(email):
        errors.append("invalid email")
    if errors:
        return None, "; ".join(errors)
    try:
        request = LeadActionRequest.model_validate({
            "action": action,
            "lead_id": lead_id,
            "lead": _lead_fields(row, phone, dob, email),
        })
    except ValidationError as exc:
        return None, _readable(exc)
    return request, None


def _lead_fields(row: SheetRow, phone: str | None, dob: str | None, email: str) -> dict[str, Any]:
    return {
        "full_name": row.get("name") or None,
        "phone": phone,
        "email": email or None,
        "gender": row.get("gender") or None,
        "date_of_birth": dob,
        "location": row.get("location") or None,
        "title": row.get("case") or None,
    }


def profile_hash(row: SheetRow) -> str:
    joined = "\x1f".join(" ".join(row.get(key).split()) for key in PROFILE)
    # Gender (2026-10-20) joins the fingerprint only when filled, so adding the column does not
    # make every existing row look edited (and re-synced) at once.
    if row.get("gender").strip():
        joined += "\x1fgender=" + " ".join(row.get("gender").split()).casefold()
    return hashlib.sha256(joined.encode()).hexdigest()


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


# --- outcomes -------------------------------------------------------------------------------


@dataclass
class Outcome:
    status: str
    lead_status: str | None = None
    lead_id: str | None = None
    temporary: bool = False


def label_result(result: ActionExecution) -> Outcome:
    body = result.body or {}
    lead_id = body.get("lead_id")
    if result.status_code in (200, 201):
        status, lead_status = RESULT_LABELS.get(body.get("result"), ("Completed", None))
        return Outcome(status, lead_status, lead_id)
    still_running = result.status_code == 409 and body.get("detail") == "Request is still processing"
    if result.status_code in (408, 425, 429) or result.status_code >= 500 or still_running:
        return Outcome("", temporary=True)
    if body.get("code") in REVIEW_LABELS:
        return Outcome(REVIEW_LABELS[body["code"]], "Needs Review", lead_id)
    detail = body.get("detail") if isinstance(body.get("detail"), str) else body.get("code")
    return Outcome("Error: " + str(detail or "request rejected")[:180], None, lead_id)


# --- one poll -------------------------------------------------------------------------------


@dataclass
class PollResult:
    counts: dict[str, int] = field(default_factory=lambda: {
        "commands": 0, "recovered": 0, "invalid": 0, "profiles": 0, "baselines": 0})
    handled: set[int] = field(default_factory=set)  # row numbers a command touched this poll
    # Profile fingerprints to store once this poll's writes are in the Sheet (lead id ->
    # (fingerprint, row)); a write that fails or is deferred leaves the change to the next poll.
    # lead id -> (fingerprint, row, the row's Name): the name is what a pre-048 name review lacks.
    baselines: dict[str, tuple[str, int, str]] = field(default_factory=dict)

    def save_baselines(self, deferred_rows: set[int]) -> None:
        _save_baseline({lead_id: item for lead_id, item in self.baselines.items()
                        if item[1] not in deferred_rows})


def _now() -> datetime:
    return datetime.now(UTC)


INPUT = ("name", "phone", "email", "gender", "date_of_birth", "location", "case", "action",
         "outcome_status", "lead_id", "action_request_id")


@dataclass
class Settle:
    """A command row runs only after its cells stayed the same for `seconds`.

    Staff fill a row over a minute or two and may pick the Action before they finish: acting
    on the first poll that sees "Start cadence" would start a half-typed row or show an error.
    Remembered per row in memory; a worker restart only delays rows by one settle period.
    """
    seconds: float
    seen: dict[int, tuple[str, float]] = field(default_factory=dict)

    def ready(self, row: SheetRow, now: float) -> bool:
        if self.seconds <= 0:
            return True
        fingerprint = hashlib.sha256(
            "\x1f".join(" ".join(row.get(k).split()) for k in INPUT).encode()).hexdigest()
        first = self.seen.get(row.number)
        if first is None or first[0] != fingerprint:
            self.seen[row.number] = (fingerprint, now)
            return False
        return now - first[1] >= self.seconds

    def keep(self, numbers: set[int]) -> None:
        self.seen = {n: v for n, v in self.seen.items() if n in numbers}


def claim_commands(grid: SheetGrid, trace: WorkflowTrace, *, settle: Settle | None = None,
                   now: float = 0.0, limit: int | None = None) -> list[tuple[SheetRow, Any, str]]:
    """Queue "Processing" claims (and validation errors); returns the rows to run after the flush.

    At most `limit` commands per poll (top rows first): a pasted block of thousands of rows is
    worked through over several polls instead of one poll that blocks everything else.
    """
    claims = []
    waiting: set[int] = set()
    reruns = restart_reruns(grid.rows)
    for row in grid.rows:
        if _hint_stale(row):
            grid.set(row, "outcome_status", row.get("outcome_status")[:-len(REVIEW_FIRST)])
        command = command_of(row)
        if not command:
            continue
        hint = _hint_due(row)
        if not hint and not needs_processing(row, command, reruns):
            continue
        waiting.add(row.number)
        if settle is not None and not settle.ready(row, now):
            continue
        if hint:
            # A Restart picked on a row in review used to be ignored without a word (QA
            # 2026-10-22). It still waits for the review; the row now says so, and keeps it.
            grid.set(row, "outcome_status", _with_hint(row.get("outcome_status"), True))
            trace.log("sheet_command_waits_for_review", row=row.number, action=command[1])
            continue
        if limit is not None and len(claims) >= limit:
            continue
        label, action = command
        request, error = build_action(row, action)
        if error:
            grid.set(row, "outcome_status", "Error: " + error[:180])
            trace.log("sheet_command_invalid", row=row.number, action=action)
            continue
        if action == "start_cadence" and (done := _start_row_already_done(row, request)):
            _write_outcome(grid, row, done)
            continue
        request_id = str(uuid4())
        grid.set(row, "action_request_id", request_id)
        grid.set(row, "action_started_at", _now().isoformat().replace("+00:00", "Z"))
        grid.set(row, "outcome_status", f"Processing: {label}")
        claims.append((row, request, request_id))
    if settle is not None:
        settle.keep(waiting)
    return claims


def _start_row_already_done(row: SheetRow, request) -> Outcome | None:
    """A re-run Start row whose earlier request did create THIS lead: show it, don't start twice.

    Only when the stored request is this row's request exactly (same request hash): a row copied
    for a family member keeps the old Action Request ID but is another person.
    """
    old = row.get("action_request_id")
    if not old or not _is_uuid(old):
        return None
    with transaction() as conn:
        stored = conn.execute(
            "select r.lead_id,r.request_hash from lead_action_requests r "
            # As text: the live lead_action_requests.lead_id is text, the migrated one uuid.
            "join leads l on l.id::text=r.lead_id::text "
            "where r.request_id=%s and r.status='completed' and r.action='start_cadence'",
            (old,),
        ).fetchone()
    lead = request.lead.model_dump(mode="python")
    if not stored or stored["request_hash"] != _request_hash("start_cadence", None, lead):
        return None
    return Outcome("Cadence started", "In cadence", str(stored["lead_id"]))


def run_claims(grid: SheetGrid, claims, deferred: set[int], result: PollResult,
               trace: WorkflowTrace) -> None:
    for row, request, request_id in claims:
        if row.number in deferred:
            continue  # the row moved before the claim landed; next poll sees it fresh
        label = command_of(row)[0]
        outcome = _execute(request, request_id, trace)
        if outcome.temporary:
            outcome = Outcome(f"Retrying 1/3: {label}")
        _write_outcome(grid, row, outcome, result if request.action == "start_cadence" else None)
        result.handled.add(row.number)
        result.counts["commands"] += 1


def recover_stuck(grid: SheetGrid, result: PollResult, stuck_seconds: int,
                  trace: WorkflowTrace) -> None:
    """Re-run commands left in progress, with the same Action Request ID (n8n recovery flow)."""
    now = _now()
    for row in grid.rows:
        if row.number in result.handled:
            continue
        status = row.get("outcome_status")
        if status.casefold().startswith("processing: "):
            tries = 1
        elif match := RETRYING.match(status):
            tries = int(match[1])
        else:
            continue
        command, request_id = command_of(row), row.get("action_request_id")
        if not command or not _is_uuid(request_id):
            continue  # malformed: left for staff, as n8n did
        started = parse_started_at(row.get("action_started_at"))
        if not started or started > now or now - started < timedelta(seconds=stuck_seconds):
            continue  # no readable start time: left for staff, as n8n did
        label, action = command
        # The backend may already have the answer (it committed, the Sheet never heard): replay
        # it as stored. Rebuilding from the row could differ if staff edited it meanwhile.
        outcome = _stored_outcome(request_id)
        if outcome is None:
            request, error = build_action(row, action)
            if error:
                continue  # the row changed into something invalid: left for staff, as n8n did
            outcome = _execute(request, request_id, trace)
        if outcome.temporary and tries + 1 < 3:
            outcome = Outcome(f"Retrying {tries + 1}/3: {label}")
            grid.set(row, "action_started_at", now.isoformat().replace("+00:00", "Z"))
        elif outcome.temporary:
            outcome = Outcome("Error: backend unavailable after 3 attempts")
        _write_outcome(grid, row, outcome, result if action == "start_cadence" else None)
        result.handled.add(row.number)
        result.counts["recovered"] += 1


def _stored_outcome(request_id: str) -> Outcome | None:
    with transaction() as conn:
        stored = conn.execute(
            "select status,response_body from lead_action_requests where request_id=%s",
            (request_id,),
        ).fetchone()
    if not stored or stored["status"] not in {"completed", "failed"}:
        return None
    replayed = _replay_stored_response(stored["response_body"])
    return label_result(replayed) if replayed else None


def _execute(request, request_id: str, trace: WorkflowTrace) -> Outcome:
    try:
        executed = execute_lead_action(
            request_id=UUID(request_id),
            action=request.action,
            lead_id=request.lead_id,
            lead=request.lead.model_dump(mode="python"),
        )
    except Exception as exc:  # noqa: BLE001 - database trouble: retried like an n8n timeout
        trace.log("sheet_command_failed", error_category=type(exc).__name__)
        return Outcome("", temporary=True)
    trace.log("sheet_command_done", action=request.action, http_status=executed.status_code)
    return label_result(executed)


def _write_outcome(grid: SheetGrid, row: SheetRow, outcome: Outcome,
                   result: PollResult | None = None) -> None:
    # n8n: Lead ID = the backend's, else the row's own; the link follows it.
    lead_id = outcome.lead_id or row.get("lead_id")
    grid.set(row, "lead_id", lead_id or "")
    grid.set(row, "transcript_link", (dashboard_call_link(lead_id) or "") if lead_id else "")
    if outcome.lead_status:
        grid.set(row, "lead_status", outcome.lead_status)
    grid.set(row, "outcome_status", outcome.status)
    # A Restart that ended in a review ("Number blocked; needs review") has run: next to that text
    # it would read as a Restart staff typed (`parked`), so the cell gets "Start cadence".
    if open_review_text(outcome.status) and (command_of(row) or ("", ""))[1] == "restart_cadence":
        grid.set(row, "action", "Start cadence")
    # A new lead starts from the row as it is: no profile "change" to send next poll. Other
    # commands leave the fingerprint alone, so a profile edit made with them still syncs.
    if result is not None and lead_id and _is_uuid(lead_id) and not outcome.status.startswith(
            ("Retrying", "Error")):
        result.baselines[lead_id] = (profile_hash(row), row.number, _sheet_name(row))


def sync_profiles(grid: SheetGrid, result: PollResult, trace: WorkflowTrace,
                  previous: dict[str, str] | None = None) -> dict[str, str]:
    """Send changed Name/Phone/Email/DOB/Location/Case of linked rows (n8n lead-sync flow).

    `previous` = fingerprints from the last full read. An edit is sent only once it looks the
    same on two full reads in a row, so a row caught half-edited (new name typed, phone not yet)
    is never flagged for review. Returns this read's fingerprints for the next call; None skips
    the wait (tests, one-off runs).
    """
    from ..routes.n8n import SheetLeadInput  # local: routes import services

    first: dict[str, SheetRow] = {}
    for row in grid.rows:
        lead_id = row.get("lead_id")
        if lead_id and _is_uuid(lead_id) and row.number not in result.handled:
            first.setdefault(lead_id, row)  # duplicate Lead ID rows: the first one counts
    seen_now = {lead_id: profile_hash(row) for lead_id, row in first.items()}
    if not first:
        return seen_now
    with transaction() as conn:
        stored = {
            str(r["lead_id"]): r["profile_hash"]
            for r in conn.execute(
                "select lead_id,profile_hash from sheet_row_state where lead_id=any(%s)",
                (list(first),),
            )
        }
        no_name = {
            str(r["lead_id"]) for r in conn.execute(
                "select lead_id from sheet_row_state where lead_id=any(%s) and sheet_name is null",
                (list(first),),
            )
        }
    baselines = result.baselines
    for lead_id, row in first.items():
        fingerprint = profile_hash(row)
        if lead_id not in stored:
            baselines[lead_id] = (fingerprint, row.number, _sheet_name(row))  # first sight: remember
            result.counts["baselines"] += 1
            continue
        if stored[lead_id] == fingerprint:
            if lead_id in no_name:  # rows seen before 048: record the Name the row holds
                baselines[lead_id] = (fingerprint, row.number, _sheet_name(row))
            continue
        if previous is not None and previous.get(lead_id) != fingerprint:
            continue  # changed since the last full read: wait until it holds still
        phone, dob = normalize_phone(row.get("phone")), normalize_dob(row.get("date_of_birth"))
        email = row.get("email").lower()
        errors = []
        if not phone:
            errors.append("invalid phone number")
        if row.get("date_of_birth") and not dob:
            errors.append("Date Of Birth must be MM/DD/YYYY or YYYY-MM-DD")
        if email and not EMAIL.match(email):
            errors.append("invalid email")
        lead = None
        if not errors:
            try:
                lead = SheetLeadInput.model_validate(_lead_fields(row, phone, dob, email))
            except ValidationError as exc:
                errors.append(_readable(exc))
        if errors:
            grid.set(row, "lead_status", "Needs Review")
            grid.set(row, "outcome_status", "Error: " + "; ".join(errors)[:180])
        else:
            try:
                synced = sync_sheet_lead(request_id=uuid4(), lead_id=UUID(lead_id),
                                         lead=lead.model_dump(mode="python"))
            except Exception as exc:  # noqa: BLE001 - try again next poll
                trace.log("sheet_profile_failed", error_category=type(exc).__name__)
                continue
            code = (synced.body or {}).get("code")
            if code in REVIEW_LABELS:
                grid.set(row, "lead_status", "Needs Review")
                _show_review(grid, row, REVIEW_LABELS[code], pending=_command_pending(row),
                             is_parked=parked(row))
            elif code in PHONE_CHANGED_LABELS:
                outcome_status, lead_status = PHONE_CHANGED_LABELS[code]
                grid.set(row, "lead_status", lead_status)
                if not _command_pending(row):  # a command staff just typed runs (and is refused)
                    # The lead is closed: a Restart waiting for a review goes, and the row says so.
                    grid.set(row, "outcome_status",
                             outcome_status + (NOT_RUN if parked(row) else ""))
                    grid.set(row, "action", "Start cadence")
            elif synced.status_code >= 400:
                detail = synced.body.get("detail") if isinstance(synced.body.get("detail"), str) \
                    else synced.body.get("code")
                grid.set(row, "outcome_status", "Error: " + str(detail or "request rejected")[:180])
        baselines[lead_id] = (fingerprint, row.number, _sheet_name(row))
        result.counts["profiles"] += 1
    return seen_now


def _sheet_name(row: SheetRow) -> str:
    return " ".join(row.get("name").split())


def _save_baseline(items: dict[str, tuple[str, int, str]]) -> None:
    if not items:
        return
    with transaction() as conn, conn.cursor() as cursor:
        cursor.executemany(
            "insert into sheet_row_state(lead_id,practice_id,profile_hash,row_number,sheet_name) "
            "select id,practice_id,%s,%s,%s from leads where id=%s "
            "on conflict(lead_id) do update set profile_hash=excluded.profile_hash,"
            "row_number=excluded.row_number,sheet_name=excluded.sheet_name,seen_at=now(),"
            "updated_at=now()",
            [(fingerprint, number, name or None, lead_id)
             for lead_id, (fingerprint, number, name) in items.items()],
        )
        # A name review flagged before 2026-10-22 never recorded the new name (the D. S. lead).
        # The row's Name read now fills it in, so the card shows old -> new and the normal choices
        # apply with real values. Nothing is resolved or flagged by this.
        cursor.execute(
            "update lead_reviews r set details=r.details||jsonb_build_object("
            "'old_name',l.full_name,'new_name',s.sheet_name),updated_at=now() "
            "from leads l join sheet_row_state s on s.lead_id=l.id "
            "where r.lead_id=l.id and l.id=any(%s) and r.code='name_changed' and r.resolved_at is null "
            "and not (r.details ? 'new_name') and s.sheet_name is not null "
            "and lower(s.sheet_name)<>lower(regexp_replace(trim(l.full_name),'\\s+',' ','g'))",
            ([lead_id for lead_id in items],),
        )


# --- results from the backend (n8n webhook-flow) ---------------------------------------------

ALWAYS_WRITTEN = ("cadence_day", "call_outcome", "message_outcome", "email_outcome",
                  "transcript_link")


def apply_snapshot(grid: SheetGrid, snapshot: dict[str, Any]) -> bool:
    """Write one lead's snapshot into its row (first row with that Lead ID). False: not in Sheet."""
    lead_id = str(snapshot["lead_id"])
    row = next((r for r in grid.rows if r.get("lead_id") == lead_id), None)
    if row is None:
        return False
    sheet = snapshot.get("sheet") or {}
    # What staff typed is judged on the row as it is now, before this snapshot writes into it.
    pending = _command_pending(row)
    waits = not pending and parked(row)
    for key in ALWAYS_WRITTEN:
        if key in sheet:
            grid.set(row, key, sheet[key] or "")
    if waits and sheet.get("lead_status") in ENDED_LEAD_STATUSES:
        _drop_parked(grid, row, sheet)
        grid.set(row, "lead_status", _lead_status(row, sheet))
        return True
    if "action_status" in sheet:
        grid.set(row, "outcome_status", _with_hint(sheet["action_status"] or "", waits))
    # Review flagged / resolved (services/lead_review.py). Never over a command staff typed and the
    # Sheet worker has not run yet (still settling, or waiting) or one still running: QA 2026-10-22,
    # "Start cadence" overwrote a Booked typed seconds earlier, and a new "Needs review" text would
    # block it. The command's own result is written when it runs.
    if sheet.get("review_status") and not pending:
        if open_review_text(sheet["review_status"]):
            _show_review(grid, row, sheet["review_status"], pending=pending, is_parked=waits)
        else:
            grid.set(row, "outcome_status", sheet["review_status"])
        # A Restart parked behind the review keeps its cell: with the Resolved text it now runs.
        if sheet.get("review_action") and not waits:
            grid.set(row, "action", sheet["review_action"])
    # n8n never wrote this (spec F.3); the backend sends it to stop a stale command re-running
    # ("Booked" once booked, "" after a Stride cancel). Only over a cell no command is waiting in:
    # QA 2026-10-22, a Booked staff typed was replaced before it ran and the lead was never booked.
    if "action" in sheet and not pending and not waits:
        grid.set(row, "action", sheet["action"] or "")
    grid.set(row, "lead_status", _lead_status(row, sheet))
    return True


def _show_review(grid: SheetGrid, row: SheetRow, text: str, *, pending: bool,
                 is_parked: bool) -> None:
    """Write an open review's text (pending / is_parked: the row before this write).

    A Restart left in Action from before the review (it ran from the Sheet, or the dashboard wrote
    it with "Cadence restarted") becomes "Start cadence", as a resolution does: next to the review
    text it would read as a Restart staff just picked, wait for the review, and run once resolved.
    """
    stale = (not pending and not is_parked
             and (command_of(row) or ("", ""))[1] == "restart_cadence")
    grid.set(row, "outcome_status", _with_hint(text, is_parked))
    if stale:
        grid.set(row, "action", "Start cadence")


def _drop_parked(grid: SheetGrid, row: SheetRow, sheet: dict[str, Any]) -> None:
    """The lead ended on the dashboard (or by Stride) while a Restart waited: the Restart goes."""
    text = sheet.get("review_status") or sheet.get("action_status") or sheet.get("lead_status") or ""
    if text.strip().casefold() not in FINAL_STATUSES:  # "Booked" stays exactly that (final)
        text += NOT_RUN
    grid.set(row, "outcome_status", text)
    grid.set(row, "action", sheet.get("action") or sheet.get("review_action") or "Start cadence")


def _in_progress(status: str) -> bool:
    return status.casefold().startswith("processing: ") or bool(RETRYING.match(status))


def _command_pending(row: SheetRow) -> bool:
    """A command in the row that has not finished: typed (waiting or settling) or running."""
    command = command_of(row)
    return _in_progress(row.get("outcome_status")) or bool(command and needs_processing(row, command))


def _lead_status(row: SheetRow, sheet: dict[str, Any]) -> str:
    explicit = str(sheet.get("lead_status") or "").strip()
    if explicit:
        return explicit  # the backend sends the board column (lead_stage.sheet_lead_status)
    ex = explicit.lower()
    review = str(sheet.get("needs_review") or "").strip().lower()
    outcome = row.get("outcome_status").lower()
    if outcome == "booked" or ex == "booked":
        return "Booked"
    if outcome in {"do not contact applied", "do not contact", "do not outreach"} or ex in {
            "do not contact", "do_not_contact", "do not outreach"}:
        return "Do not outreach"
    if review == "needs review" or ex == "needs review":
        return "Needs Review"
    if ex in {"in cadence", "in_progress", "active"}:
        return "In cadence"
    return explicit or row.get("lead_status")


# --- narrow polls and the nightly archive -----------------------------------------------------


def needs_attention(row: SheetRow, reruns: set[int] | None = None) -> bool:
    """From the scanned columns alone: a command to run, or one left in progress."""
    command = command_of(row)
    if command and (needs_processing(row, command, reruns) or _hint_due(row)):
        return True
    if _hint_stale(row):
        return True
    status = row.get("outcome_status")
    return status.casefold().startswith("processing: ") or bool(RETRYING.match(status))


# Rows moved per night: keeps the single Sheets request small; the rest go the next night.
ARCHIVE_MAX_ROWS = 2000
ARCHIVE_LEAD_STATUSES = ["booked", "do_not_contact", "declined", "closed_no_response",
                         "invalid_phone"]


def archive_candidates(grid: SheetGrid, days: int) -> list[SheetRow]:
    """Rows whose lead is finished (by the database, not the Sheet text) and quiet for `days`."""
    reruns = restart_reruns(grid.rows)
    rows = [r for r in grid.rows if _is_uuid(r.get("lead_id")) and not needs_attention(r, reruns)]
    if not rows:
        return []
    with transaction() as conn:
        done = {
            str(found["id"]) for found in conn.execute(
                "select id from leads where id=any(%s) and not coalesce(needs_review,false) "
                "and (status=any(%s) or cadence_state in ('completed','terminated')) "
                "and coalesce(status_changed_at,updated_at)<now()-make_interval(days=>%s)",
                ([r.get("lead_id") for r in rows], ARCHIVE_LEAD_STATUSES, days),
            )
        }
    return [r for r in rows if r.get("lead_id") in done]


def archive(backend: SheetBackend, grid: SheetGrid, days: int, tab: str,
            trace: WorkflowTrace) -> int:
    """Move finished rows to the archive tab. Rows that moved since the read are left alone."""
    rows = archive_candidates(grid, days)[:ARCHIVE_MAX_ROWS]
    if not rows:
        return 0
    fresh = backend.read_rows(sorted({grid.header_row, *(r.number for r in rows)}))
    header = fresh.get(grid.header_row) or []
    if any(as_text(header[i] if i < len(header) else "").strip().casefold()
           != COLUMNS[key].casefold() for key, i in grid.columns.items()):
        return 0  # a column moved since the read; try tomorrow
    lead_at = grid.columns["lead_id"]

    def lead_now(raw: list[Any]) -> str:
        return as_text(raw[lead_at]).strip() if lead_at < len(raw) else ""

    still = [r for r in rows if lead_now(fresh.get(r.number) or []) == r.get("lead_id")]
    if not still:
        return 0
    backend.archive_rows([r.number for r in still], grid.header_row, tab)
    with transaction() as conn, conn.cursor() as cursor:
        cursor.executemany(
            "insert into sheet_row_state(lead_id,practice_id,profile_hash,row_number,archived_at) "
            "select id,practice_id,%s,null,now() from leads where id=%s "
            "on conflict(lead_id) do update set archived_at=now(),row_number=null,updated_at=now()",
            [(profile_hash(r), r.get("lead_id")) for r in still],
        )
    trace.log("sheet_rows_archived", count=len(still))
    return len(still)


def archived(lead_ids: list[str]) -> set[str]:
    if not lead_ids:
        return set()
    with transaction() as conn:
        return {str(r["lead_id"]) for r in conn.execute(
            "select lead_id from sheet_row_state where lead_id=any(%s) and archived_at is not null",
            (lead_ids,),
        )}


# --- dry run --------------------------------------------------------------------------------


def preview(grid: SheetGrid, stuck_seconds: int) -> dict[str, Any]:
    """What the next poll would do, without doing it. Row numbers and command names only."""
    pending, invalid, stuck = [], [], []
    now = _now()
    reruns = restart_reruns(grid.rows)
    for row in grid.rows:
        command = command_of(row)
        if command and needs_processing(row, command, reruns):
            _, error = build_action(row, command[1])
            (invalid if error else pending).append({"row": row.number, "action": command[0],
                                                    **({"error": error} if error else {})})
        status = row.get("outcome_status")
        if status.casefold().startswith("processing: ") or RETRYING.match(status):
            started = parse_started_at(row.get("action_started_at"))
            due = bool(started) and now - started >= timedelta(seconds=stuck_seconds)
            stuck.append({"row": row.number, "status": status.split(":")[0], "due": due})
    linked = [r for r in grid.rows if r.get("lead_id")]
    return {
        "rows": len(grid.rows),
        "linked_rows": len(linked),
        "duplicate_lead_ids": len(linked) - len({r.get("lead_id") for r in linked}),
        "commands_to_run": pending,
        "commands_invalid": invalid,
        "stuck": stuck,
        "columns_missing": [k for k in ("lead_status", "cadence_day", "call_outcome",
                                        "message_outcome", "transcript_link",
                                        "action_started_at") if k not in grid.columns],
    }
