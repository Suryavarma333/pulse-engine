from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from tests.api.agent.conftest import NOW

HEADERS = {"X-Pulse-Control-Token": "test-control-token"}


def start_payload(**overrides):
    values = {
        "idempotency_key": "scenario-run-1",
        "scenario_name": "sudden-spike",
        "mode": "realtime",
        "environment": "local",
        "baseline_type": "pulse",
        "execution_mode": "dry_run",
        "configuration": {"users": 10, "spawn_rate": 2},
        "thresholds": {"ratio": 1.5},
        "started_at": NOW.isoformat(),
    }
    values.update(overrides)
    return values


def test_demo_run_start_requires_auth_and_is_idempotent(app_fixture) -> None:
    unauthorized = app_fixture.client.post("/internal/demo-runs", json=start_payload())
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "INVALID_CONTROL_TOKEN"

    created = app_fixture.client.post(
        "/internal/demo-runs", headers=HEADERS, json=start_payload()
    )
    assert created.status_code == 201
    assert created.json()["status"] == "running"

    repeated = app_fixture.client.post(
        "/internal/demo-runs", headers=HEADERS, json=start_payload()
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created.json()["id"]

    conflict = app_fixture.client.post(
        "/internal/demo-runs",
        headers=HEADERS,
        json=start_payload(scenario_name="different"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_demo_run_uses_server_effective_configuration_and_restart_safe_key(
    app_fixture,
) -> None:
    payload = start_payload(
        pair_group_id="pair:sudden:42",
        configuration={"users": 10, "rps_per_instance": 999},
        thresholds={"entry_ratio_threshold": 99, "confirmation_count": 99},
    )
    created = app_fixture.client.post(
        "/internal/demo-runs", headers=HEADERS, json=payload
    )
    run = app_fixture.runs.by_id[next(iter(app_fixture.runs.by_id))]

    assert created.status_code == 201
    assert run.configuration["pair_group_id"] == "pair:sudden:42"
    assert run.configuration["settings_snapshot_version"] == "v1"
    assert run.configuration["rps_per_instance"] == 25
    assert run.configuration["minimum_desired_capacity"] == 1
    assert "users" not in run.configuration
    assert run.configuration["effective_control"]["baseline_strategy"] == "moving_average"
    assert run.thresholds["entry_ratio_threshold"] == 1.5
    assert run.thresholds["confirmation_count"] == 3

    restarted = app_fixture.client.post(
        "/internal/demo-runs",
        headers=HEADERS,
        json={
            **payload,
            "started_at": (NOW + timedelta(seconds=30)).isoformat(),
        },
    )
    assert restarted.status_code == 200
    assert restarted.json()["id"] == created.json()["id"]


def test_demo_run_completion_is_authenticated_idempotent_and_evaluated(app_fixture) -> None:
    created = app_fixture.client.post(
        "/internal/demo-runs", headers=HEADERS, json=start_payload()
    )
    run_id = created.json()["id"]
    terminal = {
        "status": "completed",
        "ended_at": (NOW + timedelta(minutes=5)).isoformat(),
        "locust_summary": {
            "request_count": 100,
            "failure_count": 0,
            "checkout": {"attempts": 50, "successes": 50, "p99_latency_ms": 100},
        },
        "notes": "deterministic local run",
    }
    unauthorized = app_fixture.client.patch(
        f"/internal/demo-runs/{run_id}", json=terminal
    )
    assert unauthorized.status_code == 401

    completed = app_fixture.client.patch(
        f"/internal/demo-runs/{run_id}", headers=HEADERS, json=terminal
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["formula_version"] == "v1"
    assert completed.json()["results"]["metrics"]["checkout_success_rate"] == 1

    repeated = app_fixture.client.patch(
        f"/internal/demo-runs/{run_id}", headers=HEADERS, json=terminal
    )
    assert repeated.status_code == 200

    conflicting = {
        **terminal,
        "status": "failed",
    }
    response = app_fixture.client.patch(
        f"/internal/demo-runs/{run_id}", headers=HEADERS, json=conflicting
    )
    assert response.status_code == 409
    assert response.json()["code"] == "RUN_COMPLETION_CONFLICT"


def test_demo_run_validation_not_found_and_sensitive_evidence(app_fixture) -> None:
    invalid = app_fixture.client.post(
        "/internal/demo-runs",
        headers=HEADERS,
        json=start_payload(configuration={"aws_secret": "nope"}),
    )
    assert invalid.status_code == 422
    assert "nope" not in invalid.text

    missing = app_fixture.client.patch(
        f"/internal/demo-runs/{uuid4()}",
        headers=HEADERS,
        json={
            "status": "failed",
            "ended_at": NOW.isoformat(),
            "locust_summary": {},
        },
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "RUN_NOT_FOUND"

    invalid_status = app_fixture.client.patch(
        f"/internal/demo-runs/{uuid4()}",
        headers=HEADERS,
        json={"status": "running", "ended_at": NOW.isoformat(), "locust_summary": {}},
    )
    assert invalid_status.status_code == 422
