from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

import httpx

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


def http_session_fetcher(
    client: httpx.AsyncClient,
    url: str,
) -> SessionFetcher:
    """Create the bounded HTTP aggregate fetcher used by production assembly."""

    async def fetch() -> Mapping[str, float | datetime]:
        response = await client.get(url)
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise ValueError("session signal response must be an object")
        values: dict[str, float | datetime] = {}
        observed_at = payload.get("observed_at")
        if isinstance(observed_at, str):
            values["observed_at"] = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            )
        elif isinstance(observed_at, datetime):
            values["observed_at"] = observed_at
        for key in ("concurrent_sessions", "login_rate_rps"):
            if key in payload:
                values[key] = float(payload[key])
        return values

    return fetch


__all__ = ["SessionFetcher", "SessionLoginSignalProvider", "http_session_fetcher"]
