"""Rate-limit primitives — string parser + sliding-window counter.

The hub uses these to enforce per-api-key request limits parsed from a
publisher's manifest.rate_limit field. In-memory, lost on restart, fine
for v0 — clients retry, no persistent damage.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Callable, Optional


_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(s|sec|second|m|min|minute|h|hour|d|day)\s*$", re.I)
_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "second": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0,
    "h": 3600.0, "hour": 3600.0,
    "d": 86400.0, "day": 86400.0,
}

DEFAULT_RATE = (60, 60.0)


def parse_rate(text: Optional[str]) -> tuple[int, float]:
    """Parse a rate string like '60/min' into (limit, period_seconds).

    Falls back to (60, 60.0) for None, empty, or malformed input — the hub
    needs a default rather than refusing service when a publisher omits the
    field. Operators who want unmetered access publish with a deliberately
    high rate (e.g., '1000000/hour').
    """
    if not text:
        return DEFAULT_RATE
    m = _RATE_RE.match(text)
    if not m:
        return DEFAULT_RATE
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n, _UNIT_SECONDS.get(unit, 60.0)


class SlidingWindow:
    """Per-key sliding-window counter.

    Each key maps to a deque of timestamps. On check(), expired timestamps
    are dropped first; if the live count is under the limit, the new hit is
    recorded and check returns (True, None). Otherwise check returns
    (False, retry_after_seconds_until_oldest_hit_expires).
    """

    def __init__(
        self,
        limit: int,
        period_seconds: float,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.period = period_seconds
        self._now = now_fn
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, Optional[float]]:
        now = self._now()
        cutoff = now - self.period
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) < self.limit:
            bucket.append(now)
            return True, None
        # A non-positive limit (e.g. a publisher declaring "0/min") rejects
        # every request and never appends, so the bucket is empty here — there
        # is no oldest hit to expire against. Retry can only mean "after a full
        # period", and we must not index an empty deque.
        retry_after = (bucket[0] + self.period - now) if bucket else self.period
        return False, max(0.0, retry_after)

    def clear(self, key: str) -> None:
        self._buckets.pop(key, None)
