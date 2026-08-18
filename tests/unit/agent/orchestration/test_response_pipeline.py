from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlStateMachine
from agent.app.services.shedding_client import (
    SheddingControlError,
    SheddingControlResult,
)
from common.contracts import CapacityDecision, RecoveryPlan, ResponseCommand
from common.enums import (
    ActionStatus,
    ControlState,
    ExecutionMode,
    PredictionMode,
    ResponseIntent,
    SheddingLevel,
)

NOW = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)


def make_command(
    *,
    key: str = "realtime:test:1",
    mode: PredictionMode = PredictionMode.REALTIME,
    desired: int = 5,
    ceiling: int = 4,
    tier: SheddingLevel | None = SheddingLevel.DISABLE_RECOMMENDATIONS,
) -> ResponseCommand:
    return ResponseCommand(
        idempotency_key=key,
        correlation_id=uuid4(),
        mode=mode,
        target_resource="pulse-asg",
        requested_desired_capacity=desired,
        maximum_ceiling=ceiling,
        requested_shedding_level=tier,
        reason_code="confirmed_acceleration",
        reasoning="Confirmed accelerating traffic requires safe protection",
        signal_evidence={"request_acceleration_rps2": 5.5},
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=3,
            cooldown_seconds=120,
            decrement_step=1,
            capacity_floor=1,
        ),
    )


class FakeRepository:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.actions: dict[str, SimpleNamespace] = {}
        self.lock = asyncio.Lock()
        self.unresolved = False
        self.fail_update = False
        self.control_errors: list[str] = []

    async def has_unresolved(self, *, target_resource: str) -> bool:
        assert target_resource == "pulse-asg"
        return self.unresolved

    async def claim(self, command, **values):
        async with self.lock:
            self.order.append("claim")
            existing = self.actions.get(command.idempotency_key)
            if existing is not None:
                return SimpleNamespace(action=existing, claimed=False)
            action = SimpleNamespace(
                id=uuid4(),
                idempotency_key=command.idempotency_key,
                requested_desired_capacity=command.requested_desired_capacity,
                applied_desired_capacity=None,
                max_instance_ceiling=values["effective_ceiling"],
                execution_mode=values["execution_mode"].value,
                status=ActionStatus.PLANNED.value,
                provider_request_id=None,
                error_message=None,
                target_resource=command.target_resource,
                cooldown_until=None,
                signal_evidence=command.signal_evidence,
            )
            self.actions[command.idempotency_key] = action
            return SimpleNamespace(action=action, claimed=True)

    async def update_outcome(self, **values):
        self.order.append("update")
        if self.fail_update:
            raise RuntimeError("database unavailable")
        action = self.actions[values["idempotency_key"]]
        action.status = values["status"].value
        action.applied_desired_capacity = values["applied_desired_capacity"]
        action.provider_request_id = values.get("provider_request_id")
        action.error_message = values.get("error_message")
        action.cooldown_until = values.get("cooldown_until")
        return action

    async def record_control_error(self, **values):
        self.control_errors.append(values["error_message"])
        return self.actions[values["idempotency_key"]]


class FakeCapacityAdapter:
    execution_mode = ExecutionMode.DRY_RUN
    target_resource = "pulse-asg"

    def __init__(self, order: list[str], *, status: ActionStatus = ActionStatus.CAPPED) -> None:
        self.order = order
        self.status = status
        self.calls = 0
        self.effective_ceiling = None

    async def execute(self, *, requested_capacity, effective_ceiling, observed_at):
        self.order.append("adapter")
        self.calls += 1
        self.effective_ceiling = effective_ceiling
        await asyncio.sleep(0)
        return CapacityDecision(
            requested=requested_capacity,
            applied=(
                min(requested_capacity, effective_ceiling)
                if self.status is not ActionStatus.FAILED
                else None
            ),
            ceiling=effective_ceiling,
            execution_mode=self.execution_mode,
            status=self.status,
            sanitized_error="provider_failed" if self.status is ActionStatus.FAILED else None,
        )


class FakeSheddingClient:
    def __init__(self, order: list[str], *, fail: bool = False) -> None:
        self.order = order
        self.fail = fail
        self.calls = 0

    async def apply(self, *, command, level):
        self.order.append("shedding")
        self.calls += 1
        if self.fail:
            raise SheddingControlError("unavailable", retryable=True, status_code=503)
        return SheddingControlResult(
            changed=True,
            level=level,
            event_id=str(uuid4()),
            endpoint_policies={"checkout": "normal"},
        )


def pipeline(*, adapter_status=ActionStatus.CAPPED, shedding_fail=False):
    order: list[str] = []
    repository = FakeRepository(order)
    adapter = FakeCapacityAdapter(order, status=adapter_status)
    shedding = FakeSheddingClient(order, fail=shedding_fail)
    states = ControlStateMachine()
    response = ResponsePipeline(
        repository=repository,
        capacity_adapter=adapter,
        shedding_client=shedding,
        state_machine=states,
        global_ceiling=3,
    )
    return response, repository, adapter, shedding, states, order


