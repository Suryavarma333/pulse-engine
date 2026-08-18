from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from agent.app.metrics.providers.base import SignalProvider
from common.contracts import CapacityState, SignalReading
from common.enums import ProviderStatus
from common.time import ensure_utc
from db.models import TrafficSnapshot


class RequiredSignalUnavailable(RuntimeError):
    def __init__(self, provider_health: dict[str, dict[str, Any]]) -> None:
        super().__init__("required origin signal is unavailable")
        self.provider_health = provider_health


class SnapshotWriter(Protocol):
    async def add(self, snapshot: TrafficSnapshot) -> TrafficSnapshot: ...


class CapacityReader(Protocol):
    async def read_capacity(self, *, observed_at: datetime) -> Any: ...


@dataclass(frozen=True, slots=True)
class CollectedSignals:
    observed_at: datetime
    origin_observed_at: datetime
    origin_request_rate_rps: float
    request_count: int
    concurrent_requests: int
    error_rate: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    checkout_p99_latency_ms: float | None
    checkout_success_rate: float | None
    load_shedding_level: int
    optional_values: dict[str, float]
    provider_health: dict[str, dict[str, Any]]
    capacity_state: CapacityState | None = None
    capacity_per_instance_rps: float | None = None
    demo_run_id: UUID | None = None

    @property
    def all_values(self) -> dict[str, float]:
        return {"origin_request_rate_rps": self.origin_request_rate_rps, **self.optional_values}

    def to_snapshot(
        self,
        *,
        window_seconds: int,
        baseline_request_rate_rps: float,
        request_rate_change_rps: float,
        request_acceleration_rps2: float,
        queue_growth_per_second: float | None,
        reactive_comparator_crossed: bool,
        evidence: dict[str, Any],
    ) -> TrafficSnapshot:
        values = self.optional_values
        return TrafficSnapshot(
            environment=evidence["environment"],
            demo_run_id=self.demo_run_id,
            observed_at=self.observed_at,
            window_seconds=window_seconds,
            origin_request_rate_rps=self.origin_request_rate_rps,
            baseline_request_rate_rps=baseline_request_rate_rps,
            request_count=self.request_count,
            concurrent_requests=self.concurrent_requests,
            request_rate_change_rps=request_rate_change_rps,
            request_acceleration_rps2=request_acceleration_rps2,
            edge_request_rate_rps=values.get("edge_request_rate_rps"),
            queue_depth=_optional_int(values.get("queue_depth")),
            queue_growth_per_second=queue_growth_per_second,
            concurrent_sessions=_optional_int(values.get("concurrent_sessions")),
            login_rate_rps=values.get("login_rate_rps"),
            cpu_utilization_pct=values.get("cpu_utilization_pct"),
            p50_latency_ms=self.p50_latency_ms,
            p95_latency_ms=self.p95_latency_ms,
            p99_latency_ms=self.p99_latency_ms,
            checkout_p99_latency_ms=self.checkout_p99_latency_ms,
            checkout_success_rate=self.checkout_success_rate,
            error_rate=self.error_rate,
            asg_desired_capacity=(
                None if self.capacity_state is None else self.capacity_state.desired
            ),
            asg_in_service_capacity=(
                None if self.capacity_state is None else self.capacity_state.in_service
            ),
            capacity_per_instance_rps=self.capacity_per_instance_rps,
            load_shedding_level=self.load_shedding_level,
            reactive_comparator_crossed=reactive_comparator_crossed,
            signal_details=evidence,
        )


def _optional_int(value: float | None) -> int | None:
    return None if value is None else int(value)


