"""Google Sheet <-> backend, run by the sheet-worker itself (replaces the four n8n workflows).

One poll = one read of the whole tab, then at most two batched writes:
1. Commands (n8n "lead intake"): a row whose Action is one of the four commands and that is not
   running already is claimed ("Processing: X" + a new Action Request ID), executed through the
   same `execute_lead_action` the n8n endpoint used, labelled, and its Action emptied.
2. Stuck commands (n8n "recovery"): a row left at "Processing: X" / "Retrying n/3: X" for
   SHEET_STUCK_SECONDS is re-run with its existing Action Request ID, so a command the backend
   already committed is replayed, never applied twice.
3. Profile edits (n8n "lead-sync"): a linked row whose Name / Phone / Email / Date Of Birth /
   Location / Case fingerprint changed since the last poll is sent to `sync_sheet_lead`.
4. Results (n8n "webhook-flow"): outbox snapshots are written into the lead's row, found by
   Lead ID.

Labels and the phone / date-of-birth rules are the n8n ones (spec: context/PROJECT_CONTEXT.md,
2026-10-14 entry), so staff see the same Sheet. Our own writes never trigger anything: we only
ever empty Action, and the profile fingerprint ignores the columns we write.

The Action column (owner, 2026-10-25): staff pick a command, it runs, the cell empties. Once a
command is handled (it ran, the backend refused it, or the row could not run it) the worker empties
Action and the Action Request ID in the same write as the result (`_clear_action`); the result stays
in Outcome Status. So a command in Action is always one staff picked and not handled yet, and staff
run the same command again by picking it again. The worker never writes anything else into Action.
Any other text in Action is answered UNKNOWN_COMMAND and emptied the same way (owner, 2026-10-26).
Rows from before 2026-10-25 can still hold a handled command; `old_commands` finds them and they are
emptied without running.

Restart guardrails (owner, 2026-10-25, checked by the backend under the lead lock, `sheet_rules`):
a Restart on a lead in review is refused at once (no more waiting in Action: the review card offers
the restart), and so is one on a booked lead or a lead closed by a decision (front desk, a newer row,
not interested). Booked and Do Not Contact still run on a lead in review. A Restart that waited
behind a review under the old rules is emptied with the refusal text, never run (`_legacy_waiting`).
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
    _same_name,
    execute_lead_action,
    sync_sheet_lead,
)
from .sheet_sync import dashboard_call_link

SHEET_TZ = ZoneInfo("America/Los_Angeles")

# Sheet Action text -> (label, backend action). Exact, as the Sheet's drop-down has it (only extra spaces
# are forgiven, as n8n did): "booked" or "restart cadence" is not a command (owner, 2026-10-26: Action must
# be chosen from the drop-down). Any other text is UNKNOWN_COMMAND (`claim_commands`).
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
    # Owner, 2026-10-25: reviewed like a name (lead_actions.sync_sheet_lead, `sheet_rules`).
    "location_changed_needs_review": "Needs review: location changed",
    "case_changed_needs_review": "Needs review: case changed",
    "dob_changed_needs_review": "Needs review: date of birth changed",
}
# A changed phone number closes the lead at once (owner, 2026-10-22): Outcome Status, Lead Status.
PHONE_CHANGED_LABELS = {"phone_changed_closed": ("Phone changed: add a new row", "Closed")}
FINAL_STATUSES = {"booked", "already booked", "do not contact applied", "completed",
                  "lead already exists"}
# On a row in review, Booked and Do Not Contact still run: they end the lead whatever the review was
# about, and the backend closes the review as "booked" / "do_not_contact" (as the dashboard's Booked
# move and Do not contact switch do). QA 2026-10-22: a Booked typed on a "Needs review: wrong number"
# row was ignored without a word. A Restart is refused (below); a Start on a linked row is too.
REVIEW_ENDING = {"booked", "do_not_contact"}
# The 2026-10-22..25 rules kept a Restart picked on a row in review waiting in Action, with these words
# after the review text, and ran it once the review was resolved. Gone (owner, 2026-10-25); a row
# still showing them is emptied with REVIEW_REFUSED, never run (`_legacy_waiting`).
LEGACY_WAIT_HINT = "; resolve it on the dashboard first, then the restart runs"
# Refusals staff can act on (owner, 2026-10-25): the row says what to do, and Action is empty again,
# so picking the command again is the retry.
OUTREACH_BUSY = "Error: a call or text is going out right now. Pick Restart cadence again in a minute."
TOO_SOON = "Error: restarted less than {minutes} minutes ago. Pick Restart cadence again after {time}."
TOO_SOON_NO_TIME = ("Error: restarted less than {minutes} minutes ago. Pick Restart cadence again "
                    "in {minutes} minutes.")
# Restart guardrails (owner, 2026-10-25; lead_actions._sheet_restart_refusal): the Sheet never undoes
# a dashboard decision, and a lead in review is restarted from its review card.
REVIEW_REFUSED = ("Error: this lead needs review. Resolve it on the dashboard; you can restart it "
                  "from there.")
BOOKED_REFUSED = "Error: this lead is booked. Reopen it on the dashboard if needed."
CLOSED_REFUSED = "Error: this lead is closed ({why}). Use the dashboard."
# Start cadence on a row that already has a Lead ID would create nothing (the phone is taken): it was
# ignored without a word; now Action empties, so the row says why.
ALREADY_LINKED = "Error: this row already has a lead. Pick Restart cadence to start the calls again."
# Text in Action that is not a drop-down value ("please call", "restart", "booked") sat there without a
# word (owner, 2026-10-26): once settled, the row says so and Action empties. Nothing runs.
UNKNOWN_COMMAND = "Error: action must be chosen from the drop-down."
# Words the 2026-10-23 rules added to Outcome Status ("clear this cell ..."). They no longer apply:
# taken off when the old command next to them is emptied.
_OLD_NOTE = re.compile(r"; [^;]+ in action (already ran \(clear this cell to run it again\)"
                       r"|not run( \(clear this cell to run it\))?)$", re.IGNORECASE)
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


def unknown_command(row: SheetRow) -> bool:
    """Text in Action that is not a drop-down command, on a row not running one (that is recovery's)."""
    return bool(row.get("action").strip()) and not command_of(row) \
        and not _in_progress(row.get("outcome_status"))


def open_review_text(status: str) -> bool:
    """An Outcome Status this backend writes for an open review (lead_review.sheet_open_status).

    Older n8n texts ("Phone number changed; needs review") are left out on purpose: those rows
    can still hold the stale Booked that n8n re-sent (2026-10-13), so they keep blocking everything.
    """
    text = status.strip().casefold()
    return text.startswith(("needs review", "number blocked; needs review"))


def _handled_actions(rows) -> dict[int, str]:
    """Row -> action of the finished request its Action Request ID names (what the row last handled).

    Finished: completed, or failed (refused by the backend, or recorded as handled by the 2026-10-23
    rules). A request still processing is not finished.
    """
    ids = {row.number: row.get("action_request_id") for row in rows}
    wanted = [rid for rid in ids.values() if _is_uuid(rid)]
    if not wanted:
        return {}
    with transaction() as conn:
        actions = {r["request_id"]: r["action"] for r in conn.execute(
            # Compared as text: the live databases store request_id as text, the migrations as
            # uuid; a uuid comparison failed every Sheet poll on the demo (2026-10-21).
            "select request_id::text as request_id,action from lead_action_requests "
            "where request_id::text=any(%s::text[]) and status in ('completed','failed')", (wanted,))}
    return {number: actions[rid] for number, rid in ids.items() if rid in actions}


def _old_rules_run(row: SheetRow, command: tuple[str, str]) -> bool:
    """Would the rules before 2026-10-25 run this command? (the n8n "Action Needs Processing?" rules)

    Used only for a command that sat in Action before Action was emptied after each command
    (`old_commands`): then a command stayed in the cell, and only Outcome Status told whether it was
    done. E.g. the "Booked" the backend wrote next to "Booked", the "Restart cadence" next to
    "Cadence restarted", the "Do Not Contact" next to "Unblocked in dashboard", "Start cadence" on a
    linked row, any command next to "Error: ...".
    """
    _, action = command
    status = _base_status(row.get("outcome_status")).casefold()
    if action == "start_cadence" and (row.get("lead_id") or (status and row.get("action_request_id"))):
        return False
    if not status:
        return True
    if status.startswith(("processing:", "retrying ", "error:")):
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


def old_commands(rows) -> set[int]:
    """Rows whose Action still holds a command handled before 2026-10-25: emptied, never run.

    Since 2026-10-25 the Action Request ID is emptied together with Action once a command is handled
    (`_clear_action`), so a command next to a filled Action Request ID is either running (Outcome
    Status "Processing: ..." / "Retrying ...": left alone) or from before. From before it is old when
    the request that id names finished with this same action (it ran, or the 2026-10-23 rules
    recorded it as turned down) and Outcome Status is not blank, or when the old rules would not run
    it (`_old_rules_run`; a Restart that waited behind a review is one of these since 2026-10-25).
    Anything else is a command staff picked under the old rules (a cleared Outcome Status, a
    different command) and runs.
    One lookup per poll, only for rows holding a command and an Action Request ID.
    """
    candidates = [row for row in rows if command_of(row) and row.get("action_request_id")
                  and not _in_progress(row.get("outcome_status"))]
    if not candidates:
        return set()
    found = _handled_actions(candidates)
    old = set()
    for row in candidates:
        command = command_of(row)
        # A blank Outcome Status ran the command under the old rules even if it was the last one
        # (how staff re-ran a command then): `_old_rules_run` says so, and it still runs.
        ran = found.get(row.number) == command[1] and bool(row.get("outcome_status"))
        if ran or not _old_rules_run(row, command):
            old.add(row.number)
    return old


def _clear_action(grid: SheetGrid, row: SheetRow) -> None:
    """The command in Action is handled: empty the cell, and the Action Request ID with it.

    Owner, 2026-10-25: staff pick a command, it runs, the cell empties, so the same command can be
    picked again. Only over the text read (`expect`): a command staff picked meanwhile is theirs and
    is kept, and runs as a new one (the Action Request ID is empty either way).
    """
    held = row.cells.get("action", "")
    if held.strip():
        grid.set(row, "action", "", expect=held)
    grid.set(row, "action_request_id", "")


def retire_old(grid: SheetGrid, rows, trace: WorkflowTrace | None = None) -> None:
    """Empty the old commands among `rows` (`old_commands`), and the 2026-10-23 "clear this cell" words.

    An old Restart next to an open review text waited behind the review under the old rules: it is
    refused now (owner, 2026-10-25), and the row says so, as for one picked today.
    """
    old = old_commands(rows)
    for row in rows:
        if row.number not in old:
            continue
        action = command_of(row)[1]
        status = row.get("outcome_status")
        if action == "restart_cadence" and open_review_text(_base_status(status)):
            grid.set(row, "outcome_status", REVIEW_REFUSED)
            note_refusal(row, "Restart cadence", REVIEW_REFUSED)
        elif _OLD_NOTE.search(status):
            grid.set(row, "outcome_status", _base_status(status))
        _clear_action(grid, row)
        if trace is not None:
            trace.log("sheet_old_command_cleared", row=row.number, action=action)


def _hinted(row: SheetRow) -> bool:
    return row.get("outcome_status").casefold().endswith(LEGACY_WAIT_HINT.casefold())


def _legacy_waiting(row: SheetRow) -> bool:
    """A Restart that waited behind a review under the 2026-10-22..25 rules (the row says it waits).

    One-time, on the first polls after the owner's 2026-10-25 change: it is emptied with the refusal
    text and never run, even if the review was resolved since (staff restart from the review card).
    """
    return (command_of(row) or ("", ""))[1] == "restart_cadence" and _hinted(row)


def _hint_stale(row: SheetRow) -> bool:
    """The row still says a Restart waits, but there is no Restart in Action: the words go."""
    return _hinted(row) and not _legacy_waiting(row)


def needs_processing(row: SheetRow, command: tuple[str, str]) -> bool:
    """Does the command in Action still need running? (Owner, 2026-10-25.)

    Action is emptied once a command is handled, so a command there is new, unless it is running
    now (Processing / Retrying: recovery's). Rows from before 2026-10-25 can hold a handled command:
    `old_commands` empties those first. What the backend refuses (a Restart on a lead in review,
    booked or closed) is still sent: the refusal is the row's answer, and Action empties.
    Booked and Do Not Contact run on a row in review (REVIEW_ENDING, 2026-10-22). Start cadence on
    a linked row is turned down with a reason (`claim_commands`).
    """
    del command
    return not _in_progress(row.get("outcome_status"))


def _base_status(status: str) -> str:
    """Outcome Status without the "clear this cell" words of the 2026-10-23 rules."""
    return _OLD_NOTE.sub("", status)


def note_refusal(row: SheetRow, label: str, text: str) -> None:
    """A command the Sheet worker refused itself (no backend request): said in the lead's Activity.

    Owner, 2026-10-25: every Sheet command shows in the lead's Activity, refusals with the reason.
    Commands the backend handled are read from lead_action_requests (routes/dashboard.py); these
    few never reach it (Start on a linked row, an invalid linked row, an old waiting Restart), so a
    status history line (source n8n_sheet, from = to) records them. Never fails the poll.
    """
    lead_id = row.get("lead_id")
    if not _is_uuid(lead_id):
        return
    reason = f"{label} refused: {text.removeprefix('Error: ')}"[:500]
    try:
        with transaction() as conn:
            conn.execute(
                "insert into lead_status_history(lead_id,from_status,to_status,source,reason) "
                "select id,status,status,'n8n_sheet',%s from leads where id::text=%s",
                (reason, lead_id),
            )
    except Exception:  # noqa: BLE001, S110 - the Activity line is a note; the row is handled anyway
        pass


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
    if text := _refusal_text(body):
        return Outcome(text, None, lead_id)
    detail = body.get("detail") if isinstance(body.get("detail"), str) else body.get("code")
    return Outcome("Error: " + str(detail or "request rejected")[:180], None, lead_id)


def _refusal_text(body: dict[str, Any]) -> str | None:
    """Sheet words for a refusal staff fix by picking the command again later (owner, 2026-10-25)."""
    from .lead_actions import RESTART_COOLDOWN_MINUTES

    code = body.get("code")
    if code == "outreach_unresolved":
        return OUTREACH_BUSY
    if code == "restart_needs_review":
        return REVIEW_REFUSED
    if code == "restart_booked":
        return BOOKED_REFUSED
    if code == "restart_closed":
        return CLOSED_REFUSED.format(why=body.get("closed_reason") or "closed on the dashboard")
    if code != "restart_too_soon":
        return None
    try:
        retry_at = datetime.fromisoformat(str(body.get("retry_at"))).astimezone(SHEET_TZ)
    except ValueError:  # a refusal stored before retry_at was sent
        return TOO_SOON_NO_TIME.format(minutes=RESTART_COOLDOWN_MINUTES)
    clock = retry_at.strftime("%I:%M %p").lstrip("0") + " PT"
    return TOO_SOON.format(minutes=RESTART_COOLDOWN_MINUTES, time=clock)


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

    # Cells written back after "keep old name" / "keep old <field>": lead id -> (fingerprint
    # before, after, row, Name), and which Sheet columns were written (RESTORABLE).
    renamed: dict[str, tuple[str, str, int, str]] = field(default_factory=dict)
    restored: dict[str, set[str]] = field(default_factory=dict)

    def save_baselines(self, deferred_rows: set[int]) -> None:
        """`deferred_rows`: rows not written (moved), or whose guarded write-back was skipped."""
        _save_baseline({lead_id: item for lead_id, item in self.baselines.items()
                        if item[1] not in deferred_rows})
        _save_renamed({lead_id: item for lead_id, item in self.renamed.items()
                       if item[2] not in deferred_rows}, self.restored)


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
    # Commands left in Action before 2026-10-25 that were already handled: emptied, never run.
    retire_old(grid, grid.rows, trace)
    for row in grid.rows:
        if _hint_stale(row):
            grid.set(row, "outcome_status", row.get("outcome_status")[:-len(LEGACY_WAIT_HINT)])
        command = command_of(row)
        unknown = unknown_command(row)
        if not unknown and (not command or not needs_processing(row, command)):
            continue
        waiting.add(row.number)
        if settle is not None and not settle.ready(row, now):
            continue
        if unknown:
            typed = " ".join(row.get("action").split())[:40]
            grid.set(row, "outcome_status", UNKNOWN_COMMAND)
            _clear_action(grid, row)
            note_refusal(row, f'"{typed}"', UNKNOWN_COMMAND)
            trace.log("sheet_command_unknown", row=row.number)
            continue
        if limit is not None and len(claims) >= limit:
            continue
        label, action = command
        if action == "start_cadence" and row.get("lead_id"):
            # Start cadence on a linked row was never a command (n8n rule). It used to stay in the
            # cell without a word; now Action empties, so the row says what to pick instead.
            grid.set(row, "outcome_status", ALREADY_LINKED)
            _clear_action(grid, row)
            note_refusal(row, label, ALREADY_LINKED)
            trace.log("sheet_command_refused", row=row.number, action=action)
            continue
        if _legacy_waiting(row):
            # A Restart left waiting behind a review by the old rules (owner, 2026-10-25): it is
            # refused like one picked today, and never run, even if the review was resolved since.
            grid.set(row, "outcome_status", REVIEW_REFUSED)
            _clear_action(grid, row)
            note_refusal(row, label, REVIEW_REFUSED)
            trace.log("sheet_command_refused", row=row.number, action=action)
            continue
        request, error = build_action(row, action)
        if error:
            grid.set(row, "outcome_status", "Error: " + error[:180])
            _clear_action(grid, row)
            note_refusal(row, label, error[:180])
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
            sheet_rules=True,
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
    # Handled (it ran, or was refused): Action empties (owner, 2026-10-25), also after a Restart that
    # ended in a review ("Number blocked; needs review").
    # "Retrying n/3" is still running: the command and its Action Request ID stay for recovery.
    if not outcome.status.startswith("Retrying"):
        _clear_action(grid, row)
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
                                         lead=lead.model_dump(mode="python"), sheet_rules=True)
            except Exception as exc:  # noqa: BLE001 - try again next poll
                trace.log("sheet_profile_failed", error_category=type(exc).__name__)
                continue
            code = (synced.body or {}).get("code")
            if code in REVIEW_LABELS:
                grid.set(row, "lead_status", "Needs Review")
                grid.set(row, "outcome_status", REVIEW_LABELS[code])
            elif code in PHONE_CHANGED_LABELS:
                outcome_status, lead_status = PHONE_CHANGED_LABELS[code]
                grid.set(row, "lead_status", lead_status)
                if not _command_pending(row):  # a command staff just typed runs (and is refused)
                    grid.set(row, "outcome_status", outcome_status)
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


