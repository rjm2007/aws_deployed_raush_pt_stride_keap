"""Reply to patients' texts: the SMS agent's own process (docs/SMS_AGENT.md, "Flow").

The Twilio webhook only stores a text and queues a job (services/sms_inbound.queue_reply). This
worker claims due jobs, decides whether to reply (services/sms_inbound.should_reply), runs the agent
and sends the reply. Its own container: a model call takes 10-30 s and the cadence worker must never
wait on it. Exactly one instance claims (session advisory lock), and jobs run one at a time, so one
phone never gets two replies at once.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .config import Settings, get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .providers import ProviderClients, ProviderError
from .services import sms_inbound
from .services.lead_status import SMS_AGENT_SOURCE, record_status
from .services.review import flag_lead_for_review, record_review

SERVICE = "sms-agent-worker"
# Session-level advisory lock: a second container started by mistake idles instead of replying
# to the same patients.
SMS_AGENT_WORKER_LOCK_ID = 7_272_054_130
# A job left 'running' this long was cut off (container restart). It may have sent texts, so it
# is closed as failed, never run again.
STUCK_AFTER = "10 minutes"
HEARTBEAT_SECONDS = 60
SWITCHED_OFF_SECONDS = 60
# One retry of a send Twilio clearly refused before sending (no retry storm, owner 2026-11-02).
SEND_RETRY_DELAY_SECONDS = 2.0

logger = logging.getLogger(__name__)

# Due jobs, oldest first; never one whose phone already has a job running.
CLAIM_SQL = """
with due as (
  select j.id from sms_agent_jobs j
  where j.status='pending' and j.due_at<=now()
    and not exists(select 1 from sms_agent_jobs r where r.phone_e164=j.phone_e164 and r.status='running')
  order by j.due_at,j.id limit 1 for update skip locked
)
update sms_agent_jobs j set status='running',claimed_at=now(),attempts=j.attempts+1,updated_at=now()
from due where j.id=due.id
returning j.id,j.lead_id,j.phone_e164,j.last_inbound_id,j.attempts
"""


@contextmanager
def sms_agent_worker_lock(database_url: str, connect_timeout: float):
    with psycopg.connect(
        database_url, autocommit=True, connect_timeout=max(1, int(connect_timeout))
    ) as conn:
        acquired = conn.execute(
            "select pg_try_advisory_lock(%s)", (SMS_AGENT_WORKER_LOCK_ID,)
        ).fetchone()[0]
        if not acquired:
            raise RuntimeError("another sms-agent-worker already owns the worker lock")
        try:
            yield conn
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (SMS_AGENT_WORKER_LOCK_ID,))


def claim_job() -> dict[str, Any] | None:
    with transaction() as conn:
        return conn.execute(CLAIM_SQL).fetchone()


def _finish(conn, job_id: int, status: str, outcome: str, *, error: str | None = None,
            reply_message_id: int | None = None) -> None:
    conn.execute(
        "update sms_agent_jobs set status=%s,outcome=%s,error=%s,"
        "reply_message_id=coalesce(%s,reply_message_id),finished_at=now(),updated_at=now() "
        "where id=%s",
        (status, outcome, (error or "")[:500] or None, reply_message_id, job_id),
    )


def _retry_later(conn, job: dict[str, Any], reason: str, seconds: int) -> None:
    """Put the job back for later. A newer text may already have a pending job for this phone
    (only one may be pending): that job answers everything up to its newest text, so this one
    merges into it and that one waits at least as long."""
    merged = conn.execute(
        "update sms_agent_jobs set due_at=greatest(due_at,now()+make_interval(secs=>%s)),"
        "updated_at=now() where phone_e164=%s and status='pending' and id<>%s returning id",
        (seconds, job["phone_e164"], job["id"]),
    ).fetchone()
    if merged:
        _finish(conn, job["id"], "skipped", "merged")
        return
    conn.execute(
        "update sms_agent_jobs set status='pending',outcome=%s,claimed_at=null,"
        "due_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
        (reason, seconds, job["id"]),
    )


# hard_stop reasons the agent's own opt-out causes (do_not_contact when calls stopped too, which
# also suppresses the number).
OPT_OUT_STOPS = {"sms_opt_out", "do_not_contact", "suppressed"}


def _lock_lead(conn, lead_id: str | None) -> None:
    # Lead row first, the same order as bookings and call outcomes (no deadlock with them).
    if lead_id:
        conn.execute("select id from leads where id=%s for update", (lead_id,))


def _fixed_text(conn, key: str, lead_id: str | None, settings: Settings) -> str:
    from .services import sms_agent_texts  # the agent core's module; imported when first needed

    link = conn.execute(
        "select ps.booking_link_url from practice_settings ps "
        "join practices p on p.id=ps.practice_id "
        "where p.id=coalesce((select practice_id from leads where id=%s),"
        "(select id from practices where slug=%s)) limit 1",
        (lead_id, settings.stride_practice_slug),
    ).fetchone()
    first = conn.execute(
        "select coalesce(first_name,'') as first_name from leads where id=%s", (lead_id,)
    ).fetchone() if lead_id else None
    return sms_agent_texts.render(
        conn, key,
        booking_link=(link or {}).get("booking_link_url") or "",
        front_desk_phone=settings.clinic_front_desk_phone,
        first_name=(first or {}).get("first_name", ""),
    )


# A lead whose outcome is final (or whose cadence has ended) keeps it when a failure card opens:
# the same rule as Elliot's own front-desk card (sms_agent_tools.open_front_desk_card) and the case
# change card on a booked lead (lead_status.flag_case_change).
_FINAL_STATUSES = {"booked", "declined", "do_not_contact", "closed_no_response"}


def _flag(conn, lead_id: str | None, reason: str, code: str, details: dict | None = None, *,
          booked_text: bool = False) -> None:
    """Tell staff the agent failed (agent error, send failure, reply cap).

    flag_lead_for_review leaves a finished lead alone (returns False, writes nothing), so on a
    booked, declined or closed lead the failure used to vanish: a booked-by-text patient whose
    "booked" text failed was never followed up. Those leads get the card without a pause or status
    change (`finished`, like the front-desk card on them). A patient who opted out of texts gets
    an Activity line only: a card asking staff to text them back (and pausing their calls) would be
    wrong - unless the unsent text was a booking confirmation, which staff must still pass on.
    """
    if not lead_id:
        return
    lead = conn.execute(
        "select status,cadence_state,sms_opt_out,sms_disabled from leads where id=%s for update",
        (lead_id,),
    ).fetchone()
    if not lead:
        return
    texts_off = (lead["status"] == "do_not_contact" or lead["sms_opt_out"]
                 or lead["sms_disabled"])
    if texts_off and not booked_text:
        record_status(conn, lead_id, lead["status"], lead["status"], SMS_AGENT_SOURCE,
                      f"{reason} (no review: the patient gets no texts)"[:500])
        return
    finished = (lead["status"] in _FINAL_STATUSES
                or lead["cadence_state"] in {"completed", "terminated"})
    if not finished:
        flag_lead_for_review(conn, lead_id, reason, code=code, details=details,
                             source="text")  # "Text message" on the card (lead_review.SOURCE_LABELS)
        return
    conn.execute(
        "update leads set needs_review=true,review_reason=coalesce(case when needs_review "
        "then review_reason end,%s),review_flagged_at=coalesce(case when needs_review "
        "then review_flagged_at end,now()) where id=%s",
        (reason[:500], lead_id),
    )
    record_review(conn, lead_id, code, reason, details={**(details or {}), "finished": True},
                  source="text")


# The texts a tool sends word for word (sms_agent_texts). The worker tells them apart from Elliot's
# own words by their template: AgentReply carries the texts in sending order only.
_OTHER_FIXED_KEYS = ("intro", "new_person_intro", "front_desk", "english_only", "text_only",
                     "unknown_number", "cancel_visit", "booking_problem", "reply_limit",
                     "insurance")
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_NOT_WORD = re.compile(r"[^0-9a-z]+")
# A Studio template with almost no fixed words would match anything: not used to recognise texts.
_MIN_TEMPLATE_CHARS = 20


def _canon(text: str) -> str:
    # Letters and digits only: render() tidies spaces and commas around an empty first name.
    return _NOT_WORD.sub("", (text or "").lower())


def _matches(text: str, body: str) -> bool:
    literals = [_canon(part) for part in _PLACEHOLDER.split(body or "")]
    if len("".join(literals)) < _MIN_TEMPLATE_CHARS:
        return False
    return re.fullmatch(".*?".join(map(re.escape, literals)), _canon(text)) is not None


def _sort_texts(conn, lead_id: str | None, texts: list[str]) -> tuple[list[str], list[str]]:
    """(booked texts, Elliot's own texts) among a reply's texts; the rest are other fixed texts."""
    from .services import sms_agent_texts  # the agent core's module; imported when first needed

    row = conn.execute("select practice_id from leads where id=%s", (lead_id,)).fetchone() \
        if lead_id else None
    practice_id = (row or {}).get("practice_id")
    booked_body = sms_agent_texts.template_body(conn, "booked", practice_id)
    other_bodies = [sms_agent_texts.template_body(conn, key, practice_id) for key in _OTHER_FIXED_KEYS]
    booked, own = [], []
    for text in texts:
        if _matches(text, booked_body):
            booked.append(text)
        elif not (sms_agent_texts.is_emergency_text(text)
                  or sms_agent_texts.parse_card(text)
                  or any(_matches(text, body) for body in other_bodies)):
            own.append(text)
    return booked, own


def _booked_in_reply(reply: Any) -> bool:
    return any(action.kind == "book" and action.result_code == "BOOKED"
               for action in (getattr(reply, "actions", None) or []))


def _send(trace: WorkflowTrace, providers: ProviderClients, phone: str, body: str) -> str:
    """Send one text; one retry when Twilio refused it without sending (never an ambiguous one)."""
    try:
        return providers.twilio.send_sms(trace, phone, body)
    except ProviderError as exc:
        if exc.ambiguous or not exc.retryable:
            raise
        trace.log("sms_agent_send_retry", error_category=exc.code)
        time.sleep(SEND_RETRY_DELAY_SECONDS)
        return providers.twilio.send_sms(trace, phone, body)


def process_job(
    trace: WorkflowTrace, job: dict[str, Any], providers: ProviderClients, settings: Settings
) -> str:
    """Decide, run and send for one claimed job; returns its outcome."""
    job_id = job["id"]
    with transaction() as conn:
        # The text may have been filed under a lead that has since been replaced on this phone.
        lead_id = sms_inbound.active_lead_id(conn, job["phone_e164"])
        _lock_lead(conn, lead_id)
        if lead_id and str(job.get("lead_id") or "") != lead_id:
            conn.execute("update sms_agent_jobs set lead_id=%s where id=%s", (lead_id, job_id))
        facts = sms_inbound.load_facts(conn, job, lead_id, settings)
        decision = sms_inbound.should_reply(facts, settings)
        trace.log("sms_agent_decision", job_id=job_id, action=decision.action, reason=decision.reason)
        if decision.flag_review:
            _flag(conn, lead_id, "SMS agent reached today's reply limit for this patient",
                  sms_inbound.REVIEW_REPLY_CAP,
                  {"cap": settings.sms_agent_daily_reply_cap})
        if decision.action == "skip":
            _finish(conn, job_id, "skipped", decision.reason)
            return decision.reason
        if decision.action == "retry":
            _retry_later(conn, job, decision.reason, sms_inbound.CALL_RETRY_SECONDS)
            return decision.reason
        texts: list[str] = []
        if decision.action == "fixed_text":
            texts = [_fixed_text(conn, decision.text_key or "", lead_id, settings)]
            outcome = decision.reason
        if decision.action == "agent":
            # This job answers every text stored so far: the agent reads the history moments from
            # now, so a text that came in since the claim is in it. Its own job (queued while this
            # one ran) then finds nothing new and skips, never a second answer to the same text.
            # The lead row lock holds back a text being filed under this lead meanwhile.
            conn.execute(
                "update sms_agent_jobs set last_inbound_id=greatest(last_inbound_id,"
                "(select max(id) from sms_messages where direction='inbound' and lead_id in "
                "(select id from leads where phone_e164=%s))),updated_at=now() where id=%s",
                (job["phone_e164"], job_id),
            )

    if decision.action == "agent":
        reply = None
        error = None
        try:
            from .services.sms_agent import reply_live  # the agent core; imported when first needed

            reply = reply_live(trace, lead_id=lead_id, job_id=job_id, settings=settings,
                               providers=providers)
            error = reply.error
            if not error and not reply.no_reply and not [t for t in reply.texts if t.strip()]:
                error = "agent returned no text"
        except Exception as exc:  # noqa: BLE001 - one job's failure must not stop the worker
            error = f"agent error: {type(exc).__name__}"
            trace.fail(exc, job_id=job_id)
        with transaction() as conn:
            for action in (reply.actions if reply else []):
                # Tool and result code only: no names, dates of birth or text.
                conn.execute(
                    "insert into sms_agent_actions(job_id,lead_id,tool,result_code) values(%s,%s,%s,%s)",
                    (job_id, lead_id, str(action.kind)[:100], (action.result_code or "")[:100] or None),
                )
            if error:
                _lock_lead(conn, lead_id)
                _flag(conn, lead_id, "SMS agent could not answer the patient's text",
                      sms_inbound.REVIEW_AGENT_FAILED, {"job_id": job_id})
                _finish(conn, job_id, "failed", "agent_error", error=error)
                return "agent_error"
            if reply.no_reply:
                _finish(conn, job_id, "done", sms_inbound.OUTCOME_NO_REPLY)
                return sms_inbound.OUTCOME_NO_REPLY
        texts = [t for t in reply.texts if t.strip()]
        outcome = sms_inbound.OUTCOME_REPLIED

    opted_out = decision.action == "agent" and bool(getattr(reply, "opted_out", False))
    booked_texts: list[str] = []
    # Look again just before sending: the patient may have opted out, or staff stepped in, while
    # the agent was thinking.
    with transaction() as conn:
        _lock_lead(conn, lead_id)
        if decision.action == "agent" and (opted_out or _booked_in_reply(reply)):
            booked_texts, own_texts = _sort_texts(conn, lead_id, texts)
            if opted_out:
                # Elliot opted the patient out in this reply: only his own "We won't text you
                # again." goes out (and a booking's confirmation), no intro, card or hand-off text.
                # Filtered before the fallback below, so an unrecognisable booked text counts as
                # his own words here and never lets the other fixed texts through.
                texts = [t for t in texts if t in booked_texts or t in own_texts]
            if _booked_in_reply(reply) and not booked_texts:
                # A Studio edit made the booked text unrecognisable: the whole (remaining) reply
                # goes, as a booking must never go unconfirmed.
                booked_texts = list(texts)
        stop = sms_inbound.hard_stop(sms_inbound.load_facts(conn, job, lead_id, settings))
        if stop in OPT_OUT_STOPS and opted_out:
            # Elliot opted the patient out himself in this reply ("stop texting me"): the one
            # "We won't text you again." still goes out, like the STOP keyword's own reply.
            stop = None
        if stop and booked_texts:
            # A booking made in this reply is always confirmed (owner, 2026-11-02): the booked text
            # is not marketing, and staff stepping in or "calls only" must not leave the patient
            # unsure whether they have a visit. Everything else in the reply is dropped.
            texts = [t for t in texts if t in booked_texts]
            outcome = f"booked_text_only:{stop}"
        elif stop:
            _finish(conn, job_id, "skipped", f"stopped_before_send:{stop}")
            return stop
        if not texts:
            _finish(conn, job_id, "done", sms_inbound.OUTCOME_NO_REPLY)
            return sms_inbound.OUTCOME_NO_REPLY

    last_id = None
    for index, body in enumerate(texts):
        try:
            sid = _send(trace, providers, job["phone_e164"], body)
        except ProviderError as exc:
            booked_unsent = any(text in booked_texts for text in texts[index:])
            with transaction() as conn:
                _lock_lead(conn, lead_id)
                _flag(conn, lead_id,
                      "SMS agent's booking confirmation text was not sent" if booked_unsent
                      else "SMS agent reply was not sent", sms_inbound.REVIEW_AGENT_FAILED,
                      {"job_id": job_id, "error_code": exc.code, "texts_sent": index,
                       "booked_text_unsent": booked_unsent or None},
                      booked_text=booked_unsent)
                _finish(conn, job_id, "failed", "send_failed", error=f"twilio {exc.code}",
                        reply_message_id=last_id)
            return "send_failed"
        # Each text is recorded as soon as it is sent, so a crash never loses one that went out.
        with transaction() as conn:
            row = conn.execute(
                "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,"
                "provider_message_id,sender) values(%s,'outbound',%s,now(),'queued',%s,'ai') "
                "on conflict(provider_message_id) do nothing returning id",
                (lead_id, body, sid),
            ).fetchone()
            last_id = row["id"] if row else last_id
    with transaction() as conn:
        _finish(conn, job_id, "done", outcome, reply_message_id=last_id)
    return outcome


def sweep_stuck() -> int:
    with transaction() as conn:
        rows = conn.execute(
            "update sms_agent_jobs set status='failed',outcome='worker_stopped',"
            "error='worker stopped while the job was running',finished_at=now(),updated_at=now() "
            "where status='running' and claimed_at<now()-%s::interval returning id",
            (STUCK_AFTER,),
        ).fetchall()
    return len(rows)


def skip_pending_when_off() -> int:
    """Switched off: jobs queued before the switch are closed, not answered hours later."""
    with transaction() as conn:
        rows = conn.execute(
            "update sms_agent_jobs set status='skipped',outcome='agent_disabled',finished_at=now(),"
            "updated_at=now() where status='pending' returning id"
        ).fetchall()
    return len(rows)


def write_heartbeat(**details: Any) -> None:
    """Best effort: a heartbeat failure must never stop the replies."""
    try:
        with transaction() as conn:
            conn.execute(
                "insert into service_heartbeats(service,last_seen_at,details) values(%s,now(),%s) "
                "on conflict(service) do update set last_seen_at=now(),details=excluded.details",
                (SERVICE, Jsonb(details)),
            )
    except Exception:  # noqa: BLE001 - monitoring only
        logger.warning("sms_agent_heartbeat_failed", extra={"event": "sms_agent_heartbeat_failed"})


def run_tick(providers: ProviderClients, settings: Settings, *, max_jobs: int = 20) -> dict[str, int]:
    """Sweep, then work through due jobs one at a time."""
    counts = {"processed": 0, "stuck": sweep_stuck()}
    for _ in range(max_jobs):
        job = claim_job()
        if not job:
            break
        trace = WorkflowTrace("sms_agent_reply", SERVICE)
        try:
            outcome = process_job(trace, job, providers, settings)
            trace.complete(outcome=outcome, job_id=job["id"])
        except Exception as exc:  # noqa: BLE001 - close the job, keep the loop alive
            trace.fail(exc, job_id=job["id"])
            with transaction() as conn:
                _finish(conn, job["id"], "failed", "worker_error", error=type(exc).__name__)
        counts["processed"] += 1
    return counts


def main() -> None:
    configure_logging(SERVICE)
    settings = get_settings()
    errors = settings.runtime_errors(SERVICE)
    if not settings.supabase_db_url:
        errors.append("SUPABASE_DB_URL is required")
    if errors:
        raise SystemExit("; ".join(errors))
    if not settings.sms_agent_enabled:
        # Switched off: hold no lock; close anything queued so it is not answered late on.
        while True:
            try:
                skip_pending_when_off()
            except Exception:
                logger.exception("sms agent switched-off tick failed")
            write_heartbeat(enabled=False, state="switched_off")
            time.sleep(SWITCHED_OFF_SECONDS)
    providers = ProviderClients(settings)
    with sms_agent_worker_lock(settings.supabase_db_url, settings.db_pool_timeout_seconds):
        next_heartbeat = 0.0
        while True:
            started = time.monotonic()
            try:
                counts = run_tick(providers, settings)
            except Exception:  # the next tick retries
                logger.exception("sms agent tick failed")
                counts = {"processed": 0, "stuck": 0}
            if time.monotonic() >= next_heartbeat:
                write_heartbeat(enabled=True, **counts)
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
            time.sleep(max(0.0, settings.sms_agent_poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