class CompositeSignalCollector:
    """Collects providers concurrently and persists exactly the evaluated aggregate supplied."""

    def __init__(
        self,
        providers: list[SignalProvider],
        snapshot_writer: SnapshotWriter,
        *,
        origin_provider_name: str = "demo_app",
        omitted_providers: tuple[str, ...] = (),
        capacity_reader: CapacityReader | None = None,
        capacity_per_instance_rps: float | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        if len(providers) > 32:
            raise ValueError("at most 32 providers may be configured")
        if len({provider.name for provider in providers}) != len(providers):
            raise ValueError("provider names must be unique")
        self._providers = tuple(providers)
        self._writer = snapshot_writer
        self._origin_name = origin_provider_name
        if capacity_per_instance_rps is not None and capacity_per_instance_rps <= 0:
            raise ValueError("capacity_per_instance_rps must be positive")
        self._capacity_reader = capacity_reader
        self._capacity_per_instance_rps = capacity_per_instance_rps
        configured_names = {provider.name for provider in providers}
        if configured_names.intersection(omitted_providers):
            raise ValueError("configured providers cannot also be marked omitted")
        self._omitted_providers = tuple(sorted(set(omitted_providers)))
        if origin_provider_name not in {provider.name for provider in providers}:
            raise ValueError("origin provider is required")

    @property
    def provider_configuration(self) -> dict[str, dict[str, Any]]:
        configured = {
            provider.name: {
                "status": ProviderStatus.DEGRADED.value,
                "included": False,
                "details": {"reason": "awaiting_first_read"},
            }
            for provider in self._providers
        }
        configured.update(
            {
                name: {
                    "status": ProviderStatus.UNAVAILABLE.value,
                    "included": False,
                    "details": {"reason": "not_configured"},
                }
                for name in self._omitted_providers
            }
        )
        return configured

    async def collect(
        self, *, now: datetime, demo_run_id: UUID | None = None
    ) -> CollectedSignals:
        now = ensure_utc(now, field_name="now")
        results = await asyncio.gather(
            *(self._safe_read(provider, now) for provider in self._providers)
        )
        readings = {reading.source: reading for reading in results}
        health = {
            provider.name: self._health(
                readings[provider.name], included=self._is_fresh(readings[provider.name])
            )
            for provider in self._providers
        }
        health.update(
            {
                name: {
                    "status": ProviderStatus.UNAVAILABLE.value,
                    "included": False,
                    "details": {"reason": "not_configured"},
                }
                for name in self._omitted_providers
            }
        )
        origin = readings[self._origin_name]
        if not self._is_fresh(origin) or "origin_request_rate_rps" not in origin.values:
            raise RequiredSignalUnavailable(health)

        optional_values: dict[str, float] = {}
        collisions: list[str] = []
        for provider in self._providers:
            if provider.name == self._origin_name:
                continue
            reading = readings[provider.name]
            if not self._is_fresh(reading):
                continue
            for key, value in reading.values.items():
                if key in optional_values:
                    collisions.append(key)
                    continue
                optional_values[key] = value
        if collisions:
            health["collector"] = {
                "status": ProviderStatus.DEGRADED.value,
                "collisions": sorted(set(collisions)),
            }

        capacity_state: CapacityState | None = None
        if self._capacity_reader is not None:
            try:
                capacity = await self._capacity_reader.read_capacity(observed_at=now)
                capacity_state = capacity.state
                health["capacity"] = {
                    "status": capacity_state.provider_status.value,
                    "included": True,
                    "details": {
                        "desired": capacity_state.desired,
                        "in_service": capacity_state.in_service,
                        "pending": capacity_state.pending,
                    },
                }
            except Exception as exc:
                health["capacity"] = {
                    "status": ProviderStatus.UNAVAILABLE.value,
                    "included": False,
                    "details": {"reason": f"capacity_error:{type(exc).__name__}"},
                }

        return CollectedSignals(
            observed_at=now,
            origin_observed_at=origin.observed_at,
            origin_request_rate_rps=origin.values["origin_request_rate_rps"],
            request_count=int(origin.values.get("request_count", 0)),
            concurrent_requests=int(origin.values.get("concurrent_requests", 0)),
            error_rate=origin.values.get("error_rate"),
            p50_latency_ms=origin.values.get("p50_latency_ms"),
            p95_latency_ms=origin.values.get("p95_latency_ms"),
            p99_latency_ms=origin.values.get("p99_latency_ms"),
            checkout_p99_latency_ms=origin.values.get("checkout_p99_latency_ms"),
            checkout_success_rate=origin.values.get("checkout_success_rate"),
            load_shedding_level=int(origin.values.get("load_shedding_level", 0)),
            optional_values=optional_values,
            provider_health=health,
            capacity_state=capacity_state,
            capacity_per_instance_rps=self._capacity_per_instance_rps,
            demo_run_id=demo_run_id,
        )

    async def persist(self, snapshot: TrafficSnapshot) -> TrafficSnapshot:
        return await self._writer.add(snapshot)

    async def _safe_read(self, provider: SignalProvider, now: datetime) -> SignalReading:
        try:
            return await asyncio.wait_for(provider.read(now), timeout=provider.timeout_seconds)
        except TimeoutError:
            reason = "timeout"
        except Exception as exc:  # provider isolation is an explicit resilience boundary
            reason = f"provider_error:{type(exc).__name__}"
        return SignalReading(
            source=provider.name,
            observed_at=now,
            status=ProviderStatus.UNAVAILABLE,
            values={},
            freshness_seconds=0,
            details={"reason": reason},
        )

    def _is_fresh(self, reading: SignalReading) -> bool:
        provider = next(item for item in self._providers if item.name == reading.source)
        return (
            reading.status in {ProviderStatus.HEALTHY, ProviderStatus.SIMULATED}
            and reading.freshness_seconds <= provider.freshness_limit_seconds
        )

    @staticmethod
    def _health(reading: SignalReading, *, included: bool) -> dict[str, Any]:
        details = reading.details
        if len(json.dumps(details, separators=(",", ":")).encode()) > 1_024:
            details = {"truncated": True, "keys": sorted(details)[:16]}
        return {
            "status": reading.status.value,
            "observed_at": reading.observed_at.isoformat(),
            "freshness_seconds": round(reading.freshness_seconds, 3),
            "included": bool(reading.values) and included,
            "details": details,
        }


__all__ = [
    "CollectedSignals",
    "CompositeSignalCollector",
    "RequiredSignalUnavailable",
    "SnapshotWriter",
]