def _save_renamed(items: dict[str, tuple[str, str, int, str]],
                  restored: dict[str, set[str]] | None = None) -> None:
    """The fingerprint after a Name (or kept value) write-back, if the stored one is the row's from
    before it.

    Otherwise staff changed something else in the row since the last sync (an Email, say): the
    stored fingerprint stays, and the next full read syncs the row with the kept name, so no
    review is flagged and their edit is not lost.
    """
    from .lead_actions import spend_kept_values

    if not items:
        return
    restored = restored or {}
    named = [lead_id for lead_id in items if "name" in restored.get(lead_id, {"name"})]
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
        if named:
            cursor.executemany("update leads set sheet_name_accepted=null where id=%s",
                               [(lead_id,) for lead_id in named])
        # The same for a kept location / case / date of birth (lead_actions._kept_value).
        for lead_id in items:
            codes = [code for code, column in VALUE_CODES.items()
                     if column in restored.get(lead_id, set())]
            if codes:
                spend_kept_values(conn, lead_id, codes)


# --- results from the backend (n8n webhook-flow) ---------------------------------------------

ALWAYS_WRITTEN = ("cadence_day", "call_outcome", "message_outcome", "email_outcome",
                  "transcript_link")


# Sheet column -> review code, for the cells "keep old <field>" writes back (sheet_sync._kept_values).
VALUE_CODES = {"location_changed": "location", "case_changed": "case", "dob_changed": "date_of_birth"}
# Cells a "keep old" choice writes back; a skipped one holds the row's fingerprint back.
RESTORABLE = frozenset({"name", *VALUE_CODES.values()})