def test_pipeline_claims_before_any_external_call_and_uses_effective_minimum_ceiling() -> None:
    response, repository, adapter, shedding, states, order = pipeline()
    result = asyncio.run(
        response.execute(
            make_command(),
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert order == ["claim", "adapter", "update", "shedding"]
    assert adapter.effective_ceiling == 3
    assert result.capacity.status is ActionStatus.CAPPED
    assert result.capacity.applied == 3
    assert result.shedding.endpoint_policies["checkout"] == "normal"
    assert states.state is ControlState.PROTECT
    assert repository.actions["realtime:test:1"].max_instance_ceiling == 3
    assert repository.actions["realtime:test:1"].signal_evidence == {
        "request_acceleration_rps2": 5.5,
        "control_state_before": "normal",
        "control_state_intended": "protect",
        "effective_capacity_ceiling": 3,
        "response_intent": "detector",
    }


def test_scheduled_commands_use_the_same_service_and_enter_prewarm() -> None:
    response, _, adapter, _, states, _ = pipeline(adapter_status=ActionStatus.DRY_RUN)
    result = asyncio.run(
        response.execute(
            make_command(mode=PredictionMode.SCHEDULED, desired=2),
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert adapter.calls == 1
    assert result.state is states.state is ControlState.PREWARM


def test_recovery_intent_does_not_reenter_protect() -> None:
    order: list[str] = []
    repository = FakeRepository(order)
    adapter = FakeCapacityAdapter(order, status=ActionStatus.DRY_RUN)
    shedding = FakeSheddingClient(order)
    states = ControlStateMachine(ControlState.RECOVERY)
    response = ResponsePipeline(
        repository=repository,
        capacity_adapter=adapter,
        shedding_client=shedding,
        state_machine=states,
        global_ceiling=3,
    )
    command = make_command(desired=2, tier=SheddingLevel.DISABLE_RECOMMENDATIONS).model_copy(
        update={"intent": ResponseIntent.RECOVER}
    )
    result = asyncio.run(
        response.execute(
            command,
            current_capacity=3,
            current_shedding_level=SheddingLevel.CACHED_NONCRITICAL,
            requested_at=NOW,
        )
    )
    assert result.state is states.state is ControlState.RECOVERY


def test_concurrent_duplicate_commands_make_one_capacity_and_tier_attempt() -> None:
    async def exercise():
        response, _, adapter, shedding, _, _ = pipeline()
        command = make_command()
        results = await asyncio.gather(
            response.execute(
                command,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
            response.execute(
                command,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
        )
        return results, adapter, shedding

    results, adapter, shedding = asyncio.run(exercise())
    assert adapter.calls == 1
    assert shedding.calls == 1
    assert sorted(result.duplicate for result in results) == [False, True]


def test_provider_failure_can_increase_protection_but_enters_failure_safe() -> None:
    response, _, _, shedding, states, _ = pipeline(adapter_status=ActionStatus.FAILED)
    result = asyncio.run(
        response.execute(
            make_command(),
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert result.capacity.status is ActionStatus.FAILED
    assert shedding.calls == 1
    assert states.state is ControlState.FAILURE_SAFE


def test_provider_failure_never_lowers_protection_tier() -> None:
    response, repository, _, shedding, _, _ = pipeline(adapter_status=ActionStatus.FAILED)
    result = asyncio.run(
        response.execute(
            make_command(desired=1, tier=SheddingLevel.DISABLE_RECOMMENDATIONS),
            current_capacity=3,
            current_shedding_level=SheddingLevel.CACHED_NONCRITICAL,
            requested_at=NOW,
        )
    )
    assert shedding.calls == 0
    assert result.shedding_error == "shedding_reduction_held_after_capacity_failure"
    assert repository.control_errors == [result.shedding_error]


def test_outcome_persistence_failure_is_unknown_and_stops_tier_attempt() -> None:
    response, repository, _, shedding, states, _ = pipeline()
    repository.fail_update = True
    result = asyncio.run(
        response.execute(
            make_command(),
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert result.capacity.status is ActionStatus.UNKNOWN
    assert result.outcome_persistence_error == "RuntimeError"
    assert shedding.calls == 0
    assert states.state is ControlState.FAILURE_SAFE


def test_unknown_action_blocks_scale_in_and_audits_skipped_hold() -> None:
    response, repository, adapter, shedding, _, _ = pipeline()
    repository.unresolved = True
    result = asyncio.run(
        response.execute(
            make_command(desired=1, tier=SheddingLevel.NORMAL),
            current_capacity=3,
            current_shedding_level=SheddingLevel.DISABLE_RECOMMENDATIONS,
            requested_at=NOW,
        )
    )
    assert result.capacity.status is ActionStatus.SKIPPED
    assert adapter.calls == 0
    assert shedding.calls == 0


def test_shedding_failure_is_sanitized_audited_and_preserves_checkout_boundary() -> None:
    response, repository, _, shedding, states, _ = pipeline(shedding_fail=True)
    result = asyncio.run(
        response.execute(
            make_command(),
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert shedding.calls == 1
    assert result.shedding_error == "shedding_control_failed:SheddingControlError"
    assert repository.control_errors == [result.shedding_error]
    assert states.state is ControlState.FAILURE_SAFE


def test_failure_safe_state_audits_hold_and_performs_no_external_call() -> None:
    order: list[str] = []
    repository = FakeRepository(order)
    adapter = FakeCapacityAdapter(order)
    shedding = FakeSheddingClient(order)
    states = ControlStateMachine(ControlState.FAILURE_SAFE)
    response = ResponsePipeline(
        repository=repository,
        capacity_adapter=adapter,
        shedding_client=shedding,
        state_machine=states,
        global_ceiling=3,
    )
    result = asyncio.run(
        response.execute(
            make_command(),
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert result.capacity.status is ActionStatus.SKIPPED
    assert adapter.calls == 0
    assert shedding.calls == 0
    assert states.state is ControlState.FAILURE_SAFE
