from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common.enums import EndpointMode, SheddingLevel
from demo_app.app.config import AppSettings
from demo_app.app.main import create_app
from demo_app.app.shedding.policies import endpoint_mode
from demo_app.app.shedding.store import InMemoryLoadSheddingStore


@pytest.fixture
def store() -> InMemoryLoadSheddingStore:
    return InMemoryLoadSheddingStore()


@pytest.fixture
def client(store: InMemoryLoadSheddingStore):
    settings = AppSettings(
        environment="test",
        database_url=None,
        control_token="test-token",
        metrics_window_seconds=10,
        noncritical_rate_per_second=0.001,
        noncritical_rate_burst=1,
    )
    with TestClient(create_app(settings, store=store)) as test_client:
        yield test_client


def transition(client: TestClient, level: int) -> dict:
    response = client.post(
        "/internal/load-shedding",
        headers={"X-Pulse-Control-Token": "test-token"},
        json={
            "level": level,
            "reason_code": "unit_test",
            "reasoning": f"Exercise level {level}",
            "signal_evidence": {"request_acceleration_rps2": 12.5},
        },
    )
    assert response.status_code == 200
    return response.json()


def test_checkout_is_normal_at_every_shedding_level(client: TestClient) -> None:
    for level in range(4):
        transition(client, level)
        response = client.post("/checkout", json={"cart_id": "cart-1", "item_count": 2})
        assert response.status_code == 200
        assert response.json()["accepted"] is True
        assert response.headers["X-Pulse-Endpoint-Mode"] == "normal"
        assert response.headers["X-Pulse-Protection"] == "critical-never-shed"
        assert response.headers["X-Pulse-Shedding-Level"] == str(level)


def test_recommendations_disable_at_level_one(client: TestClient) -> None:
    normal = client.get("/recommendations")
    assert normal.status_code == 200
    assert normal.json()["degraded"] is False
    assert len(normal.json()["items"]) == 2

    state = transition(client, 1)
    degraded = client.get("/recommendations")

    assert state["changed"] is True
    assert degraded.status_code == 200
    assert degraded.json()["degraded"] is True
    assert degraded.json()["items"] == []
    assert degraded.headers["X-Pulse-Endpoint-Mode"] == "disabled"


def test_catalog_uses_stale_cache_at_level_two(client: TestClient) -> None:
    transition(client, 2)
    response = client.get("/catalog")

    assert response.status_code == 200
    assert response.json()["degraded"] is True
    assert response.json()["cache_status"] == "stale-while-revalidate"
    assert response.headers["X-Pulse-Endpoint-Mode"] == "cached"
    assert "Response is stale" in response.headers["Warning"]


def test_level_three_rate_limits_only_noncritical_traffic(client: TestClient) -> None:
    transition(client, 3)

    first = client.get("/catalog")
    second = client.get("/catalog")
    checkout = client.post("/checkout", json={"cart_id": "protected", "item_count": 1})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"
    assert checkout.status_code == 200


def test_control_endpoint_requires_token(client: TestClient) -> None:
    response = client.post(
        "/internal/load-shedding",
        json={"level": 1, "reason_code": "test", "reasoning": "missing auth"},
    )
    assert response.status_code == 401


def test_state_change_is_audited_and_noop_is_not_duplicated(
    client: TestClient, store: InMemoryLoadSheddingStore
) -> None:
    changed = transition(client, 1)
    unchanged = transition(client, 1)

    assert changed["changed"] is True
    assert unchanged["changed"] is False
    assert len(store.transitions) == 1
    assert store.transitions[0].signal_evidence["request_acceleration_rps2"] == 12.5


def test_health_reports_tier_and_endpoint_policy(client: TestClient) -> None:
    transition(client, 2)
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["critical_path_protected"] is True
    assert body["load_shedding"]["level"] == 2
    assert body["load_shedding"]["endpoint_policies"] == {
        "checkout": "normal",
        "recommendations": "disabled",
        "catalog": "cached",
    }


def test_policy_matrix_cannot_shed_checkout() -> None:
    for level in SheddingLevel:
        assert endpoint_mode("checkout", level) is EndpointMode.NORMAL
