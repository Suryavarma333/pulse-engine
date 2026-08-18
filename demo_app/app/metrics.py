from __future__ import annotations

import asyncio
import math
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class RequestSample:
    completed_at: float
    path: str
    latency_ms: float
    status_code: int


class TrafficMetrics:
    def __init__(self, window_seconds: int) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._samples: deque[RequestSample] = deque()
        self._in_flight = 0
        self._started_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def request_started(self) -> None:
        async with self._lock:
            self._in_flight += 1

    async def request_finished(self, *, path: str, latency_ms: float, status_code: int) -> None:
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            now = time.monotonic()
            self._samples.append(RequestSample(now, path, latency_ms, status_code))
            self._drop_expired(now)

    async def snapshot(self) -> dict:
        async with self._lock:
            now = time.monotonic()
            self._drop_expired(now)
            samples = list(self._samples)
            in_flight = self._in_flight

        latencies = sorted(sample.latency_ms for sample in samples)
        errors = sum(sample.status_code >= 500 for sample in samples)
        elapsed = max(1.0, min(float(self.window_seconds), now - self._started_at))
        endpoint_counts = Counter(sample.path for sample in samples)
        return {
            "observed_at": datetime.now(UTC),
            "window_seconds": self.window_seconds,
            "origin_request_rate_rps": len(samples) / elapsed,
            "concurrent_requests": in_flight,
            "request_count": len(samples),
            "error_rate": errors / len(samples) if samples else 0.0,
            "p50_latency_ms": self._percentile(latencies, 0.50),
            "p95_latency_ms": self._percentile(latencies, 0.95),
            "p99_latency_ms": self._percentile(latencies, 0.99),
            "requests_by_path": dict(endpoint_counts),
        }

    def _drop_expired(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0].completed_at < cutoff:
            self._samples.popleft()

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        index = max(0, math.ceil(percentile * len(values)) - 1)
        return round(values[index], 3)