def apply_snapshot(grid: SheetGrid, snapshot: dict[str, Any],
                   result: PollResult | None = None) -> bool:
    """Write one lead's snapshot into its row (first row with that Lead ID). False: not in Sheet.

    Action is never written here (owner, 2026-10-25): only a command run on the row empties it.
    The snapshot's `action` / `review_action` are for n8n mode only (sheet_sync).
    """
    lead_id = str(snapshot["lead_id"])
    row = next((r for r in grid.rows if r.get("lead_id") == lead_id), None)
    if row is None:
        return False
    sheet = snapshot.get("sheet") or {}
    before = profile_hash(row)
    written = [*(["name"] if sheet.get("name") and _restore_name(grid, row, sheet) else []),
               *_restore_values(grid, row, sheet.get("kept") or {})]
    if written and result is not None:
        result.renamed[lead_id] = (before, profile_hash(row), row.number, _sheet_name(row))
        result.restored[lead_id] = set(written)
    # What staff typed is judged on the row as it is now, before this snapshot writes into it. An
    # old command (before 2026-10-25) is emptied first, so it is not taken for a pending one.
    retire_old(grid, [row])
    pending = _command_pending(row)
    for key in ALWAYS_WRITTEN:
        if key in sheet:
            grid.set(row, key, sheet[key] or "")
    if "action_status" in sheet:
        grid.set(row, "outcome_status", sheet["action_status"] or "")
    # Review flagged / resolved (services/lead_review.py). Never over a command staff typed and the
    # Sheet worker has not run yet (still settling) or one still running: QA 2026-10-22, a new
    # "Needs review" text would hide a Booked typed seconds earlier. The command's own result is
    # written when it runs.
    if sheet.get("review_status") and not pending:
        grid.set(row, "outcome_status", sheet["review_status"])
    grid.set(row, "lead_status", _lead_status(row, sheet))
    return True


