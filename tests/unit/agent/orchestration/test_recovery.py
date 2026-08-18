from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent.app.orchestration.recovery import (
    RecoveryCoordinator,
    RecoveryObservation,
    RecoveryWorker,
)
from agent.app.orchestration.response_pipeline import ResponseExecution
from agent.app.orchestration.state_machine import ControlStateMachine
from common.contracts import CapacityDecision, RecoveryPlan, ResponseCommand
from common.enums import (
    ActionStatus,
    ControlState,
    ExecutionMode,
    PredictionMode,
    SheddingLevel,
)

NOW = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)


def template(*, confirmations: int = 3) -> ResponseCommand:
    return ResponseCommand(
        idempotency_key="response:original",
        correlation_id=uuid4(),
        mode=PredictionMode.REALTIME,
        target_resource="pulse-asg",
        requested_desired_capacity=3,
        maximum_ceiling=4,
        requested_shedding_level=SheddingLevel.CACHED_NONCRITICAL,
        reason_code="surge",
        reasoning="Surge protection",
        signal_evidence={"source": "test"},
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=confirmations,
            cooldown_seconds=60,
            decrement_step=1,
            capacity_floor=1,
        ),
    )


def observation(
    at: datetime,
    *,
    rps: float = 1,
    high: bool = False,
    capacity: int = 3,
    tier: SheddingLevel = SheddingLevel.CACHED_NONCRITICAL,
) -> RecoveryObservation:
    return RecoveryObservation(
        observed_at=at,
        request_rate_rps=rps,
        high_load=high,
        current_capacity=capacity,
        current_shedding_level=tier,
    )


class FakePipeline:
    def __init__(self, machine: ControlStateMachine, *, status=ActionStatus.DRY_RUN) -> None:
        self.machine = machine
        self.status = status
        self.executed: list[ResponseCommand] = []
        self.held: list[ResponseCommand] = []

    async def execute(self, command, **values):
        self.executed.append(command)
        return ResponseExecution(
            action=None,
            capacity=CapacityDecision(
                requested=command.requested_desired_capacity,
                applied=(
                    command.requested_desired_capacity
                    if self.status is not ActionStatus.FAILED
                    else None
                ),
                ceiling=command.maximum_ceiling,
                execution_mode=ExecutionMode.DRY_RUN,
                status=self.status,
                sanitized_error="failed" if self.status is ActionStatus.FAILED else None,
            ),
            state=self.machine.state,
        )

    async def audit_hold(self, command, **values):
        self.held.append(command)
        return ResponseExecution(
            action=None,
            capacity=CapacityDecision(
                requested=values["current_capacity"],
                applied=None,
                ceiling=command.maximum_ceiling,
                execution_mode=ExecutionMode.DRY_RUN,
                status=ActionStatus.SKIPPED,
            ),
            state=self.machine.state,
        )


def test_recovery_requires_exact_low_confirmation_and_unwinds_one_tier() -> None:
    machine = ControlStateMachine(ControlState.PROTECT)
    coordinator = RecoveryCoordinator(machine)
    pipeline = FakePipeline(machine)
    worker = RecoveryWorker(coordinator=coordinator, response_pipeline=pipeline)  # type: ignore[arg-type]

    first = asyncio.run(worker.run_once(observation(NOW), command_template=template()))
    second = asyncio.run(
        worker.run_once(observation(NOW + timedelta(seconds=1)), command_template=template())
    )
    third = asyncio.run(
        worker.run_once(observation(NOW + timedelta(seconds=2)), command_template=template())
    )

    assert first.decision.confirmation_count == 1
    assert second.decision.confirmation_count == 2
    assert first.execution.capacity.status is ActionStatus.SKIPPED
    assert len(pipeline.held) == 2
    assert third.decision.confirmation_count == 3
    assert third.command.requested_desired_capacity == 2
    assert third.command.requested_shedding_level is SheddingLevel.DISABLE_RECOMMENDATIONS
    assert machine.state is ControlState.COOLDOWN
    assert coordinator.cooldown_until == NOW + timedelta(seconds=62)


