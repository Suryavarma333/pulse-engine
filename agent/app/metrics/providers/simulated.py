from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field, model_validator

from common.contracts import ContractModel, SignalReading
from common.enums import ProviderStatus
from common.time import ensure_utc


class DuplicateSignalError(ValueError):
    pass


class ExpiredSignalError(ValueError):
    pass


class SignalBufferFullError(RuntimeError):
    pass


class SimulatedSignalFrame(ContractModel):
    source: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    demo_run_id: UUID | None = None
    ttl_seconds: int = Field(ge=1, le=3_600)
    edge_request_rate_rps: float | None = Field(default=None, ge=0, le=10_000_000)
    queue_depth: int | None = Field(default=None, ge=0, le=1_000_000_000)
    concurrent_sessions: int | None = Field(default=None, ge=0, le=10_000_000)
    login_rate_rps: float | None = Field(default=None, ge=0, le=10_000_000)
    cpu_utilization_pct: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_frame(self) -> SimulatedSignalFrame:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if all(
            value is None
            for value in (
                self.edge_request_rate_rps,
                self.queue_depth,
                self.concurrent_sessions,
                self.login_rate_rps,
                self.cpu_utilization_pct,
            )
        ):
            raise ValueError("at least one simulated signal value is required")
        return self

    @property
    def expires_at(self) -> datetime:
        return self.observed_at + timedelta(seconds=self.ttl_seconds)

    def values(self) -> dict[str, float]:
        return {
            key: float(value)
            for key, value in {
                "edge_request_rate_rps": self.edge_request_rate_rps,
                "queue_depth": self.queue_depth,
                "concurrent_sessions": self.concurrent_sessions,
                "login_rate_rps": self.login_rate_rps,
                "cpu_utilization_pct": self.cpu_utilization_pct,
            }.items()
            if value is not None
        }


class SimulatedSignalBuffer:
    """TTL-aware buffer that rejects overflow instead of silently discarding evidence."""

    def __init__(
        self,
        *,
        capacity: int,
        max_ttl_seconds: int = 300,
        future_tolerance_seconds: int = 5,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.max_ttl_seconds = max_ttl_seconds
        self.future_tolerance_seconds = future_tolerance_seconds
        self._frames: deque[SimulatedSignalFrame] = deque()
        self._keys: set[tuple[str, datetime, UUID | None]] = set()
        self._lock = asyncio.Lock()

    async def add(self, frame: SimulatedSignalFrame, *, now: datetime) -> SimulatedSignalFrame:
        now = ensure_utc(now, field_name="now")
        if frame.ttl_seconds > self.max_ttl_seconds:
            raise ValueError(f"ttl_seconds must not exceed {self.max_ttl_seconds}")
        if frame.observed_at > now + timedelta(seconds=self.future_tolerance_seconds):
            raise ValueError("observed_at is too far in the future")
        if frame.expires_at <= now:
            raise ExpiredSignalError("simulated signal has expired")
        async with self._lock:
            self._prune(now)
            key = self._key(frame)
            if key in self._keys:
                raise DuplicateSignalError("duplicate simulated signal")
            if len(self._frames) >= self.capacity:
                raise SignalBufferFullError("simulated signal buffer is full")
            self._frames.append(frame)
            self._keys.add(key)
        return frame

    async def latest(self, *, now: datetime) -> SimulatedSignalFrame | None:
        now = ensure_utc(now, field_name="now")
        async with self._lock:
            self._prune(now)
            if not self._frames:
                return None
            return max(self._frames, key=lambda item: item.observed_at)

    async def size(self, *, now: datetime) -> int:
        now = ensure_utc(now, field_name="now")
        async with self._lock:
            self._prune(now)
            return len(self._frames)

    def _prune(self, now: datetime) -> None:
        retained = deque(frame for frame in self._frames if frame.expires_at > now)
        self._frames = retained
        self._keys = {self._key(frame) for frame in retained}

    @staticmethod
    def _key(frame: SimulatedSignalFrame) -> tuple[str, datetime, UUID | None]:
        return frame.source, frame.observed_at, frame.demo_run_id


class SimulatedSignalProvider:
    name = "simulated"
    required = False

    def __init__(
        self,
        buffer: SimulatedSignalBuffer,
        *,
        timeout_seconds: float = 0.1,
        freshness_limit_seconds: float = 15,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.freshness_limit_seconds = freshness_limit_seconds
        self._buffer = buffer

    async def read(self, now: datetime) -> SignalReading:
        now = ensure_utc(now, field_name="now")
        frame = await asyncio.wait_for(self._buffer.latest(now=now), self.timeout_seconds)
        if frame is None:
            return SignalReading(
                source=self.name,
                observed_at=now,
                status=ProviderStatus.UNAVAILABLE,
                values={},
                freshness_seconds=0,
                details={"reason": "empty_buffer"},
            )
        freshness = max(0.0, (now - frame.observed_at).total_seconds())
        return SignalReading(
            source=self.name,
            observed_at=frame.observed_at,
            status=(
                ProviderStatus.SIMULATED
                if freshness <= self.freshness_limit_seconds
                else ProviderStatus.STALE
            ),
            values=frame.values(),
            freshness_seconds=freshness,
            details={
                "source": frame.source,
                "expires_at": frame.expires_at.isoformat(),
                "demo_run_id": str(frame.demo_run_id) if frame.demo_run_id else None,
            },
        )


__all__ = [
    "DuplicateSignalError",
    "ExpiredSignalError",
    "SignalBufferFullError",
    "SimulatedSignalBuffer",
    "SimulatedSignalFrame",
    "SimulatedSignalProvider",
]
