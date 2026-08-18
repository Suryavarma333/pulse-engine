from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from common.contracts import SignalReading
from common.enums import ProviderStatus
from common.time import ensure_utc


class DemoAppSignalProvider:
    """Reads the protected application's aggregate snapshot without touching request paths."""

    name = "demo_app"
    required = True

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        freshness_limit_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.freshness_limit_seconds = freshness_limit_seconds
        self._url = f"{base_url.rstrip('/')}/metrics/snapshot"
        self._client = client

    async def read(self, now: datetime) -> SignalReading:
        now = ensure_utc(now, field_name="now")
        if self._client is not None:
            response = await asyncio.wait_for(
                self._client.get(self._url), timeout=self.timeout_seconds
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await asyncio.wait_for(
                    client.get(self._url), timeout=self.timeout_seconds
                )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        observed_at = ensure_utc(
            datetime.fromisoformat(payload["observed_at"]), field_name="observed_at"
        )
        freshness = max(0.0, (now - observed_at).total_seconds())
        checkout = payload.get("endpoints", {}).get("checkout", {})
        values = {
            "origin_request_rate_rps": float(payload["origin_request_rate_rps"]),
            "request_count": float(payload.get("request_count", 0)),
            "concurrent_requests": float(payload.get("concurrent_requests", 0)),
            "error_rate": float(payload.get("error_rate", 0)),
            "p50_latency_ms": float(payload.get("p50_latency_ms", 0)),
            "p95_latency_ms": float(payload.get("p95_latency_ms", 0)),
            "p99_latency_ms": float(payload.get("p99_latency_ms", 0)),
            "checkout_p99_latency_ms": float(checkout.get("p99_latency_ms", 0)),
            "checkout_success_rate": float(checkout.get("success_rate", 0)),
            "load_shedding_level": float(payload.get("load_shedding_level", 0)),
        }
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
            details={
                "persistence_available": bool(payload.get("persistence", {}).get("available")),
                "samples_truncated": bool(payload.get("samples_truncated", False)),
            },
        )


__all__ = ["DemoAppSignalProvider"]
