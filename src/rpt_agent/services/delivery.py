from __future__ import annotations

from zoneinfo import ZoneInfo

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from ..retry import retry_delay_seconds
from ..usage_report import record_test_usage
from ..vapi_contract import extract_vapi_context, outcome_from_ended_reason
from .lead_status import apply_call_outcome


def process_pending_integrations(
    trace: WorkflowTrace, providers: ProviderClients | None = None
) -> dict[str, int]:
    providers = providers or ProviderClients()
    settings = providers.settings
    counts = {"sms": 0, "handoff": 0, "retried": 0, "dead": 0, "failed": 0}
    with transaction() as conn:
        conn.execute(
            "update notification_log n set status='skipped',error=%s,updated_at=now() "
            "from leads l where n.lead_id=l.id and n.status='queued' and (l.sms_opt_out or exists("
            "select 1 from suppressed_numbers s where s.phone_e164=l.phone_e164))",
            ("notification canceled because the recipient is opted out or suppressed",),
        )
        notifications = conn.execute(
            "select n.id,n.lead_id,n.appointment_id,n.notification_type,n.payload,n.attempts,"
            "l.phone_e164,l.first_name,a.start_utc,"
            "coalesce(ps.stride_location_timezone,l.timezone,'America/Los_Angeles') "
            "as stride_location_timezone from notification_log n join leads l on l.id=n.lead_id "
            "left join appointments a on a.id=n.appointment_id "
            "join practice_settings ps on ps.practice_id=l.practice_id "
            "where n.status='queued' and n.next_attempt_at<=now() "
            "and not l.sms_opt_out and not exists("
            "select 1 from suppressed_numbers s where s.phone_e164=l.phone_e164) "
            "order by n.id limit 20 for update of n skip locked"
        ).fetchall()
        for row in notifications:
            conn.execute(
                "update notification_log set status='sending',attempts=attempts+1,updated_at=now() "
                "where id=%s",
                (row["id"],),
            )
        outbox = conn.execute(
            "select id,payload,attempts from integration_outbox "
            "where status='pending' and next_attempt_at<=now() "
            "order by id limit 20 for update skip locked"
        ).fetchall()
        for row in outbox:
            conn.execute(
                "update integration_outbox set status='sending',attempts=attempts+1,updated_at=now() "
                "where id=%s",
                (row["id"],),
            )
    for row in notifications:
        try:
            greeting = f"Hi {row['first_name']}, " if row["first_name"] else ""
            if row["notification_type"] == "sms_booking_link":
                link = (row["payload"] or {}).get("booking_link_url", "")
                if not link:
                    raise ValueError("booking link notification has no URL")
                body = (
                    f"{greeting}schedule your Rausch PT evaluation here: {link}. "
                    "Reply STOP to opt out."
                )
            else:
                local_start = row["start_utc"].astimezone(
                    ZoneInfo(row["stride_location_timezone"])
                )
                when = local_start.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
                body = (
                    f"{greeting}your Rausch PT appointment is confirmed for {when}. "
                    "Call 949-276-5401 with questions. Reply STOP to opt out."
                )
            sid = providers.send_sms(trace, row["phone_e164"], body)
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='sent',provider_ref=%s,sent_at=now(),"
                    "updated_at=now() where id=%s",
                    (sid, row["id"]),
                )
                if providers.settings.mode("twilio") == "real":
                    record_test_usage(
                        conn,
                        "twilio",
                        "booking_link_sms"
                        if row["notification_type"] == "sms_booking_link"
                        else "booking_confirmation_sms",
                        row["lead_id"],
                        sid,
                    )
            counts["sms"] += 1
        except ProviderError as exc:
            with transaction() as conn:
                attempt = row["attempts"] + 1
                if exc.retryable and attempt < settings.retry_max_attempts:
                    delay = retry_delay_seconds(attempt, exc.retry_after_seconds, settings)
                    conn.execute(
                        "update notification_log set status='queued',error=%s,"
                        "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                        (str(exc)[:500], delay, row["id"]),
                    )
                    counts["retried"] += 1
                    trace.log(
                        "notification_retry_scheduled",
                        notification_id=row["id"],
                        attempt=attempt,
                        retry_in_seconds=delay,
                    )
                    continue
                status = "unknown" if exc.ambiguous else "failed"
                conn.execute(
                    "update notification_log set status=%s,error=%s,updated_at=now() where id=%s",
                    (status, str(exc)[:500], row["id"]),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                    "where id=%s",
                    (
                        "ambiguous SMS notification; reconcile before retry"
                        if exc.ambiguous else (
                            "SMS notification retries exhausted"
                            if exc.retryable else "SMS notification failed permanently"
                        ),
                        row["lead_id"],
                    ),
                )
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - an accepted SMS cannot be retried safely
            trace.log(
                "integration_delivery_failed",
                provider="twilio",
                notification_id=row["id"],
                error_category=type(exc).__name__,
            )
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='unknown',error=%s,updated_at=now() where id=%s",
                    (f"unexpected delivery error: {type(exc).__name__}", row["id"]),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                    "where id=%s",
                    ("SMS notification delivery requires review", row["lead_id"]),
                )
            counts["failed"] += 1
    for row in outbox:
        try:
            providers.deliver_handoff(trace, row["payload"])
            with transaction() as conn:
                conn.execute(
                    "update integration_outbox set status='delivered',delivered_at=now(),"
                    "updated_at=now() where id=%s", (row["id"],)
                )
            counts["handoff"] += 1
        except ProviderError as exc:
            with transaction() as conn:
                attempt = row["attempts"] + 1
                if exc.retryable and attempt < settings.retry_max_attempts:
                    delay = retry_delay_seconds(attempt, exc.retry_after_seconds, settings)
                    conn.execute(
                        "update integration_outbox set status='pending',last_error=%s,"
                        "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                        (str(exc)[:500], delay, row["id"]),
                    )
                    counts["retried"] += 1
                else:
                    conn.execute(
                        "update integration_outbox set status='dead',last_error=%s,updated_at=now() "
                        "where id=%s",
                        (str(exc)[:500], row["id"]),
                    )
                    counts["dead"] += 1
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - outbox event_id makes retries idempotent
            trace.log(
                "integration_delivery_failed",
                provider="keap",
                outbox_id=row["id"],
                error_category=type(exc).__name__,
            )
            with transaction() as conn:
                attempt = row["attempts"] + 1
                if attempt < settings.retry_max_attempts:
                    delay = retry_delay_seconds(attempt, settings=settings)
                    conn.execute(
                        "update integration_outbox set status='pending',last_error=%s,"
                        "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                        (f"unexpected delivery error: {type(exc).__name__}", delay, row["id"]),
                    )
                    counts["retried"] += 1
                else:
                    conn.execute(
                        "update integration_outbox set status='dead',last_error=%s,updated_at=now() "
                        "where id=%s",
                        (f"unexpected delivery error: {type(exc).__name__}", row["id"]),
                    )
                    counts["dead"] += 1
            counts["failed"] += 1
    trace.log("integration_batch_completed", **counts)
    return counts


