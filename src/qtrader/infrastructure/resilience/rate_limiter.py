"""In-process async token-bucket rate limiter.

Puts a ceiling on calls made to external providers / public endpoints so a
bug or a burst never hammers an upstream. Buckets are per-instance; pair with
a Redis-based limiter when rate limits must be shared across workers.
"""

from __future__ import annotations

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


class TokenBucket:
    """Token bucket with continuous refill.

    - ``capacity`` — maximum tokens the bucket can hold.
    - ``refill_rate_per_second`` — tokens added per second (steady-state rate).
    """

    def __init__(self, capacity: float, refill_rate_per_second: float, *, name: str = "") -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_rate_per_second < 0:
            raise ValueError("refill_rate_per_second must be >= 0")
        self.name = name
        self._capacity = float(capacity)
        self._rate = float(refill_rate_per_second)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> bool:
        """Try to take ``tokens``; returns False when the budget is exhausted."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        async with self._lock:
            self._refill()
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True

    async def wait(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them."""
        while not await self.acquire(tokens):  # noqa: ASYNC110 (polling a token bucket)
            await asyncio.sleep(self._seconds_to_next(tokens))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    def _seconds_to_next(self, tokens: float) -> float:
        deficit = tokens - self._tokens
        if self._rate <= 0:
            return 1.0
        return max(0.0, deficit / self._rate)

    @property
    def available(self) -> float:
        """Approximate available tokens (lock-free read for diagnostics)."""
        self._refill()
        return self._tokens
