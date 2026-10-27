"""Check the hang-up booking rule (owner, 2026-10-27) on a throwaway local database.

    ~/.local/bin/uv run --frozen python scripts/check_hangup_rule.py [--verbose] [--keep]

Plays each case through the real end-of-call code (delivery.process_vapi_end_report, with a payload
shaped like a real Vapi end-of-call report) and checks the result in the database. Prints one
PASS / FAIL line per case and a summary; exit code 0 only if every case passes.

Safety: it never uses the database or providers from .env. It starts its own Postgres (pgserver's
binaries) on 127.0.0.1 in a temp folder, applies the migrations to a fresh database, uses a fake
Stride and mock providers, and blocks any network connection that is not to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
REEXEC_FLAG = "RPT_CHECK_HANGUP_REEXEC"
INSTALL_HINT = ("pgserver is missing. Install it once with: "
                "~/.local/bin/uv run --frozen --with pgserver python scripts/check_hangup_rule.py")


def _pg_bin() -> Path:
    """pgserver's Postgres binaries; re-runs this script with `uv --with pgserver` if needed."""
    try:
        import pgserver  # noqa: F401
        from pgserver._commands import POSTGRES_BIN_PATH
        return Path(POSTGRES_BIN_PATH)
    except ImportError:
        pass
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if os.environ.get(REEXEC_FLAG) or not Path(uv).exists():
        print(INSTALL_HINT)
        sys.exit(2)
    env = {**os.environ, REEXEC_FLAG: "1"}
    result = subprocess.run([uv, "run", "--frozen", "--with", "pgserver", "python", __file__,
                             *sys.argv[1:]], cwd=ROOT, env=env, check=False)
    sys.exit(result.returncode)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalPostgres:
    def __init__(self, bin_dir: Path, verbose: bool):
        self.bin = bin_dir
        self.dir = Path(tempfile.mkdtemp(prefix="rpt-hangup-check-"))
        self.port = _free_port()
        self.out = None if verbose else subprocess.DEVNULL
        # Migration 001 creates pgcrypto, which pgserver's build lacks; gen_random_uuid is built in.
        ext = bin_dir.parent / "share" / "postgresql" / "extension"
        if ext.is_dir() and not (ext / "pgcrypto.control").exists():
            (ext / "pgcrypto.control").write_text("comment = 'empty stub'\ndefault_version = '1.3'\n"
                                                  "relocatable = true\n")
            (ext / "pgcrypto--1.3.sql").write_text("-- empty stub\n")

    def start(self) -> str:
        data = self.dir / "data"
        subprocess.run([self.bin / "initdb", "-D", data, "-U", "postgres", "--auth=trust",
                        "-E", "UTF8"], check=True, stdout=self.out, stderr=self.out)
        subprocess.run([self.bin / "pg_ctl", "-D", data, "-l", self.dir / "pg.log", "-w", "-o",
                        f"-p {self.port} -k {self.dir} -c listen_addresses=127.0.0.1", "start"],
                       check=True, stdout=self.out, stderr=self.out)
        import psycopg
        with psycopg.connect(f"postgresql://postgres@127.0.0.1:{self.port}/postgres",
                             autocommit=True) as conn:
            conn.execute("create database rpt_hangup_check")
        return f"postgresql://postgres@127.0.0.1:{self.port}/rpt_hangup_check"

    def stop(self, keep: bool) -> None:
        subprocess.run([self.bin / "pg_ctl", "-D", self.dir / "data", "-m", "fast", "-w", "stop"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if keep:
            print(f"Kept the temp database files in {self.dir}")
        else:
            shutil.rmtree(self.dir, ignore_errors=True)


def _block_network() -> None:
    """Any connection that is not to 127.0.0.1 / localhost / a local socket fails at once."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(f"blocked a real network call to {host}")
        return real_connect(self, address)

    socket.socket.connect = guarded
    socket.create_connection = lambda address, *a, **k: (_ for _ in ()).throw(
        RuntimeError(f"blocked a real network call to {address[0]}"))


# --------------------------------------------------------------------------- the cases

TODAY = "2026-06-15"  # pinned demo "today"
DAY1, DAY2 = "2026-06-16", "2026-06-17"  # Tuesday, Wednesday
CONFIRM = ("So that's Tuesday the sixteenth at nine in the morning with Thao at our Laguna Niguel "
           "office. Shall I book it?")
INTRO = ("Hi Rpt, it's Elliot from Rausch Physical Therapy. We got your referral, and I'd love to get "
         "your first visit on the calendar. It only takes a couple of minutes. Do you have a moment?")
WAIT_FIND = "One moment, please, while I check our availability."
WAIT_BOOK = "Thank you. One moment, please, while I confirm your appointment."
OPENING = [
    ("user", "Hello?"), ("bot", "Hi, am I speaking with Rpt Tester?"), ("user", "Yes."),
    ("bot", INTRO), ("user", "Sure."),
    ("bot", "So this is for your knee visit, at our Laguna Niguel office. Is that location good for you?"),
    ("user", "Yes."), ("bot", "And do mornings or afternoons usually work better for you?"),
    ("user", "Mornings."), ("tool", "find_slots"), ("bot", WAIT_FIND),
    ("result", ("find_slots", "OPENINGS ...")),
    ("bot", "Tuesday the sixteenth I've got nine or ten with Thao. Would either of those work?"),
    ("user", "Nine works."),
]
BIRTHDAY_YES = [("bot", ("And just so I pull up the right file, your birthday's April twenty-sixth, "
                         "nineteen ninety?")), ("user", "Yes.")]
DECLINED = {"status": "declined", "summary": "The patient said they are all set."}


def real_shaped_report(lead, event, call_id, turns, structured=None, ended="customer-ended-call"):
    """Like a real Vapi end-of-call report (demo report 11307): system message first, tool calls as
    their own items, the tool's waiting line as a separate bot item after the call."""
    msgs = [{"role": "system", "message": "<prompt>", "time": 0, "secondsFromStart": 0}]
    oai = [{"role": "system", "content": "<prompt>"}]
    for i, (role, words) in enumerate(turns):
        if role == "tool":
            call = {"id": f"call_{i}", "type": "function",
                    "function": {"name": words, "arguments": json.dumps({})}}
            msgs.append({"role": "tool_calls", "message": "", "toolCalls": [call]})
            oai.append({"role": "assistant", "tool_calls": [call]})
        elif role == "result":
            msgs.append({"name": words[0], "role": "tool_call_result", "result": words[1],
                         "toolCallId": f"call_{i - 1}"})
            oai.append({"role": "tool", "tool_call_id": f"call_{i - 1}", "content": words[1]})
        else:
            msgs.append({"role": role, "message": words, "source": "", "secondsFromStart": i})
            oai.append({"role": "assistant" if role == "bot" else "user", "content": words})
    transcript = "\n".join(f"{'AI' if r == 'bot' else 'User'}: {w}" for r, w in turns
                           if r in ("bot", "user"))
    so = structured or DECLINED
    return {"message": {
        "type": "end-of-call-report", "endedReason": ended,
        "call": {"id": call_id, "assistantOverrides": {
            "variableValues": {"lead_id": lead, "outreach_event_id": str(event)}}},
        "messages": msgs, "transcript": transcript, "analysis": {"summary": so.get("summary", "")},
        "artifact": {"messages": msgs, "messagesOpenAIFormatted": oai, "transcript": transcript,
                     "structuredOutputs": {"so": {"name": "outcome", "result": so}}},
    }}


def run_cases(verbose: bool) -> list[tuple[str, bool, str]]:
    import psycopg
    from psycopg.rows import dict_row

    from rpt_agent.observability import WorkflowTrace
    from rpt_agent.providers import Slot
    from rpt_agent.services import slot_booking
    from rpt_agent.services.availability_sync import run_sync
    from rpt_agent.services.delivery import process_vapi_end_report

    url = os.environ["SUPABASE_DB_URL"]
    db = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    trace = WorkflowTrace("hangup_check", "cli")

    class FakeStride:
        def __init__(self):
            self.open = {5981: {DAY1: ["09:00:00", "10:00:00"], DAY2: ["08:00:00"]}}
            self.creates: list[str] = []
            self.next_id = 10**9 + uuid4().int % 10**8 * 10

        def stride_availability(self, trace, *, location, duration, clinician_ids, start_date,
                                end_date):
            wanted = {int(v) for v in str(clinician_ids).split(",")}
            return [Slot(c, "US/Eastern", day, t) for c, days in self.open.items() if c in wanted
                    for day, times in days.items()
                    if start_date.isoformat() <= day <= end_date.isoformat() for t in times]

        def stride_create(self, trace, resource, payload):
            self.creates.append(resource)
            self.next_id += 1
            return self.next_id

    class Case:
        """One fresh practice, clinic, two therapists, one lead, one answered call with times offered."""

        def __init__(self, booking_enabled=True):
            self.stride = FakeStride()
            slot_booking._fast_providers = lambda settings: self.stride
            practice = db.execute(
                "insert into practices(name,slug,timezone) values('Check Practice',%s,"
                "'America/Los_Angeles') returning id", (f"check-{uuid4().hex[:10]}",)).fetchone()["id"]
            db.execute("insert into practice_settings(practice_id,stride_location_timezone,"
                       "stride_booking_enabled) values(%s,'America/New_York',%s)",
                       (practice, booking_enabled))
            location = db.execute(
                "insert into booking_locations(practice_id,name,stride_location_id,timezone) "
                "values(%s,'Laguna Niguel',3170,'America/New_York') returning id",
                (practice,)).fetchone()["id"]
            case_type = db.execute(
                "insert into case_types(practice_id,name,stride_appointment_type_id,duration_minutes,"
                "is_default) values(%s,'Physical Therapy',1452,60,true) returning id",
                (practice,)).fetchone()["id"]
            for stride_id, name in ((5981, "Thao Nguyen"), (5982, "Alan Rome")):
                clinician = db.execute(
                    "insert into clinicians(practice_id,stride_user_id,display_name) values(%s,%s,%s) "
                    "returning id", (practice, stride_id, name)).fetchone()["id"]
                db.execute("insert into clinician_assignments(clinician_id,booking_location_id,"
                           "case_type_id) values(%s,%s,%s)", (clinician, location, case_type))
            run_sync(trace, providers=self.stride, practice_id=practice)
            self.lead = str(db.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,"
                "phone_e164,date_of_birth,status,cadence_state,is_test,location,lead_type) "
                "values(%s,'dashboard','Rpt Tester','Rpt','Tester',%s,'1990-04-26','in_progress',"
                "'active',true,'Laguna Niguel','knee') returning id",
                (practice, f"+1555{uuid4().int % 10**7:07d}")).fetchone()["id"])
            self.call_id = f"check-{uuid4().hex[:12]}"
            self.event = db.execute(
                "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for,"
                "vapi_call_id) values(%s,1,'call','in_flight',now(),%s) returning id",
                (self.lead, self.call_id)).fetchone()["id"]
            if booking_enabled:
                slot_booking.find_slots(trace, lead_id=self.lead, call_id=self.call_id,
                                        when="earliest", time_of_day="morning", providers=self.stride)

        def report(self, turns, structured=None, ended="customer-ended-call"):
            return process_vapi_end_report(trace, real_shaped_report(
                self.lead, self.event, self.call_id, turns, structured, ended))

        def state(self) -> dict:
            lead = db.execute("select status,cadence_state,needs_review from leads where id=%s",
                              (self.lead,)).fetchone()
            appointments = db.execute("select count(*) n from appointments where lead_id=%s and "
                                      "state='scheduled'", (self.lead,)).fetchone()["n"]
            card = db.execute("select code from lead_reviews where lead_id=%s and resolved_at is null "
                              "order by id desc limit 1", (self.lead,)).fetchone()
            return {"status": lead["status"], "cadence": lead["cadence_state"],
                    "appointments": appointments,
                    "stride_appointments": self.stride.creates.count("appointments"),
                    "card": card["code"] if card else None}

    def describe(state: dict) -> str:
        card = f", Needs Review card {state['card']}" if state["card"] else ", no card"
        return (f"status {state['status']}, {state['appointments']} booking(s), "
                f"{state['stride_appointments']} Stride appointment(s){card}")

    def booked(s):
        return s["status"] == "booked" and s["appointments"] == 1 and s["stride_appointments"] == 1 \
            and not s["card"]

    def not_booked_not_declined(s):
        return s["status"] not in ("booked", "declined") and s["appointments"] == 0 \
            and s["stride_appointments"] == 0

    def quiet(s):
        return not_booked_not_declined(s) and not s["card"]

    def review(s):
        return s["card"] == "booking_said_yes" and s["appointments"] == 0 \
            and s["stride_appointments"] == 0

    def yes_then(reply, *more):
        return OPENING + BIRTHDAY_YES + [("bot", CONFIRM), ("user", reply)] + [("user", m) for m in more]

    def case_tool_already_booked(case):
        reply = slot_booking.book_slot(trace, lead_id=case.lead, call_id=case.call_id, arguments={
            "date": DAY1, "time": "9:00 AM", "first_name": "Rpt", "last_name": "Tester",
            "date_of_birth": "1990-04-26", "outreach_event_id": str(case.event)}, providers=case.stride)
        turns = yes_then("Yes.")[:-1] + [("user", "Yes."), ("tool", "book_appointment"),
                                          ("bot", WAIT_BOOK), ("result", ("book_appointment", reply)),
                                          ("bot", ("You're all set for Tuesday the sixteenth at nine "
                                                   "with Thao. Goodbye!"))]
        case.report(turns, {"summary": "Booked."})

    def case_twice(case):
        turns = yes_then("Yes.")
        case.report(turns)
        case.report(turns)

    def case_taken(case):
        case.stride.open = {5981: {DAY1: ["10:00:00"]}}  # nine is gone in Stride's live re-check
        case.report(yes_then("Yes."))

    yajat = [("user", "Hello?"), ("bot", "Hi, am I speaking with Rpt Tester?"), ("user", "Yep."),
             ("bot", INTRO), ("user", "Yeah."),
             ("bot", ("So this is for your knee visit, at our Laguna Niguel office. Is that location "
                      "good for you?")), ("user", "Yes."),
             ("bot", "And do mornings or afternoons usually work better for you?"),
             ("user", "Bone works for me."), ("bot", "One moment, please, while I check our available")]
    yajat_summary = {"status": "declined", "summary": "The patient said they are all set and do not "
                     "need to schedule an appointment at this time."}
    new_birthday = OPENING + BIRTHDAY_YES[:1] + [
        ("user", "No, it's May first."), ("bot", "No problem, what's the right one?"),
        ("user", "May first, nineteen ninety."), ("bot", CONFIRM), ("user", "Yes.")]
    intro_only = OPENING[:4]
    # Demo call 01a17ce0 (2026-10-27): hung up during the visit line; the summary said transferred.
    leg_visit = [("user", "Hello?"), ("bot", "Hi, am I speaking with Rpt Tester?"), ("user", "Yes."),
                 ("bot", INTRO), ("user", "Yep. Yes. Yes."),
                 ("bot", "So this is for your leg visit at our")]
    transferred_summary = {"status": "transferred_human", "summary": "The patient answered and "
                           "was transferred to a team member."}

    def in_cadence(s):
        return s["status"] != "transferred_human" and s["cadence"] == "active" and quiet(s)

    cases = [
        ("1. Yes, then hang up -> Booked", lambda c: c.report(yes_then("Yes.")), booked, "Booked"),
        ("2. \"Yes. Bye.\" -> Booked", lambda c: c.report(yes_then("Yes. Bye.")), booked, "Booked"),
        ("3. \"Yeah, that works\" -> Booked", lambda c: c.report(yes_then("Yeah, that works.")),
         booked, "Booked"),
        ("4. Named a time, hung up before the question -> not booked, not declined",
         lambda c: c.report(OPENING), quiet, "not booked, not declined, no card"),
        ("5. Yajat's call (hung up while checking times) -> not declined, not booked, no card",
         lambda c: c.report(yajat, yajat_summary), quiet, "not booked, not declined, no card"),
        ("6. \"No\" to the confirmation -> not booked, not declined",
         lambda c: c.report(yes_then("No.")), quiet, "not booked, not declined, no card"),
        ("7. \"Yes, but can we do 11 instead?\" -> first time not booked",
         lambda c: c.report(yes_then("Yes, but can we do 11 instead?")), not_booked_not_declined,
         "not booked, not declined"),
        ("8. \"Yes, that day is fine\" -> booked or review card, never nothing",
         lambda c: c.report(yes_then("Yes, that day is fine.")), lambda s: booked(s) or review(s),
         "Booked or a booking_said_yes card"),
        ("9. Birthday changed, then yes -> review card, no booking",
         lambda c: c.report(new_birthday), review, "a booking_said_yes card, no booking"),
        ("10. \"No thanks, I'm all set\" -> Declined",
         lambda c: c.report(intro_only + [("user", "No thanks, I'm all set.")]),
         lambda s: s["status"] == "declined", "Declined"),
        ("11. \"Maybe later, I'll pass\" -> Declined",
         lambda c: c.report(intro_only + [("user", "Maybe later, I'll pass for now.")]),
         lambda s: s["status"] == "declined", "Declined"),
        ("12. Booking tool already booked on the call -> booked once, nothing extra",
         case_tool_already_booked, booked, "Booked once, no card"),
        ("13. \"Yes.\" then \"Wait, make it eleven\" -> review, not booked",
         lambda c: c.report(yes_then("Yes.", "Wait, make it eleven.")), review,
         "a booking_said_yes card, no booking"),
        ("14. Time taken when booking automatically -> review", case_taken, review,
         "a booking_said_yes card, no booking"),
        ("15. Report sent twice -> one booking", case_twice, booked, "Booked once"),
        ("16. Booking switched off -> nothing", "off", quiet, "not booked, not declined, no card"),
        ("17. Summary says transferred but nobody was transferred -> stays in cadence",
         lambda c: c.report(leg_visit, transferred_summary), in_cadence,
         "not transferred, cadence active, no card"),
        ("18. Transfer tool ran -> transferred",
         lambda c: c.report(leg_visit[:-1] + [("tool", "transfer_tool_outreach")],
                            transferred_summary, "assistant-forwarded-call"),
         lambda s: s["status"] == "transferred_human", "Transferred to staff"),
        ("19. Yes, then the human copy's waiting lines -> Booked",
         lambda c: c.report(yes_then("Yes.") + [("tool", "book_appointment"),
                                                ("bot", "Perfect, booking that now."),
                                                ("bot", "Almost done...")]),
         booked, "Booked"),
        ("20. Yes, then a waiting line cut to one word -> Booked",
         lambda c: c.report(yes_then("Yes.") + [("tool", "book_appointment"), ("bot", "Perfect,")]),
         booked, "Booked"),
    ]
    results = []
    for name, action, check, expected in cases:
        try:
            if action == "off":
                case = Case(booking_enabled=False)
                case.report(yes_then("Yes."))
            else:
                case = Case()
                action(case)
            state = case.state()
            ok = check(state)
            results.append((name, ok, "" if ok else f"expected {expected}, got {describe(state)}"))
        except Exception as exc:  # noqa: BLE001 - one broken case must not hide the others
            if verbose:
                traceback.print_exc()
            results.append((name, False, f"error: {type(exc).__name__}: {str(exc)[:200]}"))
    db.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--verbose", action="store_true", help="show logs and stack traces")
    parser.add_argument("--keep", action="store_true", help="keep the temp database files")
    args = parser.parse_args()
    bin_dir = _pg_bin()
    server = LocalPostgres(bin_dir, args.verbose)
    try:
        url = server.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start the local test database: {exc}")
        server.stop(args.keep)
        return 2
    try:
        os.environ.update({
            "SUPABASE_DB_URL": url, "TEST_DATABASE_URL": url, "APP_ENV": "development",
            "TEST_MODE": "false", "PROVIDER_MODE": "mock", "VAPI_MODE": "mock", "TWILIO_MODE": "mock",
            "STRIDE_MODE": "mock", "KEAP_MODE": "mock", "SHEET_SYNC_ENABLED": "false",
            "OPENAI_API_KEY": "", "BOOKING_TODAY_OVERRIDE": TODAY, "BOOKING_MIN_NOTICE_MINUTES": "0",
        })
        sys.path.insert(0, str(ROOT / "src"))
        import logging
        if not args.verbose:
            logging.disable(logging.CRITICAL)
        from urllib.parse import urlparse

        from rpt_agent.cli import migrate
        from rpt_agent.config import get_settings

        get_settings.cache_clear()
        host = urlparse(get_settings().supabase_db_url).hostname
        if host not in ("127.0.0.1", "localhost"):
            print(f"Refusing to run: the database host is {host}, not 127.0.0.1.")
            return 2
        _block_network()
        migrate()
        results = run_cases(args.verbose)
        from rpt_agent.db import get_pool
        get_pool().close()
    finally:
        server.stop(args.keep)
    for name, ok, why in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({why})"))
    passed = sum(ok for _, ok, _ in results)
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
