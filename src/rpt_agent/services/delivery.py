from __future__ import annotations

import json
import re
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from ..retry import retry_delay_seconds
from ..usage_report import record_test_usage
from ..vapi_contract import extract_vapi_context, outcome_from_ended_reason
from .hang_up_booking import book_confirmed_time_after_hang_up
from .lead_status import BOOKED_WITHOUT_APPOINTMENT, CALLBACK_WITHOUT_TIME, apply_call_outcome
from .review import flag_lead_for_review
from .sheet_sync import dashboard_call_link, enqueue_sheet_update

TWILIO_ERROR = re.compile(r"Twilio error (\d{5})")

# The patient's last word on the call wins. A booking link waits two minutes
# after it is requested; if by then the lead is no longer booking_link_sent
# (they declined, asked for a callback, were transferred, or staff changed the
# lead) the text is never sent. The one exception: a patient who asked for the
# link AND a person (with_transfer) keeps the text while the lead is transferred.
CANCEL_CHANGED_BOOKING_LINK_SQL = (
    "update notification_log n set status='skipped',error=%s,updated_at=now() "
    "from leads l where n.lead_id=l.id and n.status='queued' "
    "and n.notification_type='sms_booking_link' and l.status<>'booking_link_sent' "
    "and not (l.status='transferred_human' and coalesce(n.payload->>'with_transfer','')='true')"
)


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
        # Calls only (texts off, e.g. a landline): the booking link would fail the same way.
        conn.execute(
            "update notification_log n set status='skipped',error=%s,updated_at=now() "
            "from leads l where n.lead_id=l.id and n.status='queued' and l.sms_disabled",
            ("notification canceled because texts are off for this lead (calls only)",),
        )
        conn.execute(
            CANCEL_CHANGED_BOOKING_LINK_SQL,
            ("booking link canceled because the patient changed their decision",),
        )
        notifications = conn.execute(
            "select n.id,n.lead_id,n.appointment_id,n.notification_type,n.payload,n.attempts,"
            "l.phone_e164,l.first_name,a.start_utc,bl.name as location_name,"
            "coalesce(ps.stride_location_timezone,l.timezone,'America/Los_Angeles') "
            "as stride_location_timezone from notification_log n join leads l on l.id=n.lead_id "
            "left join appointments a on a.id=n.appointment_id "
            "left join booking_locations bl on bl.id=a.booking_location_id "
            "join practice_settings ps on ps.practice_id=l.practice_id "
            "where n.status='queued' and n.next_attempt_at<=now() "
            "and not l.sms_opt_out and not l.sms_disabled and not exists("
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
            "where destination='keap' and status='pending' and next_attempt_at<=now() "
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
                # Wording carried over from the approved outreach copy patients
                # already receive; the STOP line is required and is our addition.
                body = (
                    f"Hi {row['first_name'] or 'there'},\n\n"
                    "We're excited to help you get started on your wellness journey with "
                    "Rausch PT & Wellness. You can schedule your first physical therapy "
                    f"session anytime using this link:\n\n{link}\n\n"
                    "If you have any trouble scheduling, have questions about which service "
                    "to choose, or don't see a time that works for you, please call us at "
                    "949-276-5401 and our team will be happy to help.\n\n"
                    "Kind regards,\nThe Rausch PT & Wellness Team\n\n"
                    "Reply STOP to opt out."
                )
            else:
                local_start = row["start_utc"].astimezone(
                    ZoneInfo(row["stride_location_timezone"])
                )
                when = local_start.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
                # Clinic asked for the location in the text (2026-10-21).
                where = f" at our {row['location_name']} office" if row.get("location_name") else ""
                body = (
                    f"{greeting}your Rausch PT appointment is confirmed for {when}{where}. "
                    "Call 949-276-5401 with questions. Reply STOP to opt out."
                )
            sid = providers.send_sms(trace, row["phone_e164"], body)
            # The message has left Twilio, so recording that fact comes first and
            # alone. Usage accounting is bookkeeping: if it fails it must not roll
            # back the row proving we already sent this, or the worker will treat
            # a delivered message as unsent.
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='sent',provider_ref=%s,sent_at=now(),"
                    "updated_at=now() where id=%s",
                    (sid, row["id"]),
                )
                # notification_log is the outbox -- retries, attempts, next
                # attempt. sms_messages is the conversation the patient sees,
                # and it is what the dashboard reads. Writing only the first
                # left a hole: we texted someone a booking link and their
                # thread showed nothing, so a reply arrived with no outbound
                # message above it.
                #
                # Same sid in both, which the Twilio status webhook already
                # matches on (sms_messages.provider_message_id and
                # notification_log.provider_ref), so delivery status tracks
                # itself with no extra work.
                conn.execute(
                    "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,"
                    "provider_message_id) values(%s,'outbound',%s,now(),'queued',%s) "
                    "on conflict (provider_message_id) do nothing",
                    (row["lead_id"], body, sid),
                )
            if providers.settings.mode("twilio") == "real":
                try:
                    with transaction() as conn:
                        record_test_usage(
                            conn,
                            "twilio",
                            "booking_link_sms"
                            if row["notification_type"] == "sms_booking_link"
                            else "booking_confirmation_sms",
                            row["lead_id"],
                            sid,
                        )
                except Exception as usage_error:  # noqa: BLE001 - never lose a sent SMS
                    trace.log(
                        "usage_recording_failed",
                        notification_id=row["id"],
                        error_category=type(usage_error).__name__,
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
                found = TWILIO_ERROR.search(str(exc))
                flag_lead_for_review(
                    conn,
                    row["lead_id"],
                    "ambiguous SMS notification; reconcile before retry"
                    if exc.ambiguous else (
                        "SMS notification retries exhausted"
                        if exc.retryable else "booking link text could not be delivered"
                    ),
                    code="booking_link_unconfirmed" if exc.ambiguous or exc.retryable
                    else "booking_link_failed",
                    source="text",
                    details={"refused": True, "error_code": found[1] if found else None,
                             "failure_reason": str(exc)[:300]},
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
                flag_lead_for_review(
                    conn, row["lead_id"], "SMS notification delivery requires review",
                    code="notification_unconfirmed", source="text",
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


def _as_number(value) -> float | None:
    """Vapi sends these as numbers or strings depending on the event."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _structured_outcome(message: dict) -> dict | None:
    """Read the assistant's post-call structured output, if it produced one.

    This is the backup for a call where the tool never fired: Vapi derives the
    same fields from the transcript, so a promised callback is still recoverable.
    """
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    outputs = artifact.get("structuredOutputs")
    if not isinstance(outputs, dict):
        return None
    for entry in outputs.values():
        result = entry.get("result") if isinstance(entry, dict) else None
        # An empty object is an extraction that failed (it has run out of
        # tokens before). Anything else is an answer, including "no decision".
        if isinstance(result, dict) and result:
            return result
    return None


def _structured_outputs(message: dict) -> dict | None:
    """Every structured output Vapi made for the call, as sent (id -> name, result), for
    call_logs.structured_output: staff and debugging see why a cut call moved the lead."""
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    outputs = artifact.get("structuredOutputs")
    return outputs if isinstance(outputs, dict) and outputs else None


def _call_text_artifacts(message: dict) -> tuple[str | None, str | None]:
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
    transcript = artifact.get("transcript")
    summary = analysis.get("summary")
    return (
        transcript[:100_000] if isinstance(transcript, str) else None,
        summary[:10_000] if isinstance(summary, str) else None,
    )


def _assistant_spoke(message: dict) -> bool:
    """Whether Sarah said anything at all on the call."""
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    return any(
        isinstance(item, dict) and item.get("role") == "bot" and str(item.get("message") or "").strip()
        for item in artifact.get("messages") or []
    )


def _call_turns(message: dict) -> list[tuple[str, str]]:
    """The call as (speaker, words) pairs, speaker being "bot" or "user"."""
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    turns = [
        (str(item.get("role")), str(item.get("message") or ""))
        for item in artifact.get("messages") or []
        if isinstance(item, dict) and item.get("role") in ("bot", "user")
    ]
    if turns:
        return turns
    transcript, _ = _call_text_artifacts(message)
    for line in (transcript or "").splitlines():
        speaker, _, words = line.partition(":")
        role = {"ai": "bot", "user": "user"}.get(speaker.strip().lower())
        if role:
            turns.append((role, words))
    return turns


def _patient_lines(message: dict, *, introduction: bool) -> list[str]:
    """What the patient said after Sarah's first line, or - with introduction=True - after she
    said why she was calling (her first line that names Rausch)."""
    turns = _call_turns(message)
    bot_lines = [i for i, (role, _) in enumerate(turns) if role == "bot"]
    if not bot_lines:
        return []
    start = bot_lines[0]
    if introduction:
        start = next(
            (i for i in bot_lines if "rausch" in turns[i][1].lower()), bot_lines[0]
        )
    return [words for role, words in turns[start + 1 :] if role == "user" and words.strip()]


def _patient_replied_after(message: dict, *, introduction: bool) -> bool:
    """Whether the patient said anything after Sarah's first line, or - with
    introduction=True - said more than a yes after she said why she was calling.
    "Yes" to "Am I speaking with ...?" and then silence is not an answer to
    anything, so it cannot be a refusal."""
    replies = _patient_lines(message, introduction=introduction)
    if not introduction:
        return bool(replies)
    # "Sure" to the introduction and then a hang-up is not a refusal either: the
    # summary has called exactly that "declined" (21 Oct, demo). A refusal needs
    # at least one reply that is more than a yes or a hello.
    return any(_words(reply) - _AGREEMENT_WORDS for reply in replies)


# A "declined" from the post-call summary is accepted only when the patient's own lines contain
# one of these (owner, 2026-10-27: never "declined" unless the patient actually said no). Demo
# call 01a1795f: "Yep", "Yes", "Bone works for me" ("both work for me"), then a hang-up while
# times were being checked; the summary invented "said they are all set" and the lead was closed.
# Word-boundary, case-insensitive, apostrophes and punctuation ignored ("don't" = "dont"); a phrase
# inside a question ("Is Thursday all set up?") does not count.
# Stored reason for `booking_hung_up` (owner, 2026-10-27).
BOOKING_HUNG_UP_REASON = "Patient asked to book, then hung up before confirming; nothing was booked"
# Stored reason for `callback_no_time` (owner, 2026-10-28).
CALLBACK_NO_TIME_REASON = "Patient asked to be called later, no time given"

REFUSAL_PHRASES = (
    "not interested", "no thanks", "no thank you", "dont need", "do not need", "not now",
    "not right now", "not at this time", "maybe later", "not really", "no longer",
    "already have", "already found", "already got", "found someone else", "going somewhere else",
    "going elsewhere", "not looking", "stop calling", "dont call", "do not call", "remove me",
    "remove my number", "take me off", "cancel it",
    "no im good", "nah im good", "im good thanks", "im good thank you", "im all good",
    "no im fine", "im fine thanks", "im fine thank you", "no im okay", "were fine", "were good",
    "ill pass", "changed my mind",
    # Not when a time or a detail follows: "I'm all set for nine", "no need to call my wife",
    # "I don't want Thursday", "I don't want to miss work".
    r"all set(?! for\b)",
    r"no need(?! to call my\b)",
    (r"i (?:dont|do not) want(?! (?:to miss|on|that day|that time|the|mornings?|afternoons?|"
     r"evenings?|early|late|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d)\b)"),
)
# A reply made only of these words is a plain "no" ("No.", "Nope", "No, no", "No. Bye."). It is a
# refusal only as the answer to Sarah's / Elliot's introduction ("... from Rausch ... Do you have a
# moment?"): a "no" to the clinic, a time or the booking confirmation means "not that one". "Stop"
# alone and "I'm busy" are not refusals.
_BARE_NO = frozenset({"no", "nope", "nah"})
_BARE_NO_FILLER = frozenset({"uh", "um", "oh", "sorry", "bye", "goodbye", "thanks", "thank", "you"})
_REFUSAL = re.compile(r"\b(?:" + "|".join(p.replace(" ", r"\s+") for p in REFUSAL_PHRASES) + r")\b")


def _refusal_words(sentence: str) -> str:
    # "No, thanks." -> "no thanks"; "don't" -> "dont".
    return re.sub(r"[^a-z]+", " ", sentence.lower().replace("\u2019", "").replace("'", "")).strip()


def _patient_refused(message: dict) -> bool:
    """Whether the patient themselves said no after Sarah said why she was calling."""
    turns = _call_turns(message)
    bot_lines = [i for i, (role, words) in enumerate(turns) if role == "bot" and words.strip()]
    if not bot_lines:
        return False
    intro = next((i for i in bot_lines if "rausch" in turns[i][1].lower()), bot_lines[0])
    last_bot = intro
    for i in range(intro + 1, len(turns)):
        role, reply = turns[i]
        if role == "bot":
            if reply.strip():
                last_bot = i
            continue
        if role != "user" or not reply.strip():
            continue
        whole = set(_refusal_words(reply).split())
        if (last_bot == intro and "?" not in reply and whole & _BARE_NO
                and whole <= _BARE_NO | _BARE_NO_FILLER):
            return True
        # Sentence by sentence, each with its own end mark: a question is never a refusal.
        for sentence, mark in re.findall(r"([^.?!]+)([.?!]*)", reply):
            if "?" not in mark and _REFUSAL.search(_refusal_words(sentence)):
                return True
    return False


_AGREEMENT_WORDS = frozenset(
    {
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "right", "fine",
        "great", "good", "sounds", "go", "ahead", "hello", "hi", "hey", "uh", "um", "mm",
        "hmm", "huh",
    }
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


# A "wrong_person" from the post-call summary is accepted only when the person said so themselves,
# in answer to "Am I speaking with ...?", and never said they were the patient (owner, 2026-10-28).
# Demo call 648: "Yes." then "No." over the greeting, then a hang-up; call 650: "Hello?" then a
# hang-up. Both were read as wrong person and the leads were closed as Wrong number.
WRONG_PERSON_PHRASES = (
    "wrong number", "wrong person", "wrong phone", "not me", "not him", "not her", "thats not",
    "this isnt", "this is not", "no one by that name", "nobody by that name", "no one here by",
    "nobody here by", "no one named", "nobody named", "no such person", "never heard of",
    r"(?:dont|do not) know (?:who|any|that|him|her|them)", "not the patient",
    r"(?:doesnt|does not|dont|do not) live here", "you have the wrong", "youve got the wrong",
    "you got the wrong", "have the wrong", "got the wrong", "the wrong one", r"there(?:s| is) no \w+ here",
)
# A family member or someone who knows the patient is not a wrong number (owner, 2026-10-28):
# "She's not home", "I'm her mom", "Can I take a message?" keep the cadence going.
_FAMILY = re.compile(
    r"\b(?:mom|mother|dad|father|wife|husband|son|daughter|sister|brother|grandma|grandmother|"
    r"grandpa|grandfather|aunt|uncle|niece|nephew|partner|girlfriend|boyfriend|take a message|"
    r"call (?:her|him) back)\b"
)
# Words a family member uses, but a stranger too ("Wrong number, I'm at work"): they keep the cadence
# going only when the same reply has no plain wrong-number phrase (night QA, 2026-10-29).
_FAMILY_SOFT = re.compile(r"\b(?:not home|not here|not available|not in|at work|sleeping|busy)\b")
_WRONG_PERSON = re.compile(r"\b(?:" + "|".join(p.replace(" ", r"\s+") for p in WRONG_PERSON_PHRASES)
                           + r")\b")
# "No, this is Maria." / "Nope, not Jordan. This is Mike.": a no, then another name (night QA, 2026-10-29).
_OTHER_NAME = re.compile(
    r"\b(?:no|nope|nah)\b(?: \w+){0,3} (?:this is|my name is) (?!(?:she|he|me|her|him|it|not|the|a|an|"
    r"fine|good|ok|okay|just|really|actually|about|for|what|who|why)\b)[a-z]+"
)
_IDENTITY_QUESTION = re.compile(r"speaking (?:with|to)|is this|are you")
_CONFIRM = re.compile(r"\b(?:yes|yeah|yep|yup|speaking|correct|thats right|thats me|its me|this is me|"
                      r"this is (?:she|he))\b")


def _patient_said_wrong_person(message: dict) -> bool:
    """Whether the person on the call said they are not the patient (or it is a wrong number) after
    Elliot asked who he was speaking with, without saying yes to being the patient before that."""
    turns = _call_turns(message)
    question = next((i for i, (role, words) in enumerate(turns)
                     if role == "bot" and _IDENTITY_QUESTION.search(words.lower())), None)
    if question is None:
        return False
    last_bot = question
    said_wrong = False
    for i, (role, reply) in enumerate(turns):
        if role == "bot":
            if i > question and reply.strip():
                last_bot = i
            continue
        if role != "user" or not reply.strip():
            continue
        text = _refusal_words(reply)
        if _FAMILY.search(text) or (_FAMILY_SOFT.search(text) and not _WRONG_PERSON.search(text)):
            return False
        if said_wrong:
            continue  # "Yes, bye" after "wrong number" is not a confirmation
        if i > question and _OTHER_NAME.search(text):
            said_wrong = True
            continue
        for sentence in re.findall(r"[^.?!]+", reply):
            words = _refusal_words(sentence)
            if _WRONG_PERSON.search(words):
                said_wrong = i > question
                if said_wrong:
                    break
            elif _CONFIRM.search(words):
                # A yes anywhere before the denial (648: "Yes." over the greeting) is the patient.
                return False
            elif (i > question and last_bot == question and set(words.split()) & _BARE_NO
                  and set(words.split()) <= _BARE_NO | _BARE_NO_FILLER):
                said_wrong = True  # a plain "No." to "Am I speaking with ...?"
                break
    return said_wrong


# Statuses that say the patient was put through to staff.
_TRANSFER_STATUSES = frozenset({"transferred_human", "booking_link_and_transfer"})


def _call_tool_calls(message: dict) -> list[dict]:
    """Every tool call made on the call, as {"name", "type", "arguments"}, from the report's
    messages (role "tool_calls" / "tool_call_result") and the OpenAI-formatted copy."""
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    found: list[dict] = []
    for items in (artifact.get("messages"), message.get("messages"),
                  artifact.get("messagesOpenAIFormatted")):
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("role") == "tool_call_result":
                found.append({"name": str(item.get("name") or ""), "type": "", "arguments": {}})
            for call in item.get("toolCalls") or item.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                found.append({"name": str(function.get("name") or call.get("name") or ""),
                              "type": str(call.get("type") or ""),
                              "arguments": arguments if isinstance(arguments, dict) else {}})
    return found


def _call_transferred(message: dict, ended_reason: str) -> bool:
    """Whether the call was really handed to staff: Vapi ended it as forwarded / during a transfer
    ("assistant-forwarded-call", "customer-ended-call-during-transfer", "...-transfer-failed"), or a
    transfer tool ran (Vapi's transferCall type, our "transfer_tool_outreach")."""
    reason = ended_reason.strip().lower()
    if reason == "assistant-forwarded-call" or "transfer" in reason:
        return True
    return any("transfer" in call["name"].lower() or call["type"] == "transferCall"
               for call in _call_tool_calls(message))


def _booking_link_requested(message: dict) -> bool:
    """Whether the agent asked for the booking link text on the call (update_lead_status with a
    booking_link status)."""
    return any(str(call["arguments"].get("status") or "").startswith("booking_link")
               for call in _call_tool_calls(message))


def _settle_from_structured_output(
    trace: WorkflowTrace, message: dict, *, lead_id: str, event_id: int, call_id: str
) -> str | None:
    """Apply the post-call structured output, or flag the call for staff.

    Never overwrites a tool result: the caller only reaches this when the event
    is still unsettled. Returns the outreach outcome, or None if nothing applied.
    """
    from .lead_status import report_lead_status  # local: lead_status imports nothing here

    result = _structured_outcome(message)
    if not result:
        with transaction() as conn:
            # Lead before event, as apply_call_outcome and the voice booking lock them (a booking
            # finishing now locks the lead, then this event): the other order can deadlock.
            conn.execute("select id from leads where id=%s for update", (lead_id,))
            conn.execute(
                "update outreach_events set status='delivered',settled_at=now(),"
                "settled_by='webhook',outcome='manual',failure_reason=%s,updated_at=now() "
                "where id=%s and status in ('in_flight','attempted')",
                ("call was answered but no outcome was reported", event_id),
            )
            flag_lead_for_review(
                conn, lead_id, "call answered but no outcome reported; see transcript",
                code="call_no_outcome", source="call", outreach_event_id=event_id,
                details={"call_id": call_id},
            )
        trace.log("state_transition_applied", transition="fallback_review", event_id=event_id)
        return "manual"
    # The extractor is told a hang-up is not a refusal and that any wording of
    # "stop contacting me" is do_not_contact, so its judgement is applied as-is.
    # No status, or "no_decision", means the patient never decided (silence, a cut call, a family
    # member, asked for times then hung up): count it as a missed contact and keep the cadence
    # going - only the patient can end outreach.
    status = str(result.get("status") or "").strip().lower() or "no_answer"
    if status == "no_decision":
        status = "no_answer"
    summary_note = str(result.get("summary") or "")
    # The extractor is told silence is not a refusal, but it runs on a model Vapi
    # picks (Gemini, whatever we pin) and has still called silence "declined".
    # Closing a lead is only allowed on the patient's own answer, which the
    # transcript shows or does not - an AI's reading of it is not enough.
    answered = (
        _patient_refused(message)
        if status == "declined"
        else _patient_replied_after(message, introduction=False)
        if status in {"do_not_contact", "call_opt_out", "already_booked"}
        # "manual" is report_lead_status's old name for wrong_person.
        else _patient_said_wrong_person(message)
        if status in {"wrong_person", "manual"}
        else True
    )
    # A transfer from the summary needs a transfer on the call (owner, 2026-10-27). Demo call
    # 01a17ce0: "Yes." / "Yep. Yes. Yes.", then a hang-up during "So this is for your leg visit";
    # no transfer tool ran, Vapi said customer-ended-call, yet the summary said "transferred to a
    # team member" and the lead left the cadence as "Call transferred". "Link and transfer" without
    # a transfer keeps the link only if the agent asked for it on the call.
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    ended = str(message.get("endedReason") or call.get("endedReason") or "")
    if status in _TRANSFER_STATUSES and not _call_transferred(message, ended):
        trace.log("fallback_transfer_without_transfer", event_id=event_id, extracted=status)
        if status == "booking_link_and_transfer" and _booking_link_requested(message):
            status = "booking_link"
        else:
            answered = False
    if not answered:
        trace.log("fallback_refusal_without_answer", event_id=event_id, extracted=status)
        status = "no_answer"
        summary_note = f"No answer from the patient; outreach continues. {summary_note}".strip()
    try:
        report_lead_status(
            trace,
            lead_id=lead_id,
            status=status,
            call_id=call_id,
            event_id=event_id,
            notes=summary_note,
            callback_type=result.get("callback_type"),
            delay_minutes=result.get("delay_minutes"),
            callback_datetime_iso=result.get("callback_datetime_iso"),
            source="call summary",
        )
    except (TypeError, ValueError) as exc:
        # The summary says "booked" but no booking exists: the patient was asking about or picking
        # a time and hung up before confirming. Its own card, in plain words (owner, 2026-10-27).
        hung_up = status == "booked" and str(exc) == BOOKED_WITHOUT_APPOINTMENT
        # "Call me later" with no time: Needs Review, so the front desk decides (owner, 2026-10-28).
        no_time = status == "callback_scheduled" and str(exc) == CALLBACK_WITHOUT_TIME
        reason = (BOOKING_HUNG_UP_REASON if hung_up
                  else CALLBACK_NO_TIME_REASON if no_time
                  else f"structured outcome could not be applied: {exc}")
        with transaction() as conn:
            # report_lead_status rolled back its own settle, so settle the call here with the card:
            # left in_flight/attempted, the Sheet showed nothing for the call and the sweeper later
            # added a second "Call result never arrived" card. Lead before event, as above.
            conn.execute("select id from leads where id=%s for update", (lead_id,))
            settled = conn.execute(
                "update outreach_events set status='delivered',settled_at=now(),"
                "settled_by='webhook',outcome='manual',failure_reason=%s,updated_at=now() "
                "where id=%s and lead_id=%s and status in ('in_flight','attempted') "
                "returning outcome",
                (reason, event_id, lead_id),
            ).fetchone()
            flag_lead_for_review(
                conn, lead_id, reason,
                code=("booking_hung_up" if hung_up else "callback_no_time" if no_time
                      else "call_outcome_not_applied"),
                source="call", outreach_event_id=event_id,
                details={"call_id": call_id, "extracted_status": status},
            )
        trace.log("validation_failed", reason="structured_output_rejected")
        return settled["outcome"] if settled else None
    with transaction() as conn:
        row = conn.execute(
            "select outcome from outreach_events where id=%s", (event_id,)
        ).fetchone()
    trace.log("state_transition_applied", transition="fallback_structured", event_id=event_id)
    return row["outcome"] if row else None


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
    transcript, summary = _call_text_artifacts(message)
    started_at_raw = message.get("startedAt") or call.get("startedAt")
    ended_at_raw = message.get("endedAt") or call.get("endedAt")
    duration_seconds = _as_number(message.get("durationSeconds") or call.get("durationSeconds"))
    call_cost = _as_number(message.get("cost") or call.get("cost"))
    if not lead_id or not event_id or not call_id:
        raise ValueError("webhook cannot be associated with a lead, event, and call")
    mapped_outcome = outcome_from_ended_reason(ended)
    outcome: str | None = mapped_outcome
    outcome_source: str | None = "webhook"
    if mapped_outcome == "manual":
        with transaction() as conn:
            event = conn.execute(
                "select lead_id,status,outcome from outreach_events where id=%s",
                (int(event_id),),
            ).fetchone()
        if not event or str(event["lead_id"]) != lead_id:
            raise ValueError("webhook lead and outreach event do not match")
        if event["status"] == "delivered" and event["outcome"]:
            outcome, outcome_source = event["outcome"], "tool"
        elif event["status"] in {"in_flight", "attempted"} and not _assistant_spoke(message):
            # Sarah never spoke, so the only voice on the call was a recording or
            # nobody at all: nothing on it can be the patient's decision. A
            # carrier's "forwarded to voice mail" message ended as
            # customer-ended-call, the summary read it as a decline, and the lead
            # was closed. Count it as a missed call and let the cadence go on.
            outcome = "no_answer"
            apply_call_outcome(
                trace, lead_id=lead_id, event_id=int(event_id), outcome=outcome, source="webhook"
            )
        elif event["status"] in {"in_flight", "attempted"}:
            # The patient said yes to "So that's ... Shall I book it?" and hung up before the
            # booking tool ran: that yes is a booking (owner, 2026-10-27). Demo booking engine
            # only; returns None (nothing done) otherwise.
            outcome = book_confirmed_time_after_hang_up(
                trace, message, _call_turns(message), lead_id=lead_id, event_id=int(event_id),
                call_id=call_id,
            )
            if outcome is None:
                # The tool never reported. Fall back to the structured output so a
                # callback the patient was promised is not silently lost.
                outcome = _settle_from_structured_output(
                    trace, message, lead_id=lead_id, event_id=int(event_id), call_id=call_id
                )
            outcome_source = "webhook" if outcome else None
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
        call_log = conn.execute(
            "insert into call_logs(outreach_event_id,lead_id,vapi_call_id,dialed_at,ended_at,"
            "answer_state,ended_reason,outcome_source,transcript_text,summary_text,"
            "duration_seconds,cost,structured_output) "
            "values(%s,%s,%s,coalesce(%s::timestamptz,now()),%s::timestamptz,%s,%s,%s,%s,%s,"
            # Derive the duration rather than trusting a field: we always have both
            # timestamps, and greatest() keeps a clock skew from storing a negative.
            "coalesce(%s,greatest(extract(epoch from (%s::timestamptz - %s::timestamptz)),0))::int,"
            "%s,%s) "
            "on conflict(vapi_call_id) do update set "
            "outcome_source=coalesce(call_logs.outcome_source,excluded.outcome_source),"
            "transcript_text=coalesce(excluded.transcript_text,call_logs.transcript_text),"
            "summary_text=coalesce(excluded.summary_text,call_logs.summary_text),"
            "duration_seconds=greatest(call_logs.duration_seconds,excluded.duration_seconds),"
            "cost=coalesce(excluded.cost,call_logs.cost),"
            "structured_output=coalesce(excluded.structured_output,call_logs.structured_output),"
            "ended_at=coalesce(excluded.ended_at,call_logs.ended_at) returning id",
            (
                int(event_id),
                lead_id,
                call_id,
                started_at_raw,
                ended_at_raw,
                outcome if outcome in {"voicemail", "no_answer"} else "human",
                ended,
                outcome_source,
                transcript,
                summary,
                duration_seconds,
                ended_at_raw,
                started_at_raw,
                call_cost,
                Jsonb(outputs) if (outputs := _structured_outputs(message)) else None,
            ),
        ).fetchone()
        settings = get_settings()
        transcript_link = dashboard_call_link(lead_id, settings) if transcript else None
        if transcript:
            conn.execute(
                "insert into call_transcripts(call_log_id,lead_id,transcript_text,summary,"
                "transcript_link) values(%s,%s,%s,%s,%s) "
                "on conflict(call_log_id) do update set "
                "transcript_text=excluded.transcript_text,"
                "summary=coalesce(excluded.summary,call_transcripts.summary),"
                "transcript_link=coalesce(excluded.transcript_link,call_transcripts.transcript_link)",
                (call_log["id"], lead_id, transcript, summary, transcript_link),
            )
        conn.execute(
            "update test_usage_ledger set status='ended',outcome=coalesce(%s,outcome),"
            "finalized_at=coalesce(finalized_at,now()) "
            "where provider='vapi' and provider_ref=%s",
            (outcome, call_id),
        )
        enqueue_sheet_update(
            conn,
            lead_id=lead_id,
            event_type="call_settled",
            source_key=f"{call_id}:{outcome or 'pending'}",
            outreach_event_id=int(event_id),
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
    sms_result = conn.execute(
        "update sms_messages set delivery_status=case "
        "when %s='delivered' then 'delivered' when delivery_status='delivered' then delivery_status "
        "when %s in ('failed','undelivered') then %s "
        "when delivery_status in ('failed','undelivered') then delivery_status "
        "when %s='sent' then 'sent' else delivery_status end,"
        "delivered_at=case when %s='delivered' then coalesce(delivered_at,now()) else delivered_at end,"
        "failure_reason=case when delivery_status='delivered' then failure_reason "
        "when %s in ('failed','undelivered') then %s else failure_reason end,"
        "updated_at=now() where provider_message_id=%s "
        "returning lead_id,outreach_event_id,delivery_status,failure_reason",
        (mapped, mapped, mapped, mapped, mapped, mapped, error, sid),
    )
    sms = sms_result.rowcount
    sms_record = sms_result.fetchone()
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
    actual_status = sms_record["delivery_status"] if sms_record else None
    # "sent" settles the step too. Twilio only reports "delivered" when the
    # carrier returns a receipt, and many never do - toll-free numbers especially -
    # so waiting for it froze the cadence behind a text the patient had received.
    # A later "failed"/"undelivered" still turns the step failed and flags the lead.
    if (
        actual_status in {"sent", "delivered", "failed", "undelivered"}
        and sms_record["outreach_event_id"]
    ):
        event = conn.execute(
            "update outreach_events oe set status=case when %s in ('sent','delivered') "
            "then 'delivered' else 'failed' end,settled_at=coalesce(oe.settled_at,now()),"
            "settled_by='webhook',"
            "failure_reason=case when %s in ('sent','delivered') then oe.failure_reason "
            "else %s end,"
            "updated_at=now() where oe.id=%s returning oe.id,oe.lead_id,oe.day_offset",
            (
                actual_status,
                actual_status,
                sms_record["failure_reason"] or actual_status,
                sms_record["outreach_event_id"],
            ),
        ).fetchone()
        if event:
            if actual_status in {"failed", "undelivered"}:
                flag_lead_for_review(
                    conn,
                    str(event["lead_id"]),
                    "cadence SMS was not delivered",
                    code="sms_failed",
                    source="text",
                    outreach_event_id=event["id"],
                    details={"channel": "sms", "day_offset": event.get("day_offset"),
                             "error_code": sms_record["failure_reason"]
                             if str(sms_record["failure_reason"] or "").isdigit() else None,
                             "delivery_status": actual_status},
                )
            enqueue_sheet_update(
                conn,
                lead_id=str(event["lead_id"]),
                event_type="sms_settled",
                source_key=f"{sid}:{actual_status}",
                outreach_event_id=event["id"],
            )
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