def process_vapi_end_report(trace: WorkflowTrace, body: dict) -> str:
    """Settle one durable Vapi end report and persist its call log idempotently."""
    message = body.get("message") if isinstance(body, dict) else {}
    message = message if isinstance(message, dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    context = extract_vapi_context(body)
    lead_id = str(context.get("lead_id") or "")
    event_id = context.get("outreach_event_id")
    call_id = str(call.get("id") or body.get("id") or "")
    ended = str(message.get("endedReason") or call.get("endedReason") or "")
    if not lead_id or not event_id or not call_id:
        raise ValueError("webhook cannot be associated with a lead, event, and call")
    mapped_outcome = outcome_from_ended_reason(ended)
    outcome: str | None = mapped_outcome
    if mapped_outcome == "manual":
        with transaction() as conn:
            event = conn.execute(
                "select lead_id,status,outcome from outreach_events where id=%s",
                (int(event_id),),
            ).fetchone()
        if not event or str(event["lead_id"]) != lead_id:
            raise ValueError("webhook lead and outreach event do not match")
        if event["status"] == "delivered" and event["outcome"]:
            outcome = event["outcome"]
        elif event["status"] in {"in_flight", "attempted"}:
            outcome = None
        else:
            raise ValueError("answered call report cannot settle this outreach event")
    else:
        apply_call_outcome(
            trace,
            lead_id=lead_id,
            event_id=int(event_id),
            outcome=mapped_outcome,
            source="webhook",
        )
    with transaction() as conn:
        conn.execute(
            "insert into call_logs(outreach_event_id,lead_id,vapi_call_id,dialed_at,ended_at,"
            "answer_state,ended_reason,outcome_source) values(%s,%s,%s,coalesce(%s::timestamptz,now()),"
            "%s::timestamptz,%s,%s,%s) on conflict(vapi_call_id) do update set "
            "outcome_source=coalesce(call_logs.outcome_source,excluded.outcome_source)",
            (
                int(event_id),
                lead_id,
                call_id,
                message.get("startedAt") or call.get("startedAt"),
                message.get("endedAt") or call.get("endedAt"),
                outcome if outcome in {"voicemail", "no_answer"} else "human",
                ended,
                "tool"
                if outcome and mapped_outcome == "manual"
                else ("webhook" if outcome else None),
            ),
        )
        conn.execute(
            "update test_usage_ledger set status='ended',outcome=coalesce(%s,outcome),"
            "finalized_at=coalesce(finalized_at,now()) "
            "where provider='vapi' and provider_ref=%s",
            (outcome, call_id),
        )
    recorded_outcome = outcome or "pending_tool_outcome"
    trace.log("call_report_recorded", event_id=int(event_id), outcome=recorded_outcome)
    return recorded_outcome


def reprocess_failed_vapi_events(trace: WorkflowTrace) -> int:
    """Retry webhook processing only after the original payload is durable."""
    settings = get_settings()
    with transaction() as conn:
        rows = conn.execute(
            "select id,payload,processing_attempts from provider_events "
            "where provider='vapi' and processed_at is null "
            "and dead_lettered_at is null and processing_error is not null "
            "and next_attempt_at<=now() and processing_attempts<%s "
            "order by id limit 20 for update skip locked",
            (settings.retry_max_attempts,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update provider_events set next_attempt_at=now()+interval '5 minutes' where id=%s",
                (row["id"],),
            )
    completed = 0
    for row in rows:
        try:
            body = row["payload"]
            process_vapi_end_report(trace, body)
            with transaction() as conn:
                conn.execute(
                    "update provider_events set processed_at=now(),processing_error=null where id=%s",
                    (row["id"],),
                )
            completed += 1
            trace.log("webhook_reprocessed", provider_event_id=row["id"])
        except Exception as exc:  # noqa: BLE001 - isolate malformed durable webhook records
            with transaction() as conn:
                delay = retry_delay_seconds(row["processing_attempts"] + 1, settings=settings)
                conn.execute(
                    "update provider_events set processing_attempts=processing_attempts+1,"
                    "processing_error=%s,next_attempt_at=now()+make_interval(secs=>%s),"
                    "dead_lettered_at=case when processing_attempts+1>=%s then now() else null end "
                    "where id=%s",
                    (str(exc)[:500], delay, settings.retry_max_attempts, row["id"]),
                )
            trace.log(
                "webhook_reprocess_failed",
                provider_event_id=row["id"],
                error_category=type(exc).__name__,
            )
    return completed


def apply_twilio_message_status(conn, form_data: dict[str, str]) -> int:
    sid = form_data["MessageSid"]
    mapped = form_data["MessageStatus"].lower()
    if mapped not in {"queued", "sent", "delivered", "undelivered", "failed"}:
        raise ValueError("invalid Twilio message status")
    error = form_data.get("ErrorCode") or None
    sms = conn.execute(
        "update sms_messages set delivery_status=case "
        "when %s='delivered' then 'delivered' when delivery_status='delivered' then delivery_status "
        "when %s in ('failed','undelivered') then %s "
        "when delivery_status in ('failed','undelivered') then delivery_status "
        "when %s='sent' then 'sent' else delivery_status end,"
        "delivered_at=case when %s='delivered' then coalesce(delivered_at,now()) else delivered_at end,"
        "failure_reason=case when delivery_status='delivered' then failure_reason "
        "when %s in ('failed','undelivered') then %s else failure_reason end,"
        "updated_at=now() where provider_message_id=%s",
        (mapped, mapped, mapped, mapped, mapped, mapped, error, sid),
    ).rowcount
    notification = conn.execute(
        "update notification_log set status=case "
        "when %s='delivered' then 'delivered' when status='delivered' then status "
        "when %s in ('failed','undelivered') then %s "
        "when status in ('failed','undelivered') then status "
        "when %s='sent' then 'sent' else status end,"
        "delivered_at=case when %s='delivered' then coalesce(delivered_at,now()) else delivered_at end,"
        "error=case when status='delivered' then error "
        "when %s in ('failed','undelivered') then %s else error end,updated_at=now() "
        "where provider_ref=%s",
        (mapped, mapped, mapped, mapped, mapped, mapped, error, sid),
    ).rowcount
    usage = conn.execute(
        "update test_usage_ledger set status=case "
        "when %s='delivered' then 'delivered' when status='delivered' then status "
        "when %s in ('failed','undelivered') then %s "
        "when status in ('failed','undelivered') then status "
        "when %s='sent' then 'sent' else status end,"
        "finalized_at=case when %s in ('delivered','failed','undelivered') "
        "then coalesce(finalized_at,now()) else finalized_at end "
        "where provider='twilio' and provider_ref=%s",
        (mapped, mapped, mapped, mapped, mapped, sid),
    ).rowcount
    return sms + notification + usage


def reprocess_failed_twilio_events(trace: WorkflowTrace) -> int:
    settings = get_settings()
    with transaction() as conn:
        rows = conn.execute(
            "select id,payload,processing_attempts from provider_events "
            "where provider='twilio' and event_type='message-status' and processed_at is null "
            "and dead_lettered_at is null and processing_error is not null "
            "and next_attempt_at<=now() and processing_attempts<%s "
            "order by id limit 20 for update skip locked",
            (settings.retry_max_attempts,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update provider_events set next_attempt_at=now()+interval '5 minutes' where id=%s",
                (row["id"],),
            )
    completed = 0
    for row in rows:
        try:
            with transaction() as conn:
                if not apply_twilio_message_status(conn, row["payload"]):
                    raise LookupError("Twilio message status has no matching local record")
                conn.execute(
                    "update provider_events set processed_at=now(),processing_error=null where id=%s",
                    (row["id"],),
                )
            completed += 1
            trace.log("webhook_reprocessed", provider="twilio", provider_event_id=row["id"])
        except Exception as exc:  # noqa: BLE001 - keep the callback durable until its send row exists
            attempt = row["processing_attempts"] + 1
            delay = retry_delay_seconds(attempt, settings=settings)
            with transaction() as conn:
                conn.execute(
                    "update provider_events set processing_attempts=processing_attempts+1,"
                    "processing_error=%s,next_attempt_at=now()+make_interval(secs=>%s),"
                    "dead_lettered_at=case when processing_attempts+1>=%s then now() else null end "
                    "where id=%s",
                    (str(exc)[:500], delay, settings.retry_max_attempts, row["id"]),
                )
            trace.log(
                "webhook_reprocess_failed",
                provider="twilio",
                provider_event_id=row["id"],
                error_category=type(exc).__name__,
            )
    return completed
