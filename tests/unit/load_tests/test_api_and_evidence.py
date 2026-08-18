from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from load_tests.lib.evidence import export_evidence, read_locust_summary
from load_tests.lib.models import build_scenario_plan
from load_tests.lib.pulse_api import PulseApiClient

NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)


def test_run_lifecycle_signal_and_idempotent_scoped_reset_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/internal/demo-runs" and request.method == "POST":
            return httpx.Response(201, json={"id": "run-1", "status": "running"})
        if request.url.path == "/internal/demo-runs/run-1":
            return httpx.Response(200, json={"id": "run-1", "status": "completed"})
        if request.url.path == "/internal/signals/simulated":
            return httpx.Response(202, json={"provider_status": "simulated"})
        if request.url.path == "/internal/load-shedding":
            return httpx.Response(200, json={"level": 0, "changed": False})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    plan = build_scenario_plan("sudden-spike", smoke=True)
    with httpx.Client(transport=transport) as http_client:
        api = PulseApiClient(
            agent_url="http://agent",
            demo_app_url="http://demo",
            control_token="test-value",
            client=http_client,
        )
        run = api.start_run(
            plan,
            baseline_type="pulse",
            environment="local",
            execution_mode="dry_run",
            started_at=NOW,
            idempotency_key="stable-run-key",
            host="http://demo",
        )
        api.publish_signal(
            plan.pulse_signals[0],
            observed_at=NOW,
            run_id=run["id"],
            source="test-source",
        )
        api.complete_run(
            run["id"],
            status="completed",
            ended_at=NOW,
            locust_summary={"request_count": 10, "failure_count": 0},
            notes="contract test",
        )
        assert api.reset_tier(environment="local")["level"] == 0
        assert api.reset_tier(environment="local")["changed"] is False

    start_payload = json.loads(requests[0].content)
    assert start_payload["configuration"]["seed"] == plan.seed
    assert start_payload["configuration"]["endpoint_weights"]["/checkout"] == 5
    signal_payload = json.loads(requests[1].content)
    assert signal_payload["demo_run_id"] == "run-1"
    resets = [json.loads(item.content) for item in requests if item.url.host == "demo"]
    assert resets == [resets[0], resets[0]]
    assert resets[0]["level"] == 0
    assert resets[0]["signal_evidence"] == {"environment": "local", "scope": "tier_only"}
    assert all(item.headers["X-Pulse-Control-Token"] == "test-value" for item in requests)


def test_locust_summary_and_json_markdown_export(tmp_path: Path) -> None:
    stats = tmp_path / "run_stats.csv"
    with stats.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Name", "Request Count", "Failure Count", "99%"],
        )
        writer.writeheader()
        writer.writerow(
            {"Name": "critical checkout", "Request Count": "50", "Failure Count": "1", "99%": "123"}
        )
        writer.writerow(
            {"Name": "Aggregated", "Request Count": "100", "Failure Count": "2", "99%": "200"}
        )
    summary = read_locust_summary(tmp_path / "run")
    assert summary.as_api_payload()["checkout"] == {
        "attempts": 50,
        "successes": 49,
        "p99_latency_ms": 123.0,
    }

    detail = {
        "id": "run-1",
        "scenario_name": "sudden-spike",
        "baseline_type": "pulse",
        "execution_mode": "dry_run",
        "status": "completed",
        "formula_version": "v1",
        "result_summary": {
            "metrics": {"checkout_p99_latency_ms": 123.0, "detection_lead_seconds": 15.0},
            "warnings": ["paired_reactive_baseline_unavailable"],
        },
    }
    json_path, markdown_path = export_evidence(
        detail, output_directory=tmp_path / "evidence", stem="run-1"
    )
    assert json.loads(json_path.read_text())["id"] == "run-1"
    markdown = markdown_path.read_text()
    assert "15.000" in markdown
    assert "paired_reactive_baseline_unavailable" in markdown
    assert "invent" not in markdown.lower()
