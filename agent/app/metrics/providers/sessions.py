from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

from common.contracts import SignalReading
from common.enums import ProviderStatus
from common.time import ensure_utc

SessionFetcher = Callable[[], Awaitable[Mapping[str, float | datetime]]]


class SessionLoginSignalProvider:
    """Adapts a bounded session/login aggregate fetcher into the provider contract."""

    name = "sessions"
    required = False

    def __init__(
        self,
        fetcher: SessionFetcher,
        *,
        timeout_seconds: float,
        freshness_limit_seconds: float,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.freshness_limit_seconds = freshness_limit_seconds
        self._fetcher = fetcher

    async def read(self, now: datetime) -> SignalReading:
        now = ensure_utc(now, field_name="now")
        payload = await asyncio.wait_for(self._fetcher(), timeout=self.timeout_seconds)
        raw_observed_at = payload.get("observed_at", now)
        if not isinstance(raw_observed_at, datetime):
            raise ValueError("session observed_at must be a datetime")
        observed_at = ensure_utc(raw_observed_at, field_name="observed_at")
        freshness = max(0.0, (now - observed_at).total_seconds())
        values = {
            key: float(payload[key])
            for key in ("concurrent_sessions", "login_rate_rps")
            if key in payload
        }
        if not values:
            raise ValueError("session provider returned no supported values")
        return SignalReading(
            source=self.name,
            observed_at=observed_at,
            status=(
                ProviderStatus.HEALTHY
                if freshness <= self.freshness_limit_seconds
                else ProviderStatus.STALE
            ),
            values=values,
            freshness_seconds=freshness,
            details={},
        )


__all__ = ["SessionFetcher", "SessionLoginSignalProvider"]
