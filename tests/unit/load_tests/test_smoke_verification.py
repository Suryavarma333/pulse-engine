from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.verify_smoke_results import validate_completed_run, validate_scheduled_peak

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def completed_detail(*, lead: float | None = 2) -> dict:
    return {
        "id": "run-1",
        "status": "completed",
        "result_summary": {
            "metrics": {
                "checkout_p99_latency_ms": 25,
                "checkout_success_rate": 1,
                "detection_lead_seconds": lead,
            },
            "references": {"prediction_count": 1, "action_count": 1},
        },
    }


def test_completed_smoke_requires_traceable_checkout_action_prediction_and_lead() -> None:
    validate_completed_run(completed_detail(), require_positive_lead=True)
    with pytest.raises(RuntimeError, match="predict before"):
        validate_completed_run(completed_detail(lead=None), require_positive_lead=True)
    missing = completed_detail()
    missing["result_summary"]["references"]["action_count"] = 0
    with pytest.raises(RuntimeError, match="response action"):
        validate_completed_run(missing, require_positive_lead=False)


def test_scheduled_peak_must_be_unique_bounded_and_before_event() -> None:
    action = {
        "scheduled_event_id": "event-1",
        "idempotency_key": "scheduled:event-1:-2:3",
        "requested_at": (NOW - timedelta(seconds=2)).isoformat(),
        "applied_desired_capacity": 3,
        "max_instance_ceiling": 3,
        "signal_evidence": {
            "ramp_applied_target": 3,
            "event_starts_at": NOW.isoformat(),
        },
    }
    validate_scheduled_peak([action])
    duplicate = {**action, "idempotency_key": "scheduled:event-1:-1:3"}
    with pytest.raises(RuntimeError, match="exactly one"):
        validate_scheduled_peak([action, duplicate])
