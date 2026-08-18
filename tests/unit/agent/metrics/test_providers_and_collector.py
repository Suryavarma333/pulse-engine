from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agent.app.metrics.collector import CompositeSignalCollector, RequiredSignalUnavailable
from agent.app.metrics.providers.cloudwatch import CloudWatchMetricProvider
from agent.app.metrics.providers.demo_app import DemoAppSignalProvider
from agent.app.metrics.providers.sessions import SessionLoginSignalProvider
from agent.app.metrics.providers.simulated import (
    DuplicateSignalError,
    ExpiredSignalError,
    SignalBufferFullError,
    SimulatedSignalBuffer,
    SimulatedSignalFrame,
    SimulatedSignalProvider,
)
from agent.app.metrics.providers.sqs import SqsSignalProvider
from common.contracts import CapacityState, SignalReading
from common.enums import ProviderStatus

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


class MemorySnapshotWriter:
    def __init__(self) -> None:
        self.snapshots = []

    async def add(self, snapshot):
        snapshot.id = len(self.snapshots) + 1
        self.snapshots.append(snapshot)
        return snapshot


class FakeProvider:
    def __init__(
        self,
        name: str,
        reading: SignalReading | None = None,
        *,
        required: bool = False,
        delay: float = 0,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.required = required
        self.timeout_seconds = 0.01
        self.freshness_limit_seconds = 5
        self.reading = reading
        self.delay = delay
        self.error = error

    async def read(self, now: datetime) -> SignalReading:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        assert self.reading is not None
        return self.reading


def reading(
    source: str,
    values: dict[str, float],
    *,
    status: ProviderStatus = ProviderStatus.HEALTHY,
    freshness: float = 0,
) -> SignalReading:
    return SignalReading(
        source=source,
        observed_at=NOW - timedelta(seconds=freshness),
        status=status,
        values=values,
        freshness_seconds=freshness,
        details={},
    )


def test_collector_requires_origin_and_omits_stale_or_failed_optional_values() -> None:
    collector = CompositeSignalCollector(
        [
            FakeProvider(
                "demo_app",
                reading(
                    "demo_app",
                    {
                        "origin_request_rate_rps": 12,
                        "request_count": 120,
                        "checkout_success_rate": 1,
                    },
                ),
                required=True,
            ),
            FakeProvider(
                "stale",
                reading(
                    "stale",
                    {"edge_request_rate_rps": 99},
                    status=ProviderStatus.STALE,
                    freshness=10,
                ),
            ),
            FakeProvider("failed", error=RuntimeError("credential detail")),
            FakeProvider(
                "fresh", reading("fresh", {"queue_depth": 8, "login_rate_rps": 2})
            ),
        ],
        MemorySnapshotWriter(),
        omitted_providers=("cloudfront", "sqs"),
    )

    result = asyncio.run(collector.collect(now=NOW))

    assert result.origin_request_rate_rps == 12
    assert result.optional_values == {"queue_depth": 8, "login_rate_rps": 2}
    assert result.provider_health["stale"]["status"] == "stale"
    assert result.provider_health["failed"]["details"]["reason"] == (
        "provider_error:RuntimeError"
    )
    assert "credential detail" not in str(result.provider_health)
    assert result.provider_health["cloudfront"]["details"] == {
        "reason": "not_configured"
    }
    assert collector.provider_configuration["sqs"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "origin",
    [
        reading("demo_app", {}, status=ProviderStatus.HEALTHY),
        reading(
            "demo_app",
            {"origin_request_rate_rps": 10},
            status=ProviderStatus.STALE,
            freshness=6,
        ),
    ],
)
def test_collector_holds_when_required_origin_is_missing_or_stale(
    origin: SignalReading,
) -> None:
    collector = CompositeSignalCollector(
        [FakeProvider("demo_app", origin, required=True)], MemorySnapshotWriter()
    )

    with pytest.raises(RequiredSignalUnavailable):
        asyncio.run(collector.collect(now=NOW))


def test_provider_timeout_is_bounded_and_explicit() -> None:
    collector = CompositeSignalCollector(
        [
            FakeProvider(
                "demo_app",
                reading("demo_app", {"origin_request_rate_rps": 10}),
                required=True,
            ),
            FakeProvider("slow", reading("slow", {"queue_depth": 1}), delay=0.05),
        ],
        MemorySnapshotWriter(),
    )

    result = asyncio.run(collector.collect(now=NOW))

    assert result.provider_health["slow"]["status"] == "unavailable"
    assert result.provider_health["slow"]["details"] == {"reason": "timeout"}


def test_production_collector_persists_authoritative_capacity_and_rps_assumption() -> None:
    class Capacity:
        async def read_capacity(self, *, observed_at):
            return type(
                "Observation",
                (),
                {
                    "state": CapacityState(
                        desired=3,
                        in_service=2,
                        pending=1,
                        observed_at=observed_at,
                        provider_status=ProviderStatus.SIMULATED,
                    )
                },
            )()

    collector = CompositeSignalCollector(
        [
            FakeProvider(
                "demo_app",
                reading("demo_app", {"origin_request_rate_rps": 40}),
                required=True,
            )
        ],
        MemorySnapshotWriter(),
        capacity_reader=Capacity(),
        capacity_per_instance_rps=25,
    )
    collected = asyncio.run(collector.collect(now=NOW))
    snapshot = collected.to_snapshot(
        window_seconds=60,
        baseline_request_rate_rps=10,
        request_rate_change_rps=30,
        request_acceleration_rps2=3,
        queue_growth_per_second=None,
        reactive_comparator_crossed=False,
        evidence={"environment": "local"},
    )

    assert snapshot.asg_desired_capacity == 3
    assert snapshot.asg_in_service_capacity == 2
    assert snapshot.capacity_per_instance_rps == 25
    assert collected.provider_health["capacity"]["included"] is True


def test_simulated_buffer_enforces_ttl_duplicate_and_capacity_without_eviction() -> None:
    buffer = SimulatedSignalBuffer(capacity=1, max_ttl_seconds=30)
    frame = SimulatedSignalFrame(
        source="locust",
        observed_at=NOW,
        ttl_seconds=10,
        edge_request_rate_rps=20,
    )
    asyncio.run(buffer.add(frame, now=NOW))
    with pytest.raises(DuplicateSignalError):
        asyncio.run(buffer.add(frame, now=NOW))
    with pytest.raises(SignalBufferFullError):
        asyncio.run(
            buffer.add(
                frame.model_copy(update={"source": "second", "observed_at": NOW}),
                now=NOW,
            )
        )
    with pytest.raises(ExpiredSignalError):
        asyncio.run(
            buffer.add(
                frame.model_copy(update={"observed_at": NOW - timedelta(seconds=11)}),
                now=NOW,
            )
        )
    assert asyncio.run(buffer.size(now=NOW + timedelta(seconds=11))) == 0


def test_simulated_provider_returns_latest_live_frame() -> None:
    buffer = SimulatedSignalBuffer(capacity=2)
    first = SimulatedSignalFrame(
        source="locust",
        observed_at=NOW - timedelta(seconds=1),
        ttl_seconds=10,
        queue_depth=4,
    )
    latest = SimulatedSignalFrame(
        source="locust-next",
        observed_at=NOW,
        ttl_seconds=10,
        cpu_utilization_pct=60,
    )
    asyncio.run(buffer.add(first, now=NOW))
    asyncio.run(buffer.add(latest, now=NOW))

    result = asyncio.run(SimulatedSignalProvider(buffer).read(NOW))

    assert result.status is ProviderStatus.SIMULATED
    assert result.values == {"cpu_utilization_pct": 60.0}


def test_demo_app_provider_maps_bounded_snapshot() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/metrics/snapshot"
        return httpx.Response(
            200,
            json={
                "observed_at": NOW.isoformat(),
                "origin_request_rate_rps": 20,
                "request_count": 40,
                "concurrent_requests": 2,
                "error_rate": 0.01,
                "p50_latency_ms": 2,
                "p95_latency_ms": 4,
                "p99_latency_ms": 6,
                "load_shedding_level": 1,
                "endpoints": {"checkout": {"p99_latency_ms": 3, "success_rate": 1}},
                "persistence": {"available": True},
                "samples_truncated": False,
            },
        )

    async def run() -> SignalReading:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
            provider = DemoAppSignalProvider(
                "http://demo",
                timeout_seconds=0.1,
                freshness_limit_seconds=5,
                client=client,
            )
            return await provider.read(NOW)

    result = asyncio.run(run())

    assert result.status is ProviderStatus.HEALTHY
    assert result.values["checkout_p99_latency_ms"] == 3
    assert result.values["load_shedding_level"] == 1


class CloudWatchClient:
    def get_metric_data(self, **kwargs):
        assert kwargs["MaxDatapoints"] == 2
        return {
            "MetricDataResults": [
                {"Id": "m0", "Timestamps": [NOW], "Values": [55]},
                {"Id": "m1", "Timestamps": [NOW], "Values": [25]},
            ]
        }


class SqsClient:
    def __init__(self) -> None:
        self.depths = iter((10, 16))

    def get_queue_attributes(self, **kwargs):
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(next(self.depths)),
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }


def test_cloudwatch_sqs_and_session_adapters_are_typed_and_time_aware() -> None:
    cloudwatch = CloudWatchMetricProvider(
        name="cloudfront",
        client=CloudWatchClient(),
        metric_queries={
            "edge_request_rate_rps": {"Namespace": "AWS/CloudFront"},
            "cpu_utilization_pct": {"Namespace": "AWS/EC2"},
        },
        timeout_seconds=0.1,
        freshness_limit_seconds=60,
    )
    cloud = asyncio.run(cloudwatch.read(NOW))
    assert cloud.values == {"edge_request_rate_rps": 55, "cpu_utilization_pct": 25}

    sqs = SqsSignalProvider(
        SqsClient(), "queue-url", timeout_seconds=0.1, freshness_limit_seconds=60
    )
    asyncio.run(sqs.read(NOW))
    second = asyncio.run(sqs.read(NOW + timedelta(seconds=2)))
    assert second.values["queue_growth_per_second"] == 3

    async def fetch_sessions():
        return {
            "observed_at": NOW - timedelta(seconds=10),
            "concurrent_sessions": 8,
            "login_rate_rps": 2,
        }

    sessions = SessionLoginSignalProvider(
        fetch_sessions, timeout_seconds=0.1, freshness_limit_seconds=5
    )
    session_reading = asyncio.run(sessions.read(NOW))
    assert session_reading.status is ProviderStatus.STALE
