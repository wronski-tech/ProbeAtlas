"""In-process token-bucket rate limiter.

Limits are enforced per API process. With N API replicas the effective limit is
N times higher; move the buckets to PostgreSQL or a shared cache before
scaling out horizontally.
"""

from __future__ import annotations

import threading
import time

from .errors import APIError


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, per_minute: int) -> None:
        now = time.monotonic()
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens < 1:
                retry = int((1 - tokens) / rate) + 1
                raise APIError(
                    429,
                    "rate_limited",
                    "rate limit exceeded",
                    {"limit_per_minute": per_minute, "retry_after_s": retry},
                    headers={"Retry-After": str(retry)},
                )
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > 50_000:
                self._buckets.clear()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = RateLimiter()
