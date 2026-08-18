from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from demo_app.app.config import AppSettings
from demo_app.app.main import create_app
from demo_app.app.metrics import TrafficMetrics
from demo_app.app.shedding.store import InMemoryLoadSheddingStore


def test_demo_app_settings_validate_metric_memory_bound() -> None:
    try:
        AppSettings(metrics_max_samples=99)
    except ValueError as exc:
        assert "metrics_max_samples" in str(exc)
    else:
        raise AssertionError("an unsafe metrics sample bound was accepted")


def test_metrics_snapshot_counts_only_application_traffic() -> None:
    settings = AppSettings(database_url=None, control_token="test-token")
    with TestClient(create_app(settings, store=InMemoryLoadSheddingStore())) as client:
        for index in range(3):
            response = client.post(
                "/checkout",
                json={"cart_id": f"cart-{index}", "item_count": 1},
            )
            assert response.status_code == 200

        snapshot = client.get("/metrics/snapshot").json()
        health = client.get("/health")

    assert snapshot["request_count"] == 3
    assert snapshot["requests_by_path"] == {"/checkout": 3}
    assert snapshot["origin_request_rate_rps"] > 0
    assert snapshot["p99_latency_ms"] > 0
    assert snapshot["endpoints"]["checkout"]["request_count"] == 3
    assert snapshot["endpoints"]["checkout"]["p99_latency_ms"] > 0
    assert snapshot["endpoints"]["checkout"]["success_rate"] == 1.0
    assert snapshot["sample_capacity"] == 20_000
    assert snapshot["samples_truncated"] is False
    assert snapshot["load_shedding_level"] == 0
    assert snapshot["persistence"] == {
        "backend": "memory",
        "durable": False,
        "available": True,
        "state_loaded": True,
    }
    assert health.status_code == 200


def test_metrics_keep_sample_and_endpoint_cardinality_bounded() -> None:
    async def exercise() -> dict:
        metrics = TrafficMetrics(window_seconds=10, max_samples=3)
        for index in range(5):
            await metrics.request_started()
            await metrics.request_finished(
                path=f"/untrusted-path-{index}",
                latency_ms=float(index + 1),
                status_code=200 if index < 4 else 503,
            )
        return await metrics.snapshot()

    snapshot = asyncio.run(exercise())

    assert snapshot["request_count"] == 3
    assert snapshot["sample_capacity"] == 3
    assert snapshot["samples_dropped"] == 2
    assert snapshot["samples_truncated"] is True
    assert set(snapshot["endpoints"]) == {
        "checkout",
        "recommendations",
        "catalog",
        "other",
    }
    assert snapshot["endpoints"]["other"]["request_count"] == 3
    assert snapshot["endpoints"]["other"]["success_rate"] == 2 / 3


def test_metrics_report_current_concurrency_without_external_io() -> None:
    async def exercise() -> tuple[dict, dict]:
        metrics = TrafficMetrics(window_seconds=10, max_samples=100)
        await metrics.request_started()
        active = await metrics.snapshot()
        await metrics.request_finished(path="/checkout", latency_ms=3.0, status_code=200)
        finished = await metrics.snapshot()
        return active, finished

    active, finished = asyncio.run(exercise())

    assert active["concurrent_requests"] == 1
    assert finished["concurrent_requests"] == 0
