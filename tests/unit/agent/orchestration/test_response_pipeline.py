from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlEvent, ControlStateMachine
from agent.app.services.shedding_client import (
    SheddingControlError,
    SheddingControlResult,
)
from common.contracts import CapacityDecision, CapacityState, RecoveryPlan, ResponseCommand
from common.enums import (
    ActionStatus,
    ControlState,
    ExecutionMode,
    PredictionMode,
    ProviderStatus,
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
        self.retry_requests = []

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
                response_command=command.model_dump(mode="json"),
                requested_shedding_level=(
                    None
                    if command.requested_shedding_level is None
                    else int(command.requested_shedding_level)
                ),
                shedding_status=(
                    "not_requested"
                    if command.requested_shedding_level is None
                    else "pending"
                ),
                shedding_attempts=0,
                shedding_error=None,
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

    async def mark_shedding_outcome(self, **values):
        action = self.actions[values["idempotency_key"]]
        action.shedding_status = "succeeded" if values["succeeded"] else "pending"
        if not values["succeeded"]:
            action.shedding_attempts += 1
        action.shedding_error = values.get("error_message")
        return action


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


class FakeRetryRepository:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository

    async def schedule_shedding(self, command, *, level, now, error_message):
        self.repository.retry_requests.append((command, level, now, error_message))
        return self


class FailingRetryRepository(FakeRetryRepository):
    async def schedule_shedding(self, command, *, level, now, error_message):
        raise RuntimeError("retry store unavailable")


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
        retry_repository=FakeRetryRepository(repository),
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


def test_non_recovery_command_never_lowers_capacity_or_protection_tier() -> None:
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
    assert result.capacity.status is ActionStatus.SKIPPED
    assert result.shedding_error is None
    assert repository.control_errors == []

    tier_only = asyncio.run(
        response.execute(
            make_command(desired=3, tier=SheddingLevel.NORMAL).model_copy(
                update={"idempotency_key": "realtime:tier-decrease"}
            ),
            current_capacity=3,
            current_shedding_level=SheddingLevel.CACHED_NONCRITICAL,
            requested_at=NOW,
        )
    )
    assert tier_only.capacity.status is ActionStatus.SKIPPED
    assert shedding.calls == 0


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
    assert result.shedding_retry_scheduled is True
    assert repository.control_errors == [result.shedding_error]
    assert repository.retry_requests[0][0].idempotency_key == "realtime:test:1"
    assert repository.retry_requests[0][1] is SheddingLevel.DISABLE_RECOMMENDATIONS
    assert states.state is ControlState.FAILURE_SAFE


def test_atomic_tier_intent_survives_retry_enqueue_failure_and_restart() -> None:
    order: list[str] = []
    repository = FakeRepository(order)
    adapter = FakeCapacityAdapter(order, status=ActionStatus.DRY_RUN)
    shedding = FakeSheddingClient(order, fail=True)
    first = ResponsePipeline(
        repository=repository,
        capacity_adapter=adapter,
        shedding_client=shedding,
        state_machine=ControlStateMachine(),
        global_ceiling=3,
        retry_repository=FailingRetryRepository(repository),
    )
    command = make_command(desired=3)

    failed = asyncio.run(
        first.execute(
            command,
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    persisted = repository.actions[command.idempotency_key]
    assert failed.shedding_retry_scheduled is False
    assert persisted.shedding_status == "pending"
    assert persisted.shedding_attempts == 1

    shedding.fail = False
    restarted = ResponsePipeline(
        repository=repository,
        capacity_adapter=adapter,
        shedding_client=shedding,
        state_machine=ControlStateMachine(),
        global_ceiling=3,
    )
    resumed = asyncio.run(
        restarted.execute(
            command,
            current_capacity=3,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )

    assert resumed.duplicate is True
    assert resumed.shedding is not None
    assert repository.actions[command.idempotency_key].shedding_status == "succeeded"
    assert adapter.calls == 1


def test_durable_tier_retry_observes_completed_outbox_without_second_mutation() -> None:
    class AlreadyAppliedShedding(FakeSheddingClient):
        async def read_status(self):
            return SimpleNamespace(level=SheddingLevel.DISABLE_RECOMMENDATIONS)

    order: list[str] = []
    repository = FakeRepository(order)
    command = make_command(desired=3)
    asyncio.run(
        repository.claim(
            command,
            previous_desired_capacity=1,
            execution_mode=ExecutionMode.DRY_RUN,
            requested_at=NOW,
            effective_ceiling=3,
        )
    )
    shedding = AlreadyAppliedShedding(order)
    response = ResponsePipeline(
        repository=repository,
        capacity_adapter=FakeCapacityAdapter(order),
        shedding_client=shedding,
        state_machine=ControlStateMachine(),
        global_ceiling=3,
    )

    result = asyncio.run(
        response.retry_shedding(
            command,
            level=SheddingLevel.DISABLE_RECOMMENDATIONS,
        )
    )

    assert result.changed is False
    assert shedding.calls == 0
    assert repository.actions[command.idempotency_key].shedding_status == "succeeded"


def test_cross_mode_arbiter_serializes_and_never_allows_protection_decrease() -> None:
    class AuthoritativeAdapter(FakeCapacityAdapter):
        def __init__(self, order):
            super().__init__(order, status=ActionStatus.DRY_RUN)
            self.current = 1
            self.mutations = []

        async def read_capacity(self, *, observed_at):
            return SimpleNamespace(
                state=CapacityState(
                    desired=self.current,
                    in_service=self.current,
                    pending=0,
                    observed_at=observed_at,
                    provider_status=ProviderStatus.SIMULATED,
                )
            )

        async def execute(self, *, requested_capacity, effective_ceiling, observed_at):
            result = await super().execute(
                requested_capacity=requested_capacity,
                effective_ceiling=effective_ceiling,
                observed_at=observed_at,
            )
            self.current = int(result.applied or self.current)
            self.mutations.append(self.current)
            return result

    async def exercise():
        order: list[str] = []
        repository = FakeRepository(order)
        adapter = AuthoritativeAdapter(order)
        response = ResponsePipeline(
            repository=repository,
            capacity_adapter=adapter,
            shedding_client=FakeSheddingClient(order),
            state_machine=ControlStateMachine(),
            global_ceiling=3,
        )
        realtime = make_command(key="realtime:protect", desired=3, tier=None).model_copy(
            update={"intent": ResponseIntent.PROTECT}
        )
        scheduled = make_command(
            key="scheduled:prewarm",
            mode=PredictionMode.SCHEDULED,
            desired=2,
            tier=None,
        ).model_copy(update={"intent": ResponseIntent.PREWARM})
        await asyncio.gather(
            response.execute(
                realtime,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
            response.execute(
                scheduled,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
        )
        return adapter

    adapter = asyncio.run(exercise())
    assert adapter.current == 3
    assert adapter.mutations == sorted(adapter.mutations)


def test_priority_arbiter_supersedes_concurrent_recovery_with_protection() -> None:
    async def exercise():
        order: list[str] = []
        repository = FakeRepository(order)
        adapter = FakeCapacityAdapter(order, status=ActionStatus.DRY_RUN)
        response = ResponsePipeline(
            repository=repository,
            capacity_adapter=adapter,
            shedding_client=FakeSheddingClient(order),
            state_machine=ControlStateMachine(ControlState.RECOVERY),
            global_ceiling=3,
        )
        recovery = make_command(key="realtime:recover", desired=2, tier=None).model_copy(
            update={"intent": ResponseIntent.RECOVER}
        )
        protection = make_command(key="realtime:renew", desired=3, tier=None).model_copy(
            update={"intent": ResponseIntent.PROTECT}
        )
        recovered, protected = await asyncio.gather(
            response.execute(
                recovery,
                current_capacity=3,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
            response.execute(
                protection,
                current_capacity=3,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
        )
        return recovered, protected, adapter

    recovered, protected, adapter = asyncio.run(exercise())
    assert protected.capacity.status is ActionStatus.DRY_RUN
    assert recovered.capacity.status is ActionStatus.SKIPPED
    assert adapter.calls == 1


def test_recovery_merges_distinct_active_scheduled_requirements() -> None:
    response, _, adapter, _, states, _ = pipeline(adapter_status=ActionStatus.DRY_RUN)
    first_event = uuid4()
    second_event = uuid4()
    first = make_command(
        key="scheduled:first:prewarm",
        mode=PredictionMode.SCHEDULED,
        desired=3,
        tier=None,
    ).model_copy(update={"scheduled_event_id": first_event})
    second = make_command(
        key="scheduled:second:prewarm",
        mode=PredictionMode.SCHEDULED,
        desired=2,
        tier=None,
    ).model_copy(update={"scheduled_event_id": second_event})
    asyncio.run(
        response.execute(
            first,
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    held = asyncio.run(
        response.execute(
            second,
            current_capacity=3,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW,
        )
    )
    assert held.capacity.status is ActionStatus.SKIPPED
    states.transition(ControlEvent.EVENT_CANCELLED_LOW)

    first_recovery = first.model_copy(
        update={
            "idempotency_key": "scheduled:first:recover",
            "intent": ResponseIntent.RECOVER,
            "requested_desired_capacity": 1,
        }
    )
    blocked = asyncio.run(
        response.execute(
            first_recovery,
            current_capacity=3,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW.replace(second=1),
        )
    )
    assert blocked.capacity.status is ActionStatus.SKIPPED

    second_recovery = second.model_copy(
        update={
            "idempotency_key": "scheduled:second:recover",
            "intent": ResponseIntent.RECOVER,
            "requested_desired_capacity": 1,
        }
    )
    completed = asyncio.run(
        response.execute(
            second_recovery,
            current_capacity=3,
            current_shedding_level=SheddingLevel.NORMAL,
            requested_at=NOW.replace(second=2),
        )
    )
    assert completed.capacity.status is ActionStatus.DRY_RUN
    assert adapter.calls == 2


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


def test_verified_dependencies_restore_failure_safe_and_elevated_restart_state() -> None:
    response, _, _, _, states, _ = pipeline()
    states.transition(ControlEvent.FAULT)
    response.recover_dependencies(target=ControlState.WATCH)
    assert states.state is ControlState.WATCH

    restarted, _, _, _, restarted_states, _ = pipeline()
    restarted.recover_dependencies(target=ControlState.RECOVERY)
    assert restarted_states.state is ControlState.RECOVERY
