from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from common.contracts import (
    CapacityDecision,
    DashboardSnapshotV1,
    DemoRunSpec,
    DemoRunState,
    QueryWindow,
    RecoveryPlan,
    ResponseCommand,
    SignalReading,
    SurgePredictionCandidate,
    validate_evidence,
)
from common.enums import (
    ActionStatus,
    ControlState,
    DemoRunStatus,
    ExecutionMode,
    PredictionMode,
    ProviderStatus,
    ResponseIntent,
    SheddingLevel,
)

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


def recovery_plan() -> RecoveryPlan:
    return RecoveryPlan(
        low_threshold_rps=2,
        confirmation_count=3,
        cooldown_seconds=120,
        decrement_step=1,
        capacity_floor=1,
    )


def test_signal_reading_normalizes_aware_timestamp_to_utc() -> None:
    reading = SignalReading(
        source="edge",
        observed_at=datetime(2026, 8, 18, 13, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        status=ProviderStatus.HEALTHY,
        values={"request_rate_rps": 12.5},
        freshness_seconds=0.1,
    )

    assert reading.observed_at == NOW


def test_naive_timestamps_and_invalid_query_ranges_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        SignalReading(
            source="origin",
            observed_at=datetime(2026, 8, 18, 8, 0),
            status=ProviderStatus.HEALTHY,
            freshness_seconds=0,
        )

    with pytest.raises(ValidationError, match="query end must be after start"):
        QueryWindow(start=NOW, end=NOW)


def test_prediction_contract_rejects_invalid_time_order() -> None:
    with pytest.raises(ValidationError, match="basis_window_end"):
        SurgePredictionCandidate(
            mode=PredictionMode.REALTIME,
            environment="test",
            basis_window_start=NOW,
            basis_window_end=NOW,
            predicted_start_at=NOW,
            predicted_peak_at=NOW + timedelta(seconds=30),
            baseline_rps=10,
            predicted_peak_rps=30,
            confidence=0.8,
            recommended_capacity=2,
            reason_code="accelerating",
            reasoning="Request acceleration persisted for three observations",
        )


def test_response_command_allows_clamping_but_rejects_unsafe_recovery_floor() -> None:
    command = ResponseCommand(
        idempotency_key="realtime:test:sample-1",
        correlation_id=uuid4(),
        mode=PredictionMode.REALTIME,
        target_resource="demo-asg",
        requested_desired_capacity=8,
        maximum_ceiling=3,
        requested_shedding_level=SheddingLevel.DISABLE_RECOMMENDATIONS,
        reason_code="surge_confirmed",
        reasoning="Requested capacity will be clamped by the response pipeline",
        recovery_plan=recovery_plan(),
    )

    assert command.requested_desired_capacity == 8
    assert command.maximum_ceiling == 3
    assert command.intent is ResponseIntent.DETECTOR

    with pytest.raises(ValidationError, match="capacity_floor"):
        ResponseCommand.model_validate(
            {
                **command.model_dump(),
                "maximum_ceiling": 1,
                "recovery_plan": {
                    **recovery_plan().model_dump(),
                    "capacity_floor": 2,
                },
            }
        )


def test_capacity_decision_never_accepts_applied_value_above_ceiling() -> None:
    with pytest.raises(ValidationError, match="must not exceed ceiling"):
        CapacityDecision(
            requested=8,
            applied=4,
            ceiling=3,
            execution_mode=ExecutionMode.DRY_RUN,
            status=ActionStatus.CAPPED,
        )


@pytest.mark.parametrize(
    ("desired", "in_service", "pending"),
    [(3, 1, 2), (2, 3, 0), (None, 1, None)],
)
def test_dashboard_snapshot_v1_derives_nonnegative_pending_capacity(
    desired: int | None,
    in_service: int | None,
    pending: int | None,
) -> None:
    record = SimpleNamespace(
        id=1,
        environment="local",
        observed_at=NOW,
        window_seconds=60,
        origin_request_rate_rps=10,
        baseline_request_rate_rps=8,
        asg_desired_capacity=desired,
        asg_in_service_capacity=in_service,
    )

    snapshot = DashboardSnapshotV1.from_record(record)

    assert snapshot.schema_version == "pulse.snapshot.v1"
    assert snapshot.pending_capacity == pending


@pytest.mark.parametrize("key", ["control_token", "aws_secret", "cart_id", "user_id"])
def test_evidence_rejects_sensitive_or_shopper_keys(key: str) -> None:
    with pytest.raises(ValueError, match="sensitive or shopper"):
        validate_evidence({key: "must-not-be-audited"})

    with pytest.raises(ValueError, match="sensitive or shopper"):
        validate_evidence({"provider": {key: "nested-must-not-be-audited"}})


def test_evidence_is_size_bounded() -> None:
    with pytest.raises(ValueError, match="encoded bytes"):
        validate_evidence({"signal": "x" * 20_000})


def test_demo_run_terminal_time_must_follow_start() -> None:
    spec = DemoRunSpec(
        idempotency_key="run-1",
        scenario_name="sudden-spike",
        mode=PredictionMode.REALTIME,
        environment="test",
        baseline_type="pulse",
        execution_mode=ExecutionMode.DRY_RUN,
        started_at=NOW,
    )
    with pytest.raises(ValidationError, match="ended_at"):
        DemoRunState(
            id=uuid4(),
            spec=spec,
            status=DemoRunStatus.FAILED,
            ended_at=NOW - timedelta(seconds=1),
        )


def test_control_state_contract_contains_all_designed_states() -> None:
    assert {state.value for state in ControlState} == {
        "normal",
        "watch",
        "prewarm",
        "protect",
        "recovery",
        "cooldown",
        "failure_safe",
    }
