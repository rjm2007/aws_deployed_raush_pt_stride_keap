"""Keep a local copy of every clinician's open Stride slots.

Cached availability plus a regular re-sync is how booking tools stay fast: a
live call reads the cache instead of waiting on Stride, and the cache is only
ever used to decide what to offer. The booking itself re-checks the one chosen
slot live, so a stale cache can cost a re-offer but never a double booking.

One Stride request per location + visit length covers every clinician who works
there (Stride returns clinicians who have no hours at a location as empty lists,
so asking for all of them is harmless). Slots the next sync no longer sees are
marked gone; slots we booked stay booked.

The cache holds BOOKING_CACHE_DAYS (90) days, but Stride answers at most 31 days per
request, so each location + visit length is fetched in chunks of 31 days: chunk 0
(the first 31 days, where every default search looks) every BOOKING_SYNC_SECONDS,
the later chunks every BOOKING_FAR_SYNC_SECONDS and again on a new day (their
windows move with "today"). Each chunk has its own availability_sync_state row.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError

# Stride allows 5 requests per second per user; stay well under it.
STRIDE_REQUEST_GAP_SECONDS = 0.25
# Stride's availability endpoint answers at most 31 days per request (Stride docs).
STRIDE_MAX_DAYS = 31

SYNC_TARGETS_SQL = (
    "select bl.id as booking_location_id,bl.practice_id,bl.stride_location_id,bl.timezone,"
    "ct.duration_minutes,array_agg(distinct c.stride_user_id order by c.stride_user_id) "
    "as stride_user_ids from clinician_assignments ca "
    "join clinicians c on c.id=ca.clinician_id and c.is_active "
    "join booking_locations bl on bl.id=ca.booking_location_id and bl.is_active "
    "join case_types ct on ct.id=ca.case_type_id and ct.is_active "
    "where ca.is_active {extra} "
    "group by bl.id,bl.practice_id,bl.stride_location_id,bl.timezone,ct.duration_minutes "
    "order by bl.id,ct.duration_minutes"
)


def booking_now(settings: Settings, timezone: str) -> datetime:
    """The current time at the clinic, or the same wall-clock time on the pinned test day."""
    now = datetime.now(ZoneInfo(timezone))
    if settings.booking_today_override:
        return datetime.combine(settings.booking_today_override, now.time(), ZoneInfo(timezone))
    return now


@dataclass(frozen=True)
class Chunk:
    index: int
    start: date
    end: date


def cache_chunks(today: date, cache_days: int) -> list[Chunk]:
    """The cache window from today, cut into Stride requests of at most STRIDE_MAX_DAYS days."""
    chunks = []
    for index, offset in enumerate(range(0, cache_days, STRIDE_MAX_DAYS)):
        days = min(STRIDE_MAX_DAYS, cache_days - offset)
        start = today + timedelta(days=offset)
        chunks.append(Chunk(index, start, start + timedelta(days=days - 1)))
    return chunks


def chunk_max_age(settings: Settings, index: int) -> timedelta:
    """How old a chunk may be before a search refreshes it itself.

    A later chunk is only refreshed every BOOKING_FAR_SYNC_SECONDS, so it gets that much more
    slack than the first one; otherwise every far search would wait on Stride.
    """
    seconds = settings.booking_cache_max_age_seconds
    if index:
        seconds += settings.booking_far_sync_seconds
    return timedelta(seconds=seconds)


def sync_targets(
    conn, booking_location_id: int | None = None, practice_id: int | None = None,
) -> list[dict[str, Any]]:
    filters, params = [], []
    if booking_location_id is not None:
        filters.append("and bl.id=%s")
        params.append(booking_location_id)
    if practice_id is not None:
        filters.append("and bl.practice_id=%s")
        params.append(practice_id)
    return conn.execute(SYNC_TARGETS_SQL.format(extra=" ".join(filters)), params).fetchall()


def sync_target(
    trace: WorkflowTrace,
    target: dict[str, Any],
    *,
    providers: ProviderClients,
    settings: Settings,
    chunks: list[int] | None = None,
) -> int:
    """Refresh one location + visit length (only the given chunk numbers, else all).

    Returns the number of open slots seen. Raises the first ProviderError after trying every chunk.
    """
    today = booking_now(settings, target["timezone"]).date()
    failure: ProviderError | None = None
    count = 0
    for number, chunk in enumerate(
        chunk for chunk in cache_chunks(today, settings.booking_cache_days)
        if chunks is None or chunk.index in chunks
    ):
        if number:
            time.sleep(STRIDE_REQUEST_GAP_SECONDS)
        try:
            count += _sync_chunk(trace, target, chunk, providers=providers)
        except ProviderError as exc:
            failure = failure or exc
    if failure:
        raise failure
    return count


def _sync_chunk(
    trace: WorkflowTrace, target: dict[str, Any], chunk: Chunk, *, providers: ProviderClients,
) -> int:
    window_start, window_end = chunk.start, chunk.end
    try:
        slots = providers.stride_availability(
            trace,
            location=target["stride_location_id"],
            duration=target["duration_minutes"],
            clinician_ids=",".join(str(value) for value in target["stride_user_ids"]),
            start_date=window_start,
            end_date=window_end,
        )
    except ProviderError as exc:
        with transaction() as conn:
            _record_sync(conn, target, chunk, error=str(exc)[:500])
        raise
    with transaction() as conn:
        clinicians = {
            row["stride_user_id"]: row["id"]
            for row in conn.execute(
                "select id,stride_user_id from clinicians where practice_id=%s",
                (target["practice_id"],),
            ).fetchall()
        }
        rows = []
        for slot in slots:
            clinician_id = clinicians.get(slot.clinician_id)
            if clinician_id is None:
                continue
            local = datetime.fromisoformat(f"{slot.local_date}T{slot.local_time}").replace(
                tzinfo=ZoneInfo(slot.timezone)
            )
            start_utc = local.astimezone(UTC)
            rows.append((
                target["practice_id"], clinician_id, target["booking_location_id"],
                target["duration_minutes"], start_utc,
                start_utc + timedelta(minutes=target["duration_minutes"]),
                local.date(), local.time(), slot.timezone,
            ))
        with conn.cursor() as cursor:
            cursor.executemany(
                "insert into availability_slots(practice_id,clinician_id,booking_location_id,"
                "duration_minutes,start_utc,end_utc,local_date,local_time,timezone) "
                "values(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "on conflict(clinician_id,booking_location_id,duration_minutes,start_utc) do update "
                "set last_seen_at=now(),status=case when availability_slots.status='gone' "
                "then 'open' else availability_slots.status end,"
                "updated_at=case when availability_slots.status='gone' then now() "
                "else availability_slots.updated_at end",
                rows,
            )
        # Anything in the window this sync did not see has been taken or removed.
        # Every row seen above got last_seen_at=now(), the transaction's own
        # timestamp, so comparing to it avoids app/database clock drift.
        conn.execute(
            "update availability_slots set status='gone',updated_at=now() "
            "where booking_location_id=%s and duration_minutes=%s and status='open' "
            "and local_date between %s and %s and last_seen_at<now()",
            (target["booking_location_id"], target["duration_minutes"], window_start, window_end),
        )
        _record_sync(conn, target, chunk, slot_count=len(rows))
    trace.log(
        "availability_synced",
        booking_location_id=target["booking_location_id"],
        duration_minutes=target["duration_minutes"],
        chunk=chunk.index,
        slot_count=len(rows),
    )
    return len(rows)


def _record_sync(
    conn, target: dict[str, Any], chunk: Chunk, *, slot_count: int = 0, error: str | None = None,
) -> None:
    conn.execute(
        "insert into availability_sync_state(booking_location_id,duration_minutes,chunk,"
        "window_start,window_end,last_success_at,last_attempt_at,last_error,slot_count) "
        "values(%s,%s,%s,%s,%s,case when %s::text is null then now() end,now(),%s,%s) "
        "on conflict(booking_location_id,duration_minutes,chunk) do update set "
        "window_start=case when excluded.last_error is null then excluded.window_start "
        "else availability_sync_state.window_start end,"
        "window_end=case when excluded.last_error is null then excluded.window_end "
        "else availability_sync_state.window_end end,"
        "last_success_at=coalesce(excluded.last_success_at,availability_sync_state.last_success_at),"
        "last_attempt_at=now(),last_error=excluded.last_error,"
        "slot_count=case when excluded.last_error is null then excluded.slot_count "
        "else availability_sync_state.slot_count end",
        (target["booking_location_id"], target["duration_minutes"], chunk.index, chunk.start,
         chunk.end, error, error, slot_count),
    )


def due_chunks(conn, target: dict[str, Any], settings: Settings, now: datetime | None = None,
               ) -> list[int]:
    """Chunks the background sync refreshes now: chunk 0 always; a later chunk on a new day (its
    window moved), after a failure, or when BOOKING_FAR_SYNC_SECONDS have passed."""
    today = booking_now(settings, target["timezone"]).date()
    now = now or datetime.now(UTC)
    state = {
        row["chunk"]: row for row in conn.execute(
            "select chunk,window_start,last_success_at,last_error from availability_sync_state "
            "where booking_location_id=%s and duration_minutes=%s",
            (target["booking_location_id"], target["duration_minutes"]),
        ).fetchall()
    }
    due = []
    for chunk in cache_chunks(today, settings.booking_cache_days):
        row = state.get(chunk.index)
        if (
            chunk.index == 0 or row is None or row["last_error"] or row["last_success_at"] is None
            or row["window_start"] != chunk.start
            or now - row["last_success_at"] >= timedelta(seconds=settings.booking_far_sync_seconds)
        ):
            due.append(chunk.index)
    return due


def expire_holds(conn) -> int:
    return len(conn.execute(
        "update slot_holds set status='expired',released_at=now() "
        "where status='active' and expires_at<now() returning id"
    ).fetchall())


def run_sync(
    trace: WorkflowTrace | None = None,
    providers: ProviderClients | None = None,
    settings: Settings | None = None,
    practice_id: int | None = None,
) -> dict[str, Any]:
    """Refresh every location + visit length that has an active clinician (the chunks due now)."""
    settings = settings or get_settings()
    providers = providers or ProviderClients(settings)
    trace = trace or WorkflowTrace("availability_sync", "booking-worker")
    with transaction() as conn:
        expired = expire_holds(conn)
        targets = sync_targets(conn, practice_id=practice_id)
        plan = [(target, due_chunks(conn, target, settings)) for target in targets]
    result: dict[str, Any] = {"targets": len(targets), "slots": 0, "errors": 0,
                              "requests": sum(len(chunks) for _, chunks in plan),
                              "holds_expired": expired, "by_location": defaultdict(int)}
    for index, (target, chunks) in enumerate(plan):
        if index:
            time.sleep(STRIDE_REQUEST_GAP_SECONDS)
        try:
            count = sync_target(trace, target, providers=providers, settings=settings,
                                chunks=chunks)
        except ProviderError:
            result["errors"] += 1
            continue
        result["slots"] += count
        result["by_location"][target["booking_location_id"]] += count
    result["by_location"] = dict(result["by_location"])
    return result
