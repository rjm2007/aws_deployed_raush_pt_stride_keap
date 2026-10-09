"""One command to test the booking voice agent on this Mac, without touching live.

Run from the repo root (pgserver gives a local Postgres, cloudflared a public tunnel):

    uv run --frozen --with pgserver python scripts/booking_agent_test.py setup     # once
    uv run --frozen --with pgserver python scripts/booking_agent_test.py up
    uv run --frozen --with pgserver python scripts/booking_agent_test.py call \\
        --phone +919999999999 --name "Dev Chaudhary" --clinic "Laguna Niguel"
    uv run --frozen --with pgserver python scripts/booking_agent_test.py chat --name "Dev Test"
    uv run --frozen --with pgserver python scripts/booking_agent_test.py status
    uv run --frozen --with pgserver python scripts/booking_agent_test.py down

setup  builds a local database in local/pgdata (migrations, seed, demo clinics) and points .env at it.
up     starts whatever is not running (fake SMS, API, workers, tunnel) and, when the tunnel address
       changed, points ONLY the new booking assistant at it.
call   creates a test lead and places one real call now through the cadence worker's own code, then
       prints the transcript, tool calls and booking result when the call ends.
chat   the same agent over Vapi's text chat: you type as the patient. Real tools, real Stride demo.
down   stops everything `up` started.

Safety: refuses to run when SUPABASE_DB_URL is not a local database. The old outreach assistant and
the live system are never changed. Texts are simulated locally (TWILIO_MODE=mock).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "local"
RUN = LOCAL / "run"
LOGS = LOCAL / "logs"
PGDATA = LOCAL / "pgdata"
ENV_FILE = ROOT / ".env"
CLOUDFLARED = Path.home() / ".local" / "bin" / "cloudflared"
UV = Path.home() / ".local" / "bin" / "uv"
BOOKING = json.loads((ROOT / "config" / "vapi_booking_tools.json").read_text())
NEW_ASSISTANT = BOOKING["assistant_id"]
OLD_ASSISTANT = BOOKING["copied_from"]
TOOL_IDS = {tool["function"]["name"]: tool["id"] for tool in BOOKING["tools"]}
LEAD_STATUS_COPY = BOOKING["lead_status_tool_copy_id"]
VAPI = "https://api.vapi.ai"

# --------------------------------------------------------------------------- .env


def read_env() -> dict[str, str]:
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text().splitlines()
    seen = set()
    for index, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if key in updates:
            lines[index] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{key}={value}" for key, value in updates.items() if key not in seen]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def require_local_db() -> str:
    url = read_env().get("SUPABASE_DB_URL", "")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    socket_host = "host=/" in url
    if not url or not (socket_host or host in {"localhost", "127.0.0.1"}):
        sys.exit("REFUSING: SUPABASE_DB_URL in .env is not a local database. Run `setup` first.")
    return url


# --------------------------------------------------------------------------- processes


def pid_alive(name: str) -> bool:
    pid_file = RUN / f"{name}.pid"
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def start(name: str, command: list[str]) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("a")
    process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    (RUN / f"{name}.pid").write_text(str(process.pid))


def stop(name: str) -> None:
    if pid_alive(name):
        os.killpg(int((RUN / f"{name}.pid").read_text()), signal.SIGTERM)
    (RUN / f"{name}.pid").unlink(missing_ok=True)


def healthy(url: str, timeout: float = 5) -> bool:
    try:
        return httpx.get(url, timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def wait_for(url: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if healthy(url):
            return True
        time.sleep(2)
    return False


SERVICES = {
    "mock": [str(UV), "run", "--frozen", "uvicorn", "rpt_agent.mock_server:app", "--host",
             "127.0.0.1", "--port", "9000"],
    "api": [str(UV), "run", "--frozen", "uvicorn", "rpt_agent.api:app", "--host", "127.0.0.1",
            "--port", "8000"],
    "worker": [str(UV), "run", "--frozen", "rpt-worker"],
    "booking": [str(UV), "run", "--frozen", "rpt-booking-worker"],
}

# --------------------------------------------------------------------------- database


def ensure_postgres() -> str:
    import pgserver

    PGDATA.mkdir(parents=True, exist_ok=True)
    _stub_pgcrypto(Path(pgserver.__file__).parent)
    return pgserver.get_server(str(PGDATA), cleanup_mode=None).get_uri()


def _stub_pgcrypto(package: Path) -> None:
    """Migration 001 creates pgcrypto, which this embedded Postgres lacks (gen_random_uuid is built in)."""
    for extension in package.rglob("share/postgresql/extension"):
        control = extension / "pgcrypto.control"
        if not control.exists():
            control.write_text("comment = 'stub for local tests'\ndefault_version = '1.3'\n"
                               "relocatable = true\n")
            (extension / "pgcrypto--1.3.sql").write_text("")


def rpt(*args: str) -> None:
    subprocess.run([str(UV), "run", "--frozen", "rpt", *args], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)


def cmd_setup(args) -> None:
    import psycopg

    admin_url = ensure_postgres()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname='rpt_local'").fetchone()
        if exists and args.fresh:
            conn.execute("drop database rpt_local with (force)")
            exists = None
        if not exists:
            conn.execute("create database rpt_local")
    url = admin_url.replace("/postgres?", "/rpt_local?", 1)
    env = read_env()
    write_env({
        "SUPABASE_DB_URL": url, "APP_ENV": "development", "TEST_MODE": "true",
        "TWILIO_MODE": "mock", "KEAP_MODE": "mock", "MOCK_BASE_URL": "http://127.0.0.1:9000",
        "SHEET_SYNC_ENABLED": "false", "STRIDE_IMPORT_ENABLED": "false",
        "VAPI_ASSISTANT_ID": NEW_ASSISTANT,
        "BOOKING_TODAY_OVERRIDE": env.get("BOOKING_TODAY_OVERRIDE") or "2026-06-15",
    })
    require_local_db()
    rpt("migrate")
    rpt("seed")
    rpt("booking-demo-seed")
    env = read_env()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "update practice_settings set vapi_assistant_id=%s,vapi_phone_number_id=%s,"
            "twilio_from_number=%s,stride_booking_enabled=true,"
            "booking_link_url=coalesce(nullif(%s,''),booking_link_url) "
            "where practice_id=(select id from practices where slug='rausch-pt')",
            (NEW_ASSISTANT, env.get("VAPI_PHONE_NUMBER_ID", ""), env.get("TWILIO_FROM_NUMBER", ""),
             env.get("BOOKING_LINK_URL", "")),
        )
    print("Local database ready (local/pgdata, database rpt_local). Next: `up`.")


# --------------------------------------------------------------------------- vapi


def vapi_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {read_env()['VAPI_API_KEY']}",
            "Content-Type": "application/json"}


def vapi(method: str, path: str, **kwargs) -> dict:
    """Vapi request with a few retries: its API returns the odd transient 5xx."""
    for attempt in range(4):
        response = httpx.request(method, f"{VAPI}{path}", headers=vapi_headers(), timeout=30,
                                 **kwargs)
        if response.status_code < 500:
            response.raise_for_status()
            return response.json()
        time.sleep(2 * (attempt + 1))
    response.raise_for_status()
    return {}


def point_new_agent(base: str) -> None:
    """Point ONLY the new assistant and its own tools at `base`. The old assistant is never touched."""
    old_before = vapi("GET", f"/assistant/{OLD_ASSISTANT}")["updatedAt"]
    paths = {
        TOOL_IDS["find_slots"]: "/api/v1/tools/find-slots",
        TOOL_IDS["book_appointment"]: "/api/v1/tools/book-appointment",
        LEAD_STATUS_COPY: "/api/v1/webhooks/vapi/lead-status",
    }
    for tool_id, path in paths.items():
        tool = vapi("GET", f"/tool/{tool_id}")
        if tool["server"]["url"] == base + path:
            continue
        server = copy.deepcopy(tool["server"])
        server["url"] = base + path
        vapi("PATCH", f"/tool/{tool_id}", json={"server": server})
        after = vapi("GET", f"/tool/{tool_id}")["server"]
        assert after["url"] == base + path and after.get("headers") == tool["server"].get("headers")
    assistant = vapi("GET", f"/assistant/{NEW_ASSISTANT}")
    server = copy.deepcopy(assistant["server"])
    server["url"] = base + "/api/v1/vapi/webhook"
    if assistant["server"]["url"] != server["url"]:
        vapi("PATCH", f"/assistant/{NEW_ASSISTANT}", json={"server": server})
        after = vapi("GET", f"/assistant/{NEW_ASSISTANT}")
        assert after["server"]["url"] == server["url"]
        assert after["model"]["messages"] == assistant["model"]["messages"], "prompt changed"
    assert vapi("GET", f"/assistant/{OLD_ASSISTANT}")["updatedAt"] == old_before, "old assistant changed"
    (RUN / "pointed_url").write_text(base)
    print(f"  new booking agent now points at {base}")


# --------------------------------------------------------------------------- up / down


def tunnel_url() -> str:
    log = LOGS / "tunnel.log"
    if not log.exists():
        return ""
    found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text())
    return found[-1] if found else ""


def cmd_up(args=None) -> None:
    require_local_db()
    ensure_postgres()
    for name in ("mock", "api", "worker", "booking"):
        if not pid_alive(name):
            start(name, SERVICES[name])
            print(f"  started {name}")
    if not wait_for("http://127.0.0.1:8000/ready", 60):
        sys.exit("API did not become ready; see local/logs/api.log")
    url = tunnel_url()
    if not (pid_alive("tunnel") and url and healthy(f"{url}/health", 10)):
        stop("tunnel")
        (LOGS / "tunnel.log").write_text("")
        start("tunnel", [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url",
                         "http://127.0.0.1:8000"])
        deadline = time.time() + 40
        while time.time() < deadline and not tunnel_url():
            time.sleep(1)
        url = tunnel_url()
        if not url or not wait_for(f"{url}/health", 90):
            sys.exit("Tunnel did not come up; see local/logs/tunnel.log")
        print(f"  tunnel {url}")
    if read_env().get("PUBLIC_BASE_URL") != url:
        write_env({"PUBLIC_BASE_URL": url})
        for name in ("api", "worker"):
            stop(name)
            start(name, SERVICES[name])
        wait_for("http://127.0.0.1:8000/ready", 60)
    pointed = RUN / "pointed_url"
    if not pointed.exists() or pointed.read_text() != url:
        point_new_agent(url)
    print(f"Ready. Dashboard: run `npx next dev -p 3000` in rpt_frontend. Tunnel: {url}")


def cmd_down(args) -> None:
    for name in ("tunnel", "booking", "worker", "api", "mock"):
        stop(name)
    print("Stopped. (The local database keeps running in the background; it uses no network.)")


def cmd_status(args) -> None:
    url = tunnel_url()
    for name in ("mock", "api", "worker", "booking", "tunnel"):
        print(f"  {name:8} {'running' if pid_alive(name) else 'stopped'}")
    print(f"  api ready: {healthy('http://127.0.0.1:8000/ready')}")
    print(f"  tunnel   : {url or '-'} reachable={bool(url) and healthy(f'{url}/health', 10)}")


# --------------------------------------------------------------------------- leads


def create_lead(name: str, phone: str, clinic: str, case: str, dob: str, with_call: bool) -> str:
    os.chdir(ROOT)
    from rpt_agent.db import transaction

    first, _, last = name.strip().partition(" ")
    with transaction() as conn:
        practice = conn.execute("select id from practices where slug='rausch-pt'").fetchone()["id"]
        lead = conn.execute(
            "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
            "date_of_birth,status,cadence_state,is_test,location,lead_type,status_reason) "
            "values(%s,'dashboard',%s,%s,%s,%s,%s,'in_progress',%s,true,%s,%s,"
            "'booking agent test script') returning id",
            (practice, name.strip(), first, last or first, phone, dob,
             "active" if with_call else "paused", clinic, case),
        ).fetchone()["id"]
        if with_call:
            # One call step due now; the worker dispatches it exactly like a cadence call.
            conn.execute(
                "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for,"
                "day_offset) values(%s,1,'call','planned',now(),0)",
                (lead,),
            )
    return str(lead)


def print_call(call: dict) -> None:
    artifact = call.get("artifact") or {}
    for message in artifact.get("messages") or call.get("messages") or []:
        role = message.get("role")
        when = f"{message.get('secondsFromStart', 0):6.1f}s"
        if role == "system":
            continue
        if message.get("toolCalls"):
            for tool_call in message["toolCalls"]:
                function = tool_call.get("function", {})
                print(f"{when}  TOOL  {function.get('name')} {function.get('arguments')}")
        elif role in {"tool_call_result", "tool"}:
            print(f"{when}  RESULT {str(message.get('result') or message.get('content'))[:200]}")
        else:
            print(f"{when}  {role.upper():5} {message.get('message') or message.get('content')}")
    print(f"\nEnded: {call.get('endedReason')}")


def print_outcome(lead_id: str) -> None:
    from rpt_agent.db import transaction

    with transaction() as conn:
        lead = conn.execute("select status,needs_review,review_reason from leads where id=%s",
                            (lead_id,)).fetchone()
        appointment = conn.execute(
            "select a.state,a.stride_appointment_id,c.display_name,a.start_utc from appointments a "
            "left join clinicians c on c.stride_user_id=a.clinician_id where a.lead_id=%s "
            "order by a.id desc limit 1", (lead_id,),
        ).fetchone()
    print(f"Lead: status={lead['status']} needs_review={lead['needs_review']} "
          f"{lead['review_reason'] or ''}")
    if appointment:
        print(f"Appointment: {appointment['state']} stride_id={appointment['stride_appointment_id']} "
              f"{appointment['display_name'] or ''} {appointment['start_utc']}")


def cmd_call(args) -> None:
    require_local_db()
    if not re.fullmatch(r"\+\d{8,15}", args.phone):
        sys.exit("--phone must be E.164, for example +919876543210")
    if not healthy("http://127.0.0.1:8000/ready") or not pid_alive("tunnel"):
        print("Starting the local stack first...")
        cmd_up()
    lead_id = create_lead(args.name, args.phone, args.clinic, args.case, args.dob, with_call=True)
    print(f"Lead {lead_id} created; placing the call now...")
    from rpt_agent.db import transaction
    from rpt_agent.worker import run_tick

    run_tick()  # the background worker may also pick it up; either way it is dispatched once
    call_id = None
    deadline = time.time() + 90
    while time.time() < deadline and not call_id:
        with transaction() as conn:
            row = conn.execute(
                "select vapi_call_id,status,failure_reason from outreach_events where lead_id=%s",
                (lead_id,),
            ).fetchone()
        call_id = row["vapi_call_id"]
        if row["status"] in {"failed", "unknown"}:
            sys.exit(f"Dispatch failed: {row['failure_reason']}")
        time.sleep(2)
    if not call_id:
        sys.exit("The call was not dispatched within 90 s; see local/logs/worker.log")
    print(f"Calling {args.phone} (Vapi call {call_id}). Answer the phone. Waiting for it to end...")
    if args.no_wait:
        return
    call = {}
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        call = vapi("GET", f"/call/{call_id}")
        if call.get("status") == "ended":
            break
        time.sleep(4)
    time.sleep(5)  # let the end-of-call report reach the local backend
    print("\n=== Transcript ===")
    print_call(call)
    print_outcome(lead_id)


def cmd_chat(args) -> None:
    require_local_db()
    if not healthy("http://127.0.0.1:8000/ready") or not pid_alive("tunnel"):
        print("Starting the local stack first...")
        cmd_up()
    lead_id = create_lead(args.name, "+15550100000", args.clinic, args.case, args.dob,
                          with_call=False)
    variables = {"lead_id": lead_id, "Patient_name": args.name, "patient_name": args.name,
                 "call_attempt": "1", "clinic_location": args.clinic, "case_name": args.case}
    print(f"Lead {lead_id}. You are the patient; type 'quit' to stop. Start with 'Hello?'.\n")
    previous = None
    while True:
        try:
            text = input("YOU: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text or text.lower() in {"quit", "exit"}:
            break
        body = {"assistantId": NEW_ASSISTANT, "input": text,
                "assistantOverrides": {"variableValues": variables}}
        if previous:
            body["previousChatId"] = previous
        reply = httpx.post(f"{VAPI}/chat", headers=vapi_headers(), json=body, timeout=90).json()
        previous = reply.get("id")
        for message in reply.get("output", []):
            calls = message.get("tool_calls") or message.get("toolCalls")
            if calls:
                for tool_call in calls:
                    function = tool_call.get("function", {})
                    print(f"   [tool {function.get('name')} {function.get('arguments')}]")
            elif message.get("role") == "tool":
                print(f"   [result {str(message.get('content'))[:160]}]")
            elif message.get("content"):
                print(f"SARAH: {message['content']}")
    print_outcome(lead_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="build the local database and point .env at it")
    setup.add_argument("--fresh", action="store_true", help="drop and rebuild the local database")
    sub.add_parser("up", help="start the local stack and tunnel")
    sub.add_parser("down", help="stop the local stack and tunnel")
    sub.add_parser("status", help="show what is running")
    for name in ("call", "chat"):
        command = sub.add_parser(name)
        if name == "call":
            command.add_argument("--phone", required=True, help="E.164, e.g. +919876543210")
            command.add_argument("--no-wait", action="store_true", help="do not wait for the call")
        command.add_argument("--name", required=True, help='e.g. "Dev Chaudhary"')
        command.add_argument("--clinic", default="Laguna Niguel",
                             choices=["Laguna Niguel", "Dana Point", "Mission Viejo"])
        command.add_argument("--case", default="Physical Therapy")
        command.add_argument("--dob", default="1990-01-15", help="YYYY-MM-DD stored on the lead")
    args = parser.parse_args()
    {"setup": cmd_setup, "up": cmd_up, "down": cmd_down, "status": cmd_status,
     "call": cmd_call, "chat": cmd_chat}[args.command](args)


if __name__ == "__main__":
    main()
