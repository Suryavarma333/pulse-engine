from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, *, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0 or burst <= 0:
            raise ValueError("rate_per_second and burst must be positive")
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated_at
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated_at = now
            if self._tokens < 1:
                return False
            self._tokens -= 1
            return True
