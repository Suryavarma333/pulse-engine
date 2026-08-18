from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from agent.app.api.internal import create_simulated_signal_app
from agent.app.metrics.providers.simulated import SimulatedSignalBuffer

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


def app(*, capacity: int = 2, available: bool = True):
    return create_simulated_signal_app(
        buffer=SimulatedSignalBuffer(capacity=capacity, max_ttl_seconds=30),
        control_token="test-token",
        clock=FixedClock(),
        worker_available=available,
    )


def payload(**overrides):
    return {
        "source": "locust",
        "observed_at": NOW.isoformat(),
        "ttl_seconds": 10,
        "edge_request_rate_rps": 25,
        **overrides,
    }


def post(client: TestClient, body: dict, token: str = "test-token"):
    return client.post(
        "/internal/signals/simulated",
        headers={"X-Pulse-Control-Token": token},
        json=body,
    )


def test_ingestion_requires_constant_time_authenticated_control_token() -> None:
    with TestClient(app()) as client:
        missing = client.post("/internal/signals/simulated", json=payload())
        invalid = post(client, payload(), token="wrong")
        accepted = post(client, payload())

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["provider_status"] == "simulated"
    assert accepted.json()["accepted_values"] == {"edge_request_rate_rps": 25.0}
    assert "test-token" not in accepted.text


def test_duplicate_expired_future_and_empty_frames_are_rejected() -> None:
    with TestClient(app()) as client:
        assert post(client, payload()).status_code == 202
        duplicate = post(client, payload())
        expired = post(
            client,
            payload(source="expired", observed_at=(NOW - timedelta(seconds=11)).isoformat()),
        )
        future = post(
            client,
            payload(source="future", observed_at=(NOW + timedelta(seconds=6)).isoformat()),
        )
        empty = post(
            client,
            {
                "source": "empty",
                "observed_at": NOW.isoformat(),
                "ttl_seconds": 10,
            },
        )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "DUPLICATE_SIGNAL"
    assert expired.status_code == 422
    assert expired.json()["code"] == "EXPIRED_SIGNAL"
    assert future.status_code == 422
    assert empty.status_code == 422


def test_range_ttl_capacity_and_worker_availability_are_bounded() -> None:
    with TestClient(app(capacity=1)) as client:
        bad_cpu = post(client, payload(cpu_utilization_pct=101))
        long_ttl = post(client, payload(source="long", ttl_seconds=31))
        first = post(client, payload(source="first"))
        full = post(client, payload(source="second"))

    with TestClient(app(available=False)) as unavailable_client:
        unavailable = post(unavailable_client, payload())

    assert bad_cpu.status_code == 422
    assert long_ttl.status_code == 422
    assert first.status_code == 202
    assert full.status_code == 429
    assert full.json()["code"] == "SIGNAL_BUFFER_FULL"
    assert unavailable.status_code == 503
