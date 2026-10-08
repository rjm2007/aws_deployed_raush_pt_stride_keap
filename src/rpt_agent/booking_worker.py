"""Keep the voice-agent slot cache fresh.

Every BOOKING_SYNC_SECONDS: expire stale slot holds, then refresh every
location + visit length from Stride. Separate process from the cadence worker,
so a slow Stride never delays a patient call. Idles when nothing is set up.
"""

from __future__ import annotations

import logging
import time

from psycopg.types.json import Jsonb

from .config import get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .services.availability_sync import run_sync

SERVICE = "booking-worker"

logger = logging.getLogger(__name__)


def run_once() -> dict:
    # Syncing is idempotent (upserts), so a second container would only waste
    # Stride requests, not corrupt the cache.
    trace = WorkflowTrace("availability_sync", SERVICE)
    result = run_sync(trace)
    with transaction() as conn:
        conn.execute(
            "insert into service_heartbeats(service,last_seen_at,details) values(%s,now(),%s) "
            "on conflict(service) do update set last_seen_at=now(),details=excluded.details",
            (SERVICE, Jsonb({k: v for k, v in result.items() if k != "by_location"})),
        )
    trace.complete(outcome="ok" if not result["errors"] else "partial")
    return result


def main() -> None:
    configure_logging(SERVICE)
    settings = get_settings()
    errors = settings.runtime_errors(SERVICE)
    if errors:
        raise SystemExit("; ".join(errors))
    while True:
        try:
            run_once()
        except Exception:  # keep the loop alive; the next tick retries
            logger.exception("booking sync tick failed")
        time.sleep(settings.booking_sync_seconds)


if __name__ == "__main__":
    main()