def _restore_name(grid: SheetGrid, row: SheetRow, sheet: dict[str, Any]) -> bool:
    """"Same person, keep old name": the Name cell goes back to the name on file. True if queued.

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
        return False
    grid.set(row, "name", sheet["name"], expect=shown)
    return True


def _restore_values(grid: SheetGrid, row: SheetRow, kept: dict[str, Any]) -> list[str]:
    """"Keep old location / case / date of birth": the cell goes back to the value on file.

    Owner, 2026-10-25: the same rules as the Name (`_restore_name`): only over the exact value the
    review was about, re-checked just before the write (`expect`), fingerprint stored with it (no
    new review, no loop), and the kept marker spent once it landed. Returns the columns queued.
    """
    written = []
    for column, pair in kept.items():
        if column not in VALUE_CODES.values() or not isinstance(pair, list | tuple) or len(pair) != 2:
            continue
        value, replaces = (str(part or "") for part in pair)
        shown = row.get(column)
        if column == "date_of_birth":
            same = bool(shown) and normalize_dob(shown) == normalize_dob(value)
            matches = bool(shown) and normalize_dob(shown) == normalize_dob(replaces)
        else:
            same, matches = _same_name(shown, value), _same_name(shown, replaces)
        if same or not matches or not value:
            continue
        grid.set(row, column, value, expect=shown)
        written.append(column)
    return written


def _in_progress(status: str) -> bool:
    return status.casefold().startswith("processing: ") or bool(RETRYING.match(status))


def _command_pending(row: SheetRow) -> bool:
    """A command in the row that has not finished: typed (settling) or running."""
    command = command_of(row)
    return _in_progress(row.get("outcome_status")) or bool(command and needs_processing(row, command))


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


def needs_attention(row: SheetRow, old: set[int] | frozenset[int] = frozenset()) -> bool:
    """From the scanned columns alone: a command to run (or an old one to empty), or one in progress.

    `old`: `old_commands` of the scanned rows.
    """
    command = command_of(row)
    if command and (row.number in old or needs_processing(row, command)):
        return True
    if _hint_stale(row) or unknown_command(row):
        return True
    status = row.get("outcome_status")
    return status.casefold().startswith("processing: ") or bool(RETRYING.match(status))


# Rows moved per night: keeps the single Sheets request small; the rest go the next night.
ARCHIVE_MAX_ROWS = 2000
ARCHIVE_LEAD_STATUSES = ["booked", "do_not_contact", "declined", "closed_no_response",
                         "invalid_phone"]


def archive_candidates(grid: SheetGrid, days: int) -> list[SheetRow]:
    """Rows whose lead is finished (by the database, not the Sheet text) and quiet for `days`."""
    old = old_commands(grid.rows)
    rows = [r for r in grid.rows if _is_uuid(r.get("lead_id")) and not needs_attention(r, old)]
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
    old = old_commands(grid.rows)
    for row in grid.rows:
        command = command_of(row)
        if command and row.number not in old and needs_processing(row, command):
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
        # Commands from before 2026-10-25 already handled: the next poll empties them, never runs them.
        "old_commands_to_empty": sorted(old),
        "commands_invalid": invalid,
        "stuck": stuck,
        "columns_missing": [k for k in ("lead_status", "cadence_day", "call_outcome",
                                        "message_outcome", "transcript_link",
                                        "action_started_at") if k not in grid.columns],
    }
