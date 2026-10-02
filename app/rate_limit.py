"""Small in-process sliding-window rate limiter.

Good enough for a single instance. With several replicas this should move to
Redis (e.g. INCR + EXPIRE per key) so all instances share the counters.
"""
import math
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.config import get_settings
from app.errors import RateLimited


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, math.ceil(hits[0] + window_seconds - now))
                raise RateLimited(
                    "Too many attempts, try again later",
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = SlidingWindowRateLimiter()


def limit_login_attempts(request: Request) -> None:
    settings = get_settings()
    client = request.client.host if request.client else "unknown"
    login_limiter.hit(f"login:{client}", settings.login_rate_limit, settings.login_rate_window_seconds)
