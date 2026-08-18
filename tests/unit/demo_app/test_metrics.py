from __future__ import annotations

from fastapi.testclient import TestClient

from demo_app.app.config import AppSettings
from demo_app.app.main import create_app
from demo_app.app.shedding.store import InMemoryLoadSheddingStore


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
    assert snapshot["load_shedding_level"] == 0
    assert health.status_code == 200
