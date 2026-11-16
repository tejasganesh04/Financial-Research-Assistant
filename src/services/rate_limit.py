"""Rate limits.

- `SlidingWindowLimiter`: in this process, at most `limit` events per key in any `window` seconds.
  Used for sign-in attempts per IP address and MCP tool calls per user, where a restart resetting
  the count does no harm.
- `quota_check`: fixed windows counted in PostgreSQL, so they survive restarts and are shared by
  every API process. Used for questions to the assistant (hourly and daily).
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from src.services import user_store


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 10_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> int | None:
        """Record an event; return None if allowed, or the seconds to wait if over the limit."""
        if self.limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            if len(self._events) > self.max_keys:
                self._forget_idle(now)
            events = self._events[key]
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.limit:
                return max(1, math.ceil(self.window - (now - events[0])))
            events.append(now)
            return None

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)

    def _forget_idle(self, now: float) -> None:
        for key in [k for k, v in self._events.items() if not v or v[-1] <= now - self.window]:
            del self._events[key]


class QuotaExceeded(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


def _wait_text(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = math.ceil(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes"
    return f"{math.ceil(minutes / 60)} hours"


async def quota_check(pool, key: str, limits: list[tuple[int, int, str]]) -> None:
    """Count one event against each (limit, window_seconds, label) quota, shortest window first.

    Raises QuotaExceeded for the first quota that is over its limit; refused requests still count
    in the windows checked so far, so hammering a full quota does not help.
    """
    for limit, window, label in limits:
        if limit <= 0:
            continue
        count, ends = await user_store.hit_counter(pool, f"{key}:{window}", window)
        if count > limit:
            wait = max(1, math.ceil((ends - datetime.now(timezone.utc)).total_seconds()))
            raise QuotaExceeded(
                f"You have reached the limit of {limit} {label}. Try again in {_wait_text(wait)}.", wait
            )
