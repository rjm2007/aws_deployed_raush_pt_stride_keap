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

The Action column is written only by staff (owner, 2026-10-23). A command is new when it differs
from the one the row's Action Request ID names (`spent_rows`); staff run the same command again by
clearing Outcome Status.
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

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from ..db import transaction
from ..observability import WorkflowTrace
from .google_sheet import COLUMNS, SheetBackend, SheetGrid, SheetRow, as_text
from .lead_actions import (
    ActionExecution,
    _replay_stored_response,
    _request_hash,
    _same_name,
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
# Only staff write the Action column (owner, 2026-10-23). When the backend writes a new Outcome Status
# next to a command that row already handled (`spent_rows`), the text says the command is old and how
# to run it again: a blank Outcome Status always runs the command in Action.
OLD_COMMAND = "; {label} in Action already ran (clear this cell to run it again)"
# A command its Outcome Status turned down (`refused`) used to be ignored without a word (tester
# 2026-10-23): the row now says so. RERUN only where running it again makes sense (`_rerun_ok`).
NOT_RUN_TYPED = "; {label} in Action not run"
RERUN = " (clear this cell to run it)"
_NOT_RUN_TYPED = re.compile(r"; [^;]+ in action not run( \(clear this cell to run it\))?$", re.IGNORECASE)
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


def _ran_actions(rows) -> dict[int, tuple[str, bool]]:
    """Row -> (action, it ran) of the request its Action Request ID names (what this row last handled).

    "It ran": the request completed. A refused or dropped command (`_record_handled`) did not.
    """
    ids = {row.number: row.get("action_request_id") for row in rows}
    wanted = [rid for rid in ids.values() if _is_uuid(rid)]
    if not wanted:
        return {}
    with transaction() as conn:
        actions = {r["request_id"]: (r["action"], r["status"] == "completed") for r in conn.execute(
            # Compared as text: the live databases store request_id as text, the migrations as
            # uuid; a uuid comparison failed every Sheet poll on the demo (2026-10-21).
            "select request_id::text as request_id,action,status from lead_action_requests "
            "where request_id::text=any(%s::text[])", (wanted,))}
    return {number: actions[rid] for number, rid in ids.items() if rid in actions}


class Spent(set):
    """`spent_rows`: the row numbers, plus `ran` (those whose old command really ran)."""

    def __init__(self, numbers=(), ran=()):
        super().__init__(numbers)
        self.ran = set(ran)


def spent_rows(rows) -> Spent:
    """Rows whose Action is a command this row has already handled: an old command, never run again.

    Owner decision 2026-10-23: only staff write the Action column. The backend used to overwrite an
    old command ("Start cadence" after a dashboard unblock or a resolved review, "Booked" once booked,
    "" after a Stride cancel) so it could not run again next to a new Outcome Status. Instead, every
    command a row runs, or is refused (`refused`), or loses to a dashboard decision (`_drop_parked`),
    leaves a request in `lead_action_requests` whose id is the row's Action Request ID. A command
    equal to that request's action is the old one. Staff run the same command again by clearing the
    Outcome Status cell (a blank Outcome Status always runs, as it did under n8n).
    One lookup per poll, only for rows holding a command.
    """
    candidates = [row for row in rows if command_of(row) and _is_uuid(row.get("action_request_id"))]
    if not candidates:
        return Spent()
    found = _ran_actions(candidates)
    same = [row.number for row in candidates
            if (found.get(row.number) or ("", False))[0] == command_of(row)[1]]
    return Spent(same, [number for number in same if found[number][1]])


def _rerun_ok(status: str, action: str) -> bool:
    """May the row invite staff to clear Outcome Status and run the command in Action?

    Not on a closed lead ("Closed: ...", "Phone changed: add a new row"): tester 2026-10-23, clearing
    "Closed: a newer row has this phone number" restarted the old lead and took the number back.
    Not for a Restart on a row in review: it would wait anyway (`_review_waiting`).
    """
    folded = status.strip().casefold()
    if folded.startswith(("closed", "phone changed")):
        return False
    return not (action == "restart_cadence" and open_review_text(status))


def parked(row: SheetRow, spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """Restart cadence on a row in review: staff typed it, and it waits until the review is resolved.

    Not an old Restart (`spent_rows`): a Restart that already ran stays in the cell when a review is
    flagged later, and must not run again once the review is resolved (tester 2026-10-22).
    """
    return ((command_of(row) or ("", ""))[1] == "restart_cadence"
            and open_review_text(row.get("outcome_status")) and row.number not in spent)


def _hinted(row: SheetRow) -> bool:
    return row.get("outcome_status").casefold().endswith(REVIEW_FIRST.casefold())


def _hint_due(row: SheetRow, spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """A parked Restart whose row does not say yet that it waits for the review."""
    return parked(row, spent) and not _hinted(row)


def _hint_stale(row: SheetRow, spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """The row still says a Restart waits, but staff took it out of Action."""
    return _hinted(row) and not parked(row, spent)


def _with_hint(status: str, is_parked: bool) -> str:
    """A review text written over a parked Restart keeps saying the Restart waits."""
    if is_parked and open_review_text(status) and not status.casefold().endswith(
            REVIEW_FIRST.casefold()):
        return status + REVIEW_FIRST
    return status


def _with_note(status: str, row: SheetRow, spent: set[int] | frozenset[int]) -> str:
    """A new system text next to an old command says it will not run again, and how to run it.

    E.g. "Unblocked in dashboard" next to the Do Not Contact that blocked the number. Not on a final
    text, nor on the old command's own result ("Cadence restarted" next to a Restart); Start cadence
    on a linked row is never a command, so it gets no note either.
    """
    command = command_of(row)
    if (not status or not command or row.number not in getattr(spent, "ran", spent)
            or command[1] == "start_cadence" or not _rerun_ok(status, command[1])):
        return status
    folded = status.strip().casefold()
    if folded in FINAL_STATUSES or (command[1] == "restart_cadence"
                                    and folded.startswith("cadence restarted")):
        return status
    note = OLD_COMMAND.format(label=command[0])
    return status if status.endswith(note) else status + note


def needs_processing(row: SheetRow, command: tuple[str, str],
                     spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """The n8n "Action Needs Processing?" rules (live 2026-10-13), plus these fixes.

    Fix: a Start cadence row that failed before it got a Lead ID can be re-run by clearing
    Outcome Status (n8n also demanded clearing Action Request ID, so staff got stuck);
    `_start_row_already_done` makes that re-run safe.
    Review (2026-10-22): Booked and Do Not Contact run on a row in review (REVIEW_ENDING); Start and
    Restart do not (a Restart is `parked` until the review is resolved).
    Old commands (2026-10-23): a command the row already handled (`spent`, see `spent_rows`) never
    runs again next to a non-blank Outcome Status. This also covers a Restart on a finished cadence
    (owner 2026-10-20): it runs unless the cadence that ended was started by this same Restart.
    """
    _, action = command
    status = _base_status(row.get("outcome_status")).casefold()
    if action == "start_cadence" and (row.get("lead_id") or (status and row.get("action_request_id"))):
        return False
    if not status:
        return True
    if status.startswith(("processing:", "retrying ", "error:")):
        return False
    if row.number in spent:
        return False
    if " in dashboard" in status:
        return action != "do_not_contact"
    if "needs review" in status:
        return action in REVIEW_ENDING and open_review_text(status)
    if status in FINAL_STATUSES:
        return False
    if action == "start_cadence" and status in {"cadence started", "already in cadence",
                                                "cadence completed"}:
        return False
    return not (action == "restart_cadence" and status.startswith("cadence restarted"))


def _base_status(status: str) -> str:
    """Outcome Status without the "<command> in Action not run" words a refusal added."""
    return _NOT_RUN_TYPED.sub("", status)


def _refusal_text(status: str, command: tuple[str, str]) -> str:
    label, action = command
    return status + NOT_RUN_TYPED.format(label=label) + (RERUN if _rerun_ok(status, action) else "")


def _review_waiting(lead_id: str) -> str | None:
    """The lead's open review as the Sheet words it ("Needs review: ..."), or None.

    Read from the database, not the row: staff who clear Outcome Status on a row in review must not
    start a Restart past the review (tester 2026-10-23: the Restart ran at once and closed the
    review as "restarted" by Google Sheet, accepting a new name nobody chose).
    """
    from .lead_review import sheet_open_status  # local: lead_review imports lead_actions

    with transaction() as conn:
        found = conn.execute(
            "select l.needs_review,r.code,r.details from leads l left join lateral ("
            "select code,details from lead_reviews where lead_id=l.id and resolved_at is null "
            "order by flagged_at,id limit 1) r on true where l.id::text=%s", (lead_id,)).fetchone()
    if not found or not (found["needs_review"] or found["code"]):
        return None
    return sheet_open_status(found["code"], found["details"]) if found["code"] else "Needs review"


def refused(row: SheetRow, command: tuple[str, str],
            spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """A command the row's Outcome Status turns down, not handled yet: it is recorded, never run later.

    E.g. Restart cadence next to "Booked", Do Not Contact next to "Unblocked in dashboard", or the
    Booked / Restart the backend itself wrote into Action before 2026-10-23. Left unrecorded, such a
    command would run the moment a later system text (a review, "Cadence completed") replaced the
    one that held it back. Not: Start cadence (never a command on a linked row), a parked Restart
    (it waits), a row without a Lead ID, a blank, in-progress or error status (left for staff).
    """
    _, action = command
    status = row.get("outcome_status")
    if (action == "start_cadence" or not _is_uuid(row.get("lead_id")) or row.number in spent
            or not status or _in_progress(status) or status.casefold().startswith("error:")):
        return False
    return not parked(row, spent) and not needs_processing(row, command, spent)


def _record_handled(lead_id: str, action: str, code: str) -> str | None:
    """Store a command that did not run (refused, or dropped for a dashboard decision); its id.

    Status 'failed' with no completed_at: the snapshot's "last Sheet request" queries read only
    completed requests, and a resolution's Outcome Status is kept unless a request completed later.
    None when the lead does not exist (nothing is written to the row then).
    """
    request_id = str(uuid4())
    body = {"http_status": 409, "body": {"code": code, "lead_id": lead_id, "action": action}}
    with transaction() as conn:
        stored = conn.execute(
            "insert into lead_action_requests(request_id,practice_id,action,request_hash,lead_id,"
            "status,response_body) select %s,l.practice_id,%s,%s,l.id,'failed',%s from leads l "
            "where l.id::text=%s returning request_id",
            (request_id, action, hashlib.sha256(f"{code}:{request_id}".encode()).hexdigest(),
             Jsonb(body), lead_id),
        ).fetchone()
    return request_id if stored else None


def _mark_handled(grid: SheetGrid, row: SheetRow, spent: set[int], code: str) -> bool:
    """The row's command is handled without running: its Action Request ID names that record."""
    command = command_of(row)
    if not command or not _is_uuid(row.get("lead_id")):
        return False
    request_id = _record_handled(row.get("lead_id"), command[1], code)
    if request_id is None:
        return False
    grid.set(row, "action_request_id", request_id)
    spent.add(row.number)
    return True


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

    # Names written back after "keep old name": lead id -> (fingerprint before, after, row, Name).
    renamed: dict[str, tuple[str, str, int, str]] = field(default_factory=dict)

    def save_baselines(self, deferred_rows: set[int]) -> None:
        """`deferred_rows`: rows not written (moved), or whose guarded Name write was skipped."""
        _save_baseline({lead_id: item for lead_id, item in self.baselines.items()
                        if item[1] not in deferred_rows})
        _save_renamed({lead_id: item for lead_id, item in self.renamed.items()
                       if item[2] not in deferred_rows})


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
    spent = spent_rows(grid.rows)
    for row in grid.rows:
        if _hint_stale(row, spent):
            grid.set(row, "outcome_status", row.get("outcome_status")[:-len(REVIEW_FIRST)])
        command = command_of(row)
        if not command:
            continue
        if refused(row, command, spent):
            # Turned down by its Outcome Status (as before), and now recorded as handled, so a later
            # system text cannot set it off (only staff write Action since 2026-10-23). The row says so.
            if _mark_handled(grid, row, spent, "refused_by_outcome_status"):
                grid.set(row, "outcome_status", _refusal_text(row.get("outcome_status"), command))
                trace.log("sheet_command_refused", row=row.number, action=command[1])
            continue
        hint = _hint_due(row, spent)
        if not hint and not needs_processing(row, command, spent):
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
        if action == "restart_cadence" and _is_uuid(row.get("lead_id")) and (
                review := _review_waiting(row.get("lead_id"))):
            # Outcome Status cleared on a row in review: the Restart still waits for the review
            # (Booked / Do Not Contact do run). An old Restart staff cleared the cell for becomes a
            # waiting one: its Action Request ID is emptied, so it is no longer taken as spent.
            grid.set(row, "outcome_status", review + REVIEW_FIRST)
            if row.number in spent:
                grid.set(row, "action_request_id", "")
                spent.discard(row.number)
                getattr(spent, "ran", set()).discard(row.number)
            trace.log("sheet_command_waits_for_review", row=row.number, action=action)
            continue
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
    # A Restart that ended in a review ("Number blocked; needs review") has run: its Action Request
    # ID names it, so it is not taken for a Restart staff typed (`parked`). Action is left as is.
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
                spent = spent_rows([row])
                grid.set(row, "lead_status", "Needs Review")
                grid.set(row, "outcome_status", _with_hint(_with_note(
                    REVIEW_LABELS[code], row, spent), parked(row, spent)))
            elif code in PHONE_CHANGED_LABELS:
                outcome_status, lead_status = PHONE_CHANGED_LABELS[code]
                spent = spent_rows([row])
                grid.set(row, "lead_status", lead_status)
                if not _command_pending(row, spent):  # a command staff just typed runs (and is refused)
                    # The lead is closed: a Restart waiting for a review goes, and the row says so.
                    if parked(row, spent) and _mark_handled(grid, row, spent, "dropped_phone_changed"):
                        grid.set(row, "outcome_status", outcome_status + NOT_RUN)
                    else:
                        grid.set(row, "outcome_status", _with_note(outcome_status, row, spent))
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


def _save_renamed(items: dict[str, tuple[str, str, int, str]]) -> None:
    """The fingerprint after a Name write-back, if the stored one is the row's from before it.

    Otherwise staff changed something else in the row since the last sync (an Email, say): the
    stored fingerprint stays, and the next full read syncs the row with the kept name, so no
    review is flagged and their edit is not lost.
    """
    if not items:
        return
    with transaction() as conn, conn.cursor() as cursor:
        cursor.executemany(
            "update sheet_row_state set profile_hash=%s,row_number=%s,sheet_name=%s,seen_at=now(),"
            "updated_at=now() where lead_id=%s and profile_hash=%s",
            [(after, number, name or None, lead_id, before)
             for lead_id, (before, after, number, name) in items.items()],
        )
        # The row shows the name on file again, so the kept Sheet name is spent (as a profile sync
        # of that name would do, lead_actions.sync_sheet_lead). Tester 2026-10-23: left set, the
        # same new name retyped later was accepted silently and never opened a review.
        cursor.executemany("update leads set sheet_name_accepted=null where id=%s",
                           [(lead_id,) for lead_id in items])


# --- results from the backend (n8n webhook-flow) ---------------------------------------------

ALWAYS_WRITTEN = ("cadence_day", "call_outcome", "message_outcome", "email_outcome",
                  "transcript_link")


def apply_snapshot(grid: SheetGrid, snapshot: dict[str, Any],
                   result: PollResult | None = None) -> bool:
    """Write one lead's snapshot into its row (first row with that Lead ID). False: not in Sheet.

    Never the Action cell (owner, 2026-10-23: only staff write it). The snapshot's `action` /
    `review_action` are for n8n mode only (sheet_sync); an old command left in Action is kept from
    running again by `spent_rows`, not by overwriting it.
    """
    lead_id = str(snapshot["lead_id"])
    row = next((r for r in grid.rows if r.get("lead_id") == lead_id), None)
    if row is None:
        return False
    sheet = snapshot.get("sheet") or {}
    if sheet.get("name"):
        _restore_name(grid, row, sheet, result)
    # What staff typed is judged on the row as it is now, before this snapshot writes into it.
    spent = spent_rows([row])
    command = command_of(row)
    if command and refused(row, command, spent) and _mark_handled(
            grid, row, spent, "refused_by_outcome_status"):
        grid.set(row, "outcome_status", _refusal_text(row.get("outcome_status"), command))
    pending = _command_pending(row, spent)
    waits = not pending and parked(row, spent)
    for key in ALWAYS_WRITTEN:
        if key in sheet:
            grid.set(row, key, sheet[key] or "")
    if waits and sheet.get("lead_status") in ENDED_LEAD_STATUSES:
        _drop_parked(grid, row, sheet, spent)
        grid.set(row, "lead_status", _lead_status(row, sheet))
        return True
    if "action_status" in sheet:
        grid.set(row, "outcome_status",
                 _with_hint(_with_note(sheet["action_status"] or "", row, spent), waits))
    # Review flagged / resolved (services/lead_review.py). Never over a command staff typed and the
    # Sheet worker has not run yet (still settling, or waiting) or one still running: QA 2026-10-22,
    # a new "Needs review" text would block a Booked typed seconds earlier. The command's own result
    # is written when it runs.
    if sheet.get("review_status") and not pending:
        text = sheet["review_status"]
        grid.set(row, "outcome_status", _with_hint(_with_note(text, row, spent), waits))
        # A Restart parked behind the review now runs (the Resolved text is not final), unless the
        # resolution itself restarted the cadence: then it is done, and must not run on top later.
        if waits and not open_review_text(text) and text.strip().casefold().startswith(
                "cadence restarted"):
            _mark_handled(grid, row, spent, "done_by_review_restart")
    grid.set(row, "lead_status", _lead_status(row, sheet))
    return True


def _restore_name(grid: SheetGrid, row: SheetRow, sheet: dict[str, Any],
                  result: PollResult | None) -> None:
    """"Same person, keep old name": the Name cell goes back to the name on file.

    Owner, demo test 2026-10-23: calls resumed under the old name but the row kept showing the new
    one. Only over the exact name the review was about (`name_replaces`): a third name staff typed
    since is theirs, and the normal name-change flow handles it. The pre-write identity check is
    unaffected: the row has a Lead ID, which decides alone (`_same_row`), and the cell itself is
    re-checked just before the write (`expect`). The new fingerprint is stored with the write, so
    the profile sync sees the old name as in sync (no new review, no sync loop). Only when the
    row was otherwise in sync (`_save_renamed`): another edit staff made meanwhile still syncs.
    The kept-name marker (`leads.sheet_name_accepted`) is cleared with it, so the same new name
    typed again opens a new review.
    """
    shown = row.get("name")
    if _same_name(shown, sheet["name"]) or not _same_name(shown, sheet.get("name_replaces")):
        return
    before = profile_hash(row)
    grid.set(row, "name", sheet["name"], expect=shown)
    if result is not None:
        result.renamed[str(row.get("lead_id"))] = (before, profile_hash(row), row.number,
                                                   _sheet_name(row))


def _drop_parked(grid: SheetGrid, row: SheetRow, sheet: dict[str, Any], spent: set[int]) -> None:
    """The lead ended on the dashboard (or by Stride) while a Restart waited: the Restart goes.

    Recorded as handled (`_mark_handled`), so the Restart left in Action never runs later (tester
    2026-10-22: "Front desk will handle", then the Restart ran two minutes later).
    """
    text = sheet.get("review_status") or sheet.get("action_status") or sheet.get("lead_status") or ""
    if text.strip().casefold() not in FINAL_STATUSES:  # "Booked" stays exactly that (final)
        text += NOT_RUN
    _mark_handled(grid, row, spent, "dropped_lead_ended")
    grid.set(row, "outcome_status", text)


def _in_progress(status: str) -> bool:
    return status.casefold().startswith("processing: ") or bool(RETRYING.match(status))


def _command_pending(row: SheetRow, spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """A command in the row that has not finished: typed (waiting or settling) or running."""
    command = command_of(row)
    return _in_progress(row.get("outcome_status")) or bool(
        command and needs_processing(row, command, spent))


def _lead_status(row: SheetRow, sheet: dict[str, Any]) -> str:
    explicit = str(sheet.get("lead_status") or "").strip()
    if explicit:
        return explicit  # the backend sends the board column (lead_stage.sheet_lead_status)
    ex = explicit.lower()
    review = str(sheet.get("needs_review") or "").strip().lower()
    outcome = _base_status(row.get("outcome_status")).lower()
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


def needs_attention(row: SheetRow, spent: set[int] | frozenset[int] = frozenset()) -> bool:
    """From the scanned columns alone: a command to run (or to record as refused), or one in progress.

    `spent`: `spent_rows` of the scanned rows.
    """
    command = command_of(row)
    if command and (needs_processing(row, command, spent) or _hint_due(row, spent)
                    or refused(row, command, spent)):
        return True
    if _hint_stale(row, spent):
        return True
    status = row.get("outcome_status")
    return status.casefold().startswith("processing: ") or bool(RETRYING.match(status))


# Rows moved per night: keeps the single Sheets request small; the rest go the next night.
ARCHIVE_MAX_ROWS = 2000
ARCHIVE_LEAD_STATUSES = ["booked", "do_not_contact", "declined", "closed_no_response",
                         "invalid_phone"]


def archive_candidates(grid: SheetGrid, days: int) -> list[SheetRow]:
    """Rows whose lead is finished (by the database, not the Sheet text) and quiet for `days`."""
    spent = spent_rows(grid.rows)
    rows = [r for r in grid.rows if _is_uuid(r.get("lead_id")) and not needs_attention(r, spent)]
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
    spent = spent_rows(grid.rows)
    for row in grid.rows:
        command = command_of(row)
        if command and needs_processing(row, command, spent):
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
