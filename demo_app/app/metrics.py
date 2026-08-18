from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

_TRACKED_PATHS: Final[dict[str, str]] = {
    "/checkout": "checkout",
    "/recommendations": "recommendations",
    "/catalog": "catalog",
}
_OTHER_ENDPOINT: Final = "other"


@dataclass(frozen=True, slots=True)
class RequestSample:
    completed_at: float
    endpoint: str
    latency_ms: float
    status_code: int


class TrafficMetrics:
    """A bounded in-process metrics window used only by the demo application's HTTP layer."""

    def __init__(self, window_seconds: int, max_samples: int = 20_000) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self.window_seconds = window_seconds
        self.max_samples = max_samples
        self._samples: deque[RequestSample] = deque(maxlen=max_samples)
        self._in_flight = 0
        self._samples_dropped = 0
        self._last_dropped_at: float | None = None
        self._started_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def request_started(self) -> None:
        async with self._lock:
            self._in_flight += 1

    async def request_finished(self, *, path: str, latency_ms: float, status_code: int) -> None:
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            now = time.monotonic()
            self._drop_expired(now)
            if len(self._samples) == self.max_samples:
                self._samples_dropped += 1
                self._last_dropped_at = now
            endpoint = _TRACKED_PATHS.get(path, _OTHER_ENDPOINT)
            self._samples.append(RequestSample(now, endpoint, latency_ms, status_code))

    async def snapshot(self) -> dict:
        async with self._lock:
            now = time.monotonic()
            self._drop_expired(now)
            samples = list(self._samples)
            in_flight = self._in_flight
            samples_dropped = self._samples_dropped

        latencies = sorted(sample.latency_ms for sample in samples)
        errors = sum(sample.status_code >= 500 for sample in samples)
        elapsed = max(1.0, min(float(self.window_seconds), now - self._started_at))
        endpoint_samples: dict[str, list[RequestSample]] = defaultdict(list)
        for sample in samples:
            endpoint_samples[sample.endpoint].append(sample)
        endpoints = {
            endpoint: self._endpoint_snapshot(endpoint_samples.get(endpoint, []))
            for endpoint in (*_TRACKED_PATHS.values(), _OTHER_ENDPOINT)
        }
        return {
            "observed_at": datetime.now(UTC),
            "window_seconds": self.window_seconds,
            "origin_request_rate_rps": len(samples) / elapsed,
            "concurrent_requests": in_flight,
            "request_count": len(samples),
            "sample_capacity": self.max_samples,
            "samples_dropped": samples_dropped,
            "samples_truncated": samples_dropped > 0,
            "error_rate": errors / len(samples) if samples else 0.0,
            "p50_latency_ms": self._percentile(latencies, 0.50),
            "p95_latency_ms": self._percentile(latencies, 0.95),
            "p99_latency_ms": self._percentile(latencies, 0.99),
            "requests_by_path": {
                f"/{endpoint}": metrics["request_count"]
                for endpoint, metrics in endpoints.items()
                if endpoint != _OTHER_ENDPOINT and metrics["request_count"]
            },
            "endpoints": endpoints,
        }

    def _drop_expired(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0].completed_at < cutoff:
            self._samples.popleft()
        if self._last_dropped_at is not None and self._last_dropped_at < cutoff:
            self._samples_dropped = 0
            self._last_dropped_at = None

    @classmethod
    def _endpoint_snapshot(cls, samples: list[RequestSample]) -> dict[str, float | int]:
        latencies = sorted(sample.latency_ms for sample in samples)
        successes = sum(200 <= sample.status_code < 400 for sample in samples)
        return {
            "request_count": len(samples),
            "p99_latency_ms": cls._percentile(latencies, 0.99),
            "success_rate": successes / len(samples) if samples else 0.0,
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        index = max(0, math.ceil(percentile * len(values)) - 1)
        return round(values[index], 3)