def test_scale_in_cooldown_audits_hold_then_restarts_confirmation() -> None:
    machine = ControlStateMachine(ControlState.PROTECT)
    coordinator = RecoveryCoordinator(machine)
    pipeline = FakePipeline(machine)
    worker = RecoveryWorker(coordinator=coordinator, response_pipeline=pipeline)  # type: ignore[arg-type]
    one_confirm = template(confirmations=1)
    asyncio.run(worker.run_once(observation(NOW), command_template=one_confirm))

    held = asyncio.run(
        worker.run_once(observation(NOW + timedelta(seconds=30)), command_template=one_confirm)
    )
    next_step = asyncio.run(
        worker.run_once(observation(NOW + timedelta(seconds=60)), command_template=one_confirm)
    )
    assert held.decision.reason_code == "recovery_cooldown_active"
    assert held.execution.capacity.status is ActionStatus.SKIPPED
    assert next_step.decision.execute is True


def test_recovery_respects_floor_and_final_tier_returns_normal() -> None:
    machine = ControlStateMachine(ControlState.PROTECT)
    coordinator = RecoveryCoordinator(machine)
    pipeline = FakePipeline(machine)
    worker = RecoveryWorker(coordinator=coordinator, response_pipeline=pipeline)  # type: ignore[arg-type]

    result = asyncio.run(
        worker.run_once(
            observation(
                NOW,
                capacity=2,
                tier=SheddingLevel.DISABLE_RECOMMENDATIONS,
            ),
            command_template=template(confirmations=1),
        )
    )
    assert result.command.requested_desired_capacity == 1
    assert result.command.requested_shedding_level is SheddingLevel.NORMAL
    assert result.decision.final_step is True
    assert machine.state is ControlState.NORMAL
    assert coordinator.cooldown_until is None


def test_renewed_high_load_interrupts_cooldown_and_protects_immediately() -> None:
    machine = ControlStateMachine(ControlState.PROTECT)
    coordinator = RecoveryCoordinator(machine)
    pipeline = FakePipeline(machine)
    worker = RecoveryWorker(coordinator=coordinator, response_pipeline=pipeline)  # type: ignore[arg-type]
    one_confirm = template(confirmations=1)
    asyncio.run(worker.run_once(observation(NOW), command_template=one_confirm))
    assert machine.state is ControlState.COOLDOWN

    interrupted = asyncio.run(
        worker.run_once(
            observation(NOW + timedelta(seconds=1), rps=50, high=True, capacity=2),
            command_template=one_confirm,
        )
    )
    assert interrupted.decision.interrupted is True
    assert interrupted.decision.execute is True
    assert interrupted.command.requested_desired_capacity == 3
    assert machine.state is ControlState.PROTECT
    assert coordinator.cooldown_until is None


def test_failed_recovery_step_does_not_start_cooldown_or_lower_state() -> None:
    machine = ControlStateMachine(ControlState.PROTECT)
    coordinator = RecoveryCoordinator(machine)
    pipeline = FakePipeline(machine, status=ActionStatus.FAILED)
    worker = RecoveryWorker(coordinator=coordinator, response_pipeline=pipeline)  # type: ignore[arg-type]
    result = asyncio.run(
        worker.run_once(observation(NOW), command_template=template(confirmations=1))
    )
    assert result.execution.capacity.status is ActionStatus.FAILED
    assert machine.state is ControlState.RECOVERY
    assert coordinator.cooldown_until is None


def test_normal_and_failure_safe_states_hold_without_scale_in() -> None:
    for state, expected_reason, high in [
        (ControlState.NORMAL, "recovery_not_active", False),
        (ControlState.FAILURE_SAFE, "recovery_failure_safe_hold", True),
    ]:
        machine = ControlStateMachine(state)
        coordinator = RecoveryCoordinator(machine)
        pipeline = FakePipeline(machine)
        worker = RecoveryWorker(coordinator=coordinator, response_pipeline=pipeline)  # type: ignore[arg-type]
        result = asyncio.run(
            worker.run_once(
                observation(NOW, high=high),
                command_template=template(confirmations=1),
            )
        )
        assert result.decision.reason_code == expected_reason
        assert result.execution.capacity.status is ActionStatus.SKIPPED
        assert pipeline.executed == []
