from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

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
        assert body["from"].endswith("+00:00")


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
