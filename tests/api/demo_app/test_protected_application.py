from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from demo_app.app.config import AppSettings
from demo_app.app.main import create_app
from demo_app.app.shedding.store import (
    InMemoryLoadSheddingStore,
    SheddingTransition,
    TransitionConflictError,
)


def settings() -> AppSettings:
    return AppSettings(
        environment="test",
        database_url=None,
        control_token="test-token",
        metrics_window_seconds=10,
        metrics_max_samples=1_000,
        noncritical_rate_per_second=100,
        noncritical_rate_burst=100,
    )


def control(client: TestClient, level: int, **overrides) -> object:
    payload = {
        "level": level,
        "reason_code": "api_test",
        "reasoning": f"Apply tier {level}",
        "changed_by": "agent",
        "signal_evidence": {"origin_rps": 25.0},
        **overrides,
    }
    return client.post(
        "/internal/load-shedding",
        headers={"X-Pulse-Control-Token": "test-token"},
        json=payload,
    )


class ExplodingLimiter:
    async def allow(self) -> bool:
        raise AssertionError("checkout invoked the non-critical limiter")


class CountingStore(InMemoryLoadSheddingStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.writes = 0

    async def load_active(self, environment: str):
        self.reads += 1
        return await super().load_active(environment)

    async def record_transition(self, transition: SheddingTransition) -> None:
        self.writes += 1
        await super().record_transition(transition)


def test_checkout_structurally_bypasses_every_tier_and_noncritical_limiter() -> None:
    with TestClient(create_app(settings(), store=InMemoryLoadSheddingStore())) as client:
        client.app.state.noncritical_limiter = ExplodingLimiter()
        for level in range(4):
            assert control(client, level).status_code == 200
            response = client.post("/checkout", json={"cart_id": "safe", "item_count": 1})
            assert response.status_code == 200
            assert response.headers["X-Pulse-Endpoint-Mode"] == "normal"
            assert response.headers["X-Pulse-Protection"] == "critical-never-shed"
            assert response.json()["active_shedding_level"] == level


def test_checkout_ignores_additive_client_metadata_for_base_compatibility() -> None:
    with TestClient(create_app(settings(), store=InMemoryLoadSheddingStore())) as client:
        response = client.post(
            "/checkout",
            json={
                "cart_id": "compatible",
                "item_count": 1,
                "trace_context": {"source": "existing-client"},
                "promotion_code": "ignored-by-demo",
            },
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.headers["X-Pulse-Endpoint-Mode"] == "normal"


def test_protected_request_metrics_never_touch_the_control_store() -> None:
    store = CountingStore()
    with TestClient(create_app(settings(), store=store)) as client:
        startup_reads = store.reads
        checkout = client.post("/checkout", json={"cart_id": "no-io", "item_count": 1})
        assert checkout.status_code == 200
        assert client.get("/recommendations").status_code == 200
        assert client.get("/catalog").status_code == 200

    assert startup_reads == 1
    assert store.reads == startup_reads
    assert store.writes == 0


def test_noncritical_routes_follow_the_exact_tier_matrix() -> None:
    with TestClient(create_app(settings(), store=InMemoryLoadSheddingStore())) as client:
        expected = {
            0: ("normal", "normal"),
            1: ("disabled", "normal"),
            2: ("disabled", "cached"),
            3: ("rate_limited", "cached"),
        }
        for level, (recommendation_mode, catalog_mode) in expected.items():
            assert control(client, level).status_code == 200
            recommendations = client.get("/recommendations")
            catalog = client.get("/catalog")
            assert recommendations.headers["X-Pulse-Endpoint-Mode"] == recommendation_mode
            assert catalog.headers["X-Pulse-Endpoint-Mode"] == catalog_mode


def test_correlated_transition_evidence_is_persisted_and_bounded() -> None:
    store = InMemoryLoadSheddingStore()
    correlation_id = uuid4()
    prediction_id = uuid4()
    demo_run_id = uuid4()
    with TestClient(create_app(settings(), store=store)) as client:
        response = control(
            client,
            2,
            correlation_id=str(correlation_id),
            prediction_id=str(prediction_id),
            trigger_snapshot_id=42,
            demo_run_id=str(demo_run_id),
            signal_evidence={"origin_rps": 50.0, "confidence": 0.92},
        )
        sensitive = control(client, 3, signal_evidence={"control_token": "not-allowed"})

    assert response.status_code == 200
    transition = store.transitions[0]
    assert transition.correlation_id == correlation_id
    assert transition.prediction_id == prediction_id
    assert transition.trigger_snapshot_id == 42
    assert transition.demo_run_id == demo_run_id
    assert transition.signal_evidence == {"origin_rps": 50.0, "confidence": 0.92}
    assert sensitive.status_code == 422


def test_authoritative_tier_is_restored_after_application_restart() -> None:
    store = InMemoryLoadSheddingStore()
    with TestClient(create_app(settings(), store=store)) as first_client:
        changed = control(first_client, 2)
        event_id = UUID(changed.json()["event_id"])

    with TestClient(create_app(settings(), store=store)) as restarted_client:
        health = restarted_client.get("/health")
        checkout = restarted_client.post(
            "/checkout", json={"cart_id": "after-restart", "item_count": 1}
        )

    assert health.status_code == 200
    assert UUID(health.json()["load_shedding"]["event_id"]) == event_id
    assert health.json()["load_shedding"]["level"] == 2
    assert health.json()["load_shedding"]["endpoint_policies"]["checkout"] == "normal"
    assert checkout.status_code == 200
    assert checkout.headers["X-Pulse-Endpoint-Mode"] == "normal"


class FlakyStore(InMemoryLoadSheddingStore):
    backend = "postgresql"
    durable = True

    def __init__(self) -> None:
        super().__init__()
        self.fail_writes = True

    async def record_transition(self, transition: SheddingTransition) -> None:
        if self.fail_writes:
            raise RuntimeError("simulated database failure with internal details")
        await super().record_transition(transition)


def test_persistence_failure_never_activates_tier_and_recovers_authoritatively() -> None:
    store = FlakyStore()
    with TestClient(create_app(settings(), store=store)) as client:
        failed = control(client, 3, correlation_id=str(uuid4()))
        checkout_during_failure = client.post(
            "/checkout", json={"cart_id": "during-failure", "item_count": 1}
        )
        degraded = client.get("/health")

        store.fail_writes = False
        recovered = control(client, 3, correlation_id=str(uuid4()))
        healthy = client.get("/health")
        checkout_after_recovery = client.post(
            "/checkout", json={"cart_id": "after-recovery", "item_count": 1}
        )

    assert failed.status_code == 503
    assert failed.json()["code"] == "AUDIT_PERSISTENCE_UNAVAILABLE"
    assert "internal details" not in failed.text
    assert checkout_during_failure.json()["active_shedding_level"] == 0
    assert checkout_during_failure.headers["X-Pulse-Endpoint-Mode"] == "normal"
    assert degraded.status_code == 503
    assert degraded.json()["persistence"]["available"] is False
    assert recovered.status_code == 200
    assert recovered.json()["level"] == 3
    assert healthy.status_code == 200
    assert checkout_after_recovery.headers["X-Pulse-Endpoint-Mode"] == "normal"


class StartupUnavailableStore(InMemoryLoadSheddingStore):
    backend = "postgresql"
    durable = True

    async def load_active(self, environment: str):
        raise RuntimeError("database password must never reach an API response")


def test_checkout_starts_failure_safe_when_authoritative_store_is_unavailable() -> None:
    with TestClient(create_app(settings(), store=StartupUnavailableStore())) as client:
        health = client.get("/health")
        checkout = client.post("/checkout", json={"cart_id": "fail-safe", "item_count": 1})
        transition = control(client, 1)

    assert health.status_code == 503
    assert health.json()["ready"] is False
    assert health.json()["load_shedding"]["level"] == 0
    assert "password" not in health.text.lower()
    assert checkout.status_code == 200
    assert checkout.headers["X-Pulse-Endpoint-Mode"] == "normal"
    assert transition.status_code == 503


class ApiConflictingStore(InMemoryLoadSheddingStore):
    async def record_transition(self, transition: SheddingTransition) -> None:
        await super().record_transition(
            replace(
                transition,
                event_id=uuid4(),
                to_level=2,
                reason_code="external_winner",
                reasoning="A competing controller committed first",
            )
        )
        raise TransitionConflictError("simulated conflict")


def test_transition_conflict_returns_409_and_exposes_authoritative_tier() -> None:
    with TestClient(create_app(settings(), store=ApiConflictingStore())) as client:
        conflict = control(client, 1, correlation_id=str(uuid4()))
        health = client.get("/health")

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "TRANSITION_CONFLICT"
    assert health.json()["load_shedding"]["level"] == 2


def test_control_auth_uses_constant_time_comparison_and_never_leaks_token(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare_digest(provided: bytes, expected: bytes) -> bool:
        calls.append((provided, expected))
        return provided == expected

    monkeypatch.setattr(
        "demo_app.app.routes.operations.secrets.compare_digest", compare_digest
    )
    with TestClient(create_app(settings(), store=InMemoryLoadSheddingStore())) as client:
        invalid = client.post(
            "/internal/load-shedding",
            headers={"X-Pulse-Control-Token": "different-length-wrong-token"},
            json={"level": 1, "reason_code": "test", "reasoning": "invalid token"},
        )
        valid = control(client, 1)
        snapshot = client.get("/metrics/snapshot")
        health = client.get("/health")

    assert invalid.status_code == 401
    assert invalid.json()["code"] == "INVALID_CONTROL_TOKEN"
    assert valid.status_code == 200
    assert len(calls) == 2
    assert all(isinstance(value, bytes) for call in calls for value in call)
    assert "test-token" not in json.dumps(snapshot.json())
    assert "test-token" not in json.dumps(health.json())
