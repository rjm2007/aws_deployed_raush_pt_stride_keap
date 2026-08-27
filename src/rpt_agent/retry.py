from __future__ import annotations

import math
import random

from .config import Settings, get_settings


def retry_delay_seconds(
    attempt: int,
    retry_after_seconds: int | None = None,
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    exponential = min(
        settings.retry_base_seconds * (2 ** max(0, attempt - 1)),
        settings.retry_max_seconds,
    )
    provider_delay = min(retry_after_seconds or 0, 86400)
    requested = max(exponential, provider_delay)
    jittered = requested * random.uniform(1.0, 1.25)
    maximum = max(settings.retry_max_seconds, provider_delay)
    return max(1, math.ceil(min(jittered, maximum)))
