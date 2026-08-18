from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from common.contracts import DashboardSnapshotV1, WorkerHealth
from common.enums import ProviderStatus
from tests.api.agent.conftest import NOW


def test_health_and_status_are_operational_and_secret_free(app_fixture) -> None:
    health = app_fixture.client.get("/health")
    assert health.status_code == 200
    assert health.json()["ready"] is True

    response = app_fixture.client.get("/api/v1/status")
    assert response.status_code == 200
    body = response.json()
    assert body["execution_mode"] == "dry_run"
    assert body["capacity"]["desired"] == 2
    assert body["shedding_level"] == 1
    assert body["scheduled"]["event"]["name"] == "Diwali"
    assert body["providers"]["demo_app"]["status"] == "healthy"
    assert "test-control-token" not in response.text


def test_dashboard_origin_can_read_but_is_never_credentialed(app_fixture) -> None:
    response = app_fixture.client.get(
        "/api/v1/status",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in response.headers


def test_health_returns_503_for_runtime_dependency_or_mandatory_worker_failure(
    app_fixture,
) -> None:
    worker = app_fixture.components.workers[0]
    worker.health = WorkerHealth(
        name="fake",
        status=ProviderStatus.UNAVAILABLE,
        checked_at=NOW,
        detail="persistence_failed:OperationalError",
    )
    failed_worker = app_fixture.client.get("/health")

    assert failed_worker.status_code == 503
    assert failed_worker.json()["ready"] is False

    worker.health = WorkerHealth(
        name="fake",
        status=ProviderStatus.HEALTHY,
        checked_at=NOW,
        detail="recovered",
    )
    app_fixture.client.app.state.db_ready = False
    database_outage = app_fixture.client.get("/health")
    app_fixture.client.app.state.db_ready = True
    recovered = app_fixture.client.get("/health")

    assert database_outage.status_code == 503
    assert database_outage.json()["database"] == {"ready": False}
    assert recovered.status_code == 200
    assert recovered.json()["ready"] is True


def test_all_bounded_history_and_result_endpoints_return_page_contract(app_fixture) -> None:
    start = (NOW - timedelta(minutes=1)).isoformat()
    end = (NOW + timedelta(minutes=1)).isoformat()
    endpoints = [
        "/api/v1/snapshots?environment=local",
        "/api/v1/predictions?mode=realtime&status=active",
        "/api/v1/scaling-actions?execution_mode=dry_run&status=dry_run",
        "/api/v1/load-shedding-events?environment=local&active_only=true",
        "/api/v1/scheduled-events?status=active",
        "/api/v1/results/summary?environment=local&execution_mode=dry_run",
    ]
    for endpoint in endpoints:
        separator = "&" if "?" in endpoint else "?"
        response = app_fixture.client.get(
            f"{endpoint}{separator}limit=1",
            params={"from": start, "to": end},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 1
        assert body["next_cursor"] == "next-page"
        assert body["from"].endswith(("+00:00", "Z"))


def test_snapshot_endpoint_matches_dashboard_v1_contract(app_fixture) -> None:
    fixture_path = (
        Path(__file__).parents[3]
        / "dashboard"
        / "tests"
        / "fixtures"
        / "snapshot-page-v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["items"][0]

    response = app_fixture.client.get("/api/v1/snapshots?limit=1")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert DashboardSnapshotV1.model_validate(item).pending_capacity == 1
    assert {key: item[key] for key in fixture} == fixture


def test_query_range_limit_and_cursor_validation_return_structured_422(app_fixture) -> None:
    only_from = app_fixture.client.get(
        "/api/v1/snapshots", params={"from": NOW.isoformat()}
    )
    assert only_from.status_code == 422
    assert only_from.json()["code"] == "INVALID_QUERY_WINDOW"

    too_many = app_fixture.client.get("/api/v1/snapshots?limit=11")
    assert too_many.status_code == 422
    assert too_many.json()["code"] == "QUERY_LIMIT_EXCEEDED"

    too_wide = app_fixture.client.get(
        "/api/v1/snapshots",
        params={
            "from": NOW.isoformat(),
            "to": (NOW + timedelta(days=2)).isoformat(),
        },
    )
    assert too_wide.status_code == 422
    assert too_wide.json()["code"] == "QUERY_RANGE_EXCEEDED"

    invalid_cursor = app_fixture.client.get("/api/v1/snapshots?cursor=bad")
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["code"] == "INVALID_CURSOR"

    invalid_enum = app_fixture.client.get("/api/v1/scaling-actions?status=impossible")
    assert invalid_enum.status_code == 422
    assert invalid_enum.json()["code"] == "VALIDATION_ERROR"


def test_prediction_overlay_and_result_detail_are_bounded_and_traceable(app_fixture) -> None:
    prediction_id = app_fixture.operator.prediction.id
    overlay = app_fixture.client.get(f"/api/v1/predictions/{prediction_id}")
    assert overlay.status_code == 200
    assert len(overlay.json()["predicted_points"]) == 1
    assert len(overlay.json()["actual_snapshots"]) == 1

    missing = app_fixture.client.get(f"/api/v1/predictions/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "PREDICTION_NOT_FOUND"

    detail = app_fixture.client.get(
        f"/api/v1/results/runs/{app_fixture.operator.run.id}?require_complete=true"
    )
    assert detail.status_code == 200
    assert detail.json()["references"]["snapshot_ids"] == [1]
    assert "provisioning_efficiency_pct" in detail.json()["formula_definitions"]

    app_fixture.operator.run.status = "pending_evaluation"
    conflict = app_fixture.client.get(
        f"/api/v1/results/runs/{app_fixture.operator.run.id}?require_complete=true"
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "RUN_NOT_COMPLETE"

    not_found = app_fixture.client.get(f"/api/v1/results/runs/{uuid4()}")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "RUN_NOT_FOUND"
