from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts.verify_stack import request_json


def latest_evidence(directory: Path, scenario: str) -> dict[str, Any]:
    paths = sorted(directory.glob(f"{scenario}-pulse-*.json"))
    if not paths:
        raise RuntimeError(f"no Pulse evidence found for {scenario}")
    payload = json.loads(paths[-1].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"evidence for {scenario} is not a JSON object")
    return payload


def validate_completed_run(detail: dict[str, Any], *, require_positive_lead: bool) -> None:
    if detail.get("status") != "completed":
        raise RuntimeError(f"run {detail.get('id')} did not complete evaluation")
    summary = detail.get("result_summary") or {}
    metrics = summary.get("metrics") or {}
    references = summary.get("references") or {}
    checkout_p99 = metrics.get("checkout_p99_latency_ms")
    checkout_success = metrics.get("checkout_success_rate")
    if checkout_p99 is None or float(checkout_p99) < 0:
        raise RuntimeError("completed smoke run has no checkout p99 evidence")
    if checkout_success is None or float(checkout_success) < 0.99:
        raise RuntimeError("completed smoke run did not preserve checkout success")
    if int(references.get("prediction_count", 0)) < 1:
        raise RuntimeError("completed smoke run has no linked prediction")
    if int(references.get("action_count", 0)) < 1:
        raise RuntimeError("completed smoke run has no linked response action")
    if require_positive_lead:
        lead = metrics.get("detection_lead_seconds")
        if lead is None or float(lead) <= 0:
            raise RuntimeError(
                "sudden smoke did not predict before the reactive comparator; "
                f"detection_lead_seconds={lead!r}"
            )


def validate_scheduled_peak(actions: list[dict[str, Any]]) -> None:
    for action in actions:
        applied = action.get("applied_desired_capacity")
        ceiling = action.get("max_instance_ceiling")
        if applied is not None and int(applied) > int(ceiling):
            raise RuntimeError("smoke action exceeded its effective ceiling")
    peak = [
        action
        for action in actions
        if action.get("scheduled_event_id")
        and ":protect:" not in str(action.get("idempotency_key"))
        and int((action.get("signal_evidence") or {}).get("ramp_applied_target", -1)) == 3
    ]
    if len(peak) != 1:
        raise RuntimeError("scheduled smoke did not record exactly one peak ramp action")
    action = peak[0]
    evidence = action.get("signal_evidence") or {}
    requested_at = datetime.fromisoformat(str(action["requested_at"]))
    event_starts_at = datetime.fromisoformat(str(evidence["event_starts_at"]))
    if requested_at >= event_starts_at:
        raise RuntimeError("scheduled peak was not requested before event start")
    if int(action.get("applied_desired_capacity") or -1) != 3:
        raise RuntimeError("scheduled peak did not reach the safe target of three")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify persisted bounded smoke evidence")
    parser.add_argument("--directory", type=Path, default=Path("load_tests/results"))
    parser.add_argument("--agent-url", default="http://localhost:8100")
    args = parser.parse_args()

    scheduled = latest_evidence(args.directory, "scheduled-diwali")
    sudden = latest_evidence(args.directory, "sudden-spike")
    validate_completed_run(scheduled, require_positive_lead=False)
    validate_completed_run(sudden, require_positive_lead=True)
    query = urlencode({"demo_run_id": scheduled["id"], "limit": 100})
    action_page, _ = request_json(
        f"{args.agent_url.rstrip('/')}/api/v1/scaling-actions?{query}"
    )
    validate_scheduled_peak(action_page.get("items") or [])
    print("Scheduled peak, sudden lead, checkout, audit, and ceiling evidence passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
