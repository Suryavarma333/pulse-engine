from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from agent.app.orchestration.state_machine import ControlEvent, ControlStateMachine
from agent.app.services.shedding_client import (
    SheddingControlError,
    SheddingControlResult,
)
from common.contracts import CapacityDecision, ResponseCommand
from common.enums import (
    ActionStatus,
    ControlState,
    ExecutionMode,
    PredictionMode,
    ResponseIntent,
    SheddingLevel,
)
from common.time import ensure_utc


class CapacityAdapter(Protocol):
    execution_mode: ExecutionMode
    target_resource: str

    async def execute(
        self,
        *,
        requested_capacity: int,
        effective_ceiling: int,
        observed_at: datetime,
    ) -> CapacityDecision: ...


class SheddingClient(Protocol):
    async def apply(
        self,
        *,
        command: ResponseCommand,
        level: SheddingLevel,
    ) -> SheddingControlResult: ...


class ActionRepository(Protocol):
    async def claim(
        self,
        command: ResponseCommand,
        *,
        previous_desired_capacity: int,
        execution_mode: ExecutionMode,
        requested_at: datetime,
        effective_ceiling: int | None = None,
    ) -> Any: ...


class RetryRepository(Protocol):
    async def schedule_shedding(
        self,
        command: ResponseCommand,
        *,
        level: SheddingLevel,
        now: datetime,
        error_message: str,
    ) -> Any: ...

    async def has_unresolved(self, *, target_resource: str) -> bool: ...

    async def update_outcome(self, **values: Any) -> Any: ...

    async def record_control_error(
        self,
        *,
        idempotency_key: str,
        error_message: str,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ResponseExecution:
    action: Any
    capacity: CapacityDecision
    state: ControlState
    duplicate: bool = False
    shedding: SheddingControlResult | None = None
    shedding_error: str | None = None
    shedding_retry_scheduled: bool = False
    outcome_persistence_error: str | None = None


class ResponsePipeline:
    """The only service allowed to execute detector response commands."""

    def __init__(
        self,
        *,
        repository: ActionRepository,
        capacity_adapter: CapacityAdapter,
        shedding_client: SheddingClient,
        state_machine: ControlStateMachine,
        global_ceiling: int,
        retry_repository: RetryRepository | None = None,
    ) -> None:
        if not 1 <= global_ceiling <= 100:
            raise ValueError("global_ceiling must be between 1 and 100")
        if capacity_adapter.target_resource == "":
            raise ValueError("capacity adapter target is required")
        self._repository = repository
        self._capacity = capacity_adapter
        self._shedding = shedding_client
        self._states = state_machine
        self._global_ceiling = global_ceiling
        self._retries = retry_repository

    async def execute(
        self,
        command: ResponseCommand,
        *,
        current_capacity: int,
        current_shedding_level: SheddingLevel,
        requested_at: datetime,
    ) -> ResponseExecution:
        requested_at = ensure_utc(requested_at, field_name="requested_at")
        if current_capacity < 0:
            raise ValueError("current_capacity must be nonnegative")
        if command.target_resource != self._capacity.target_resource:
            raise ValueError("command target does not match the configured capacity adapter")
        effective_ceiling = min(self._global_ceiling, command.maximum_ceiling)
        if command.recovery_plan.capacity_floor > effective_ceiling:
            self._states.transition(ControlEvent.FAULT)
            raise ValueError("recovery floor exceeds the effective capacity ceiling")
        command = _with_audit_state(command, self._states.state, effective_ceiling)
        if self._states.state is ControlState.FAILURE_SAFE:
            return await self.audit_hold(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="failure_safe_dependency_hold",
            )

        target = min(command.requested_desired_capacity, effective_ceiling)
        if target < current_capacity and await self._repository.has_unresolved(
            target_resource=command.target_resource
        ):
            return await self.audit_hold(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="scale_in_blocked_by_unresolved_action",
            )

        claim = await self._repository.claim(
            command,
            previous_desired_capacity=current_capacity,
            execution_mode=self._capacity.execution_mode,
            requested_at=requested_at,
            effective_ceiling=effective_ceiling,
        )
        if not claim.claimed:
            return ResponseExecution(
                action=claim.action,
                capacity=_decision_from_action(claim.action),
                state=self._states.state,
                duplicate=True,
            )

        self._enter_command_state(command)
        decision = await self._capacity.execute(
            requested_capacity=command.requested_desired_capacity,
            effective_ceiling=effective_ceiling,
            observed_at=requested_at,
        )
        scale_in_applied = (
            decision.applied is not None
            and decision.applied < current_capacity
            and decision.status in _SUCCESS_STATUSES
        )
        cooldown_until = (
            requested_at + timedelta(seconds=command.recovery_plan.cooldown_seconds)
            if scale_in_applied
            else None
        )
        try:
            action = await self._repository.update_outcome(
                idempotency_key=command.idempotency_key,
                status=decision.status,
                applied_desired_capacity=decision.applied,
                executed_at=requested_at,
                provider_request_id=decision.provider_request_id,
                error_message=decision.sanitized_error,
                cooldown_until=cooldown_until,
            )
        except Exception as exc:
            self._states.transition(ControlEvent.FAULT)
            unknown = CapacityDecision(
                requested=decision.requested,
                applied=decision.applied,
                ceiling=decision.ceiling,
                execution_mode=decision.execution_mode,
                status=ActionStatus.UNKNOWN,
                provider_request_id=decision.provider_request_id,
                sanitized_error="outcome_persistence_failed",
            )
            return ResponseExecution(
                action=claim.action,
                capacity=unknown,
                state=self._states.state,
                outcome_persistence_error=type(exc).__name__,
            )

        shedding_result: SheddingControlResult | None = None
        shedding_error: str | None = None
        shedding_retry_scheduled = False
        requested_level = command.requested_shedding_level
        if requested_level is not None and requested_level != current_shedding_level:
            increasing_protection = requested_level > current_shedding_level
            capacity_safe = decision.status in _SUCCESS_STATUSES
            if increasing_protection or capacity_safe:
                try:
                    shedding_result = await self._shedding.apply(
                        command=command, level=requested_level
                    )
                except SheddingControlError as exc:
                    shedding_error = f"shedding_control_failed:{type(exc).__name__}"
                    await self._record_control_error(command, shedding_error)
                    try:
                        if exc.retryable and self._retries is not None:
                            await self._retries.schedule_shedding(
                                command,
                                level=requested_level,
                                now=requested_at,
                                error_message=shedding_error,
                            )
                            shedding_retry_scheduled = True
                    finally:
                        self._states.transition(ControlEvent.FAULT)
            else:
                shedding_error = "shedding_reduction_held_after_capacity_failure"
                await self._record_control_error(command, shedding_error)

        if decision.status in {ActionStatus.FAILED, ActionStatus.UNKNOWN}:
            self._states.transition(ControlEvent.FAULT)
        return ResponseExecution(
            action=action,
            capacity=decision,
            state=self._states.state,
            shedding=shedding_result,
            shedding_error=shedding_error,
            shedding_retry_scheduled=shedding_retry_scheduled,
        )

    async def retry_shedding(
        self,
        command: ResponseCommand,
        *,
        level: SheddingLevel,
    ) -> SheddingControlResult:
        """Retry only a persisted tier operation; never repeat the capacity mutation."""

        try:
            return await self._shedding.apply(command=command, level=level)
        except SheddingControlError:
            self._states.transition(ControlEvent.FAULT)
            raise

    def recover_dependencies(self, *, target: ControlState) -> None:
        """Exit failure-safe only after the maintenance worker verifies dependencies."""

        if self._states.state is ControlState.NORMAL and target in {
            ControlState.PROTECT,
            ControlState.RECOVERY,
        }:
            self._states.transition(ControlEvent.FAULT)
        if self._states.state is ControlState.FAILURE_SAFE:
            self._states.transition(
                ControlEvent.DEPENDENCIES_RECOVERED,
                recovered_target=target,
            )

    async def audit_hold(
        self,
        command: ResponseCommand,
        *,
        current_capacity: int,
        requested_at: datetime,
        reason: str,
    ) -> ResponseExecution:
        requested_at = ensure_utc(requested_at, field_name="requested_at")
        effective_ceiling = min(self._global_ceiling, command.maximum_ceiling)
        command = _with_audit_state(command, self._states.state, effective_ceiling)
        hold_command = command.model_copy(
            update={
                "reason_code": reason[:60],
                "reasoning": f"Response held safely: {reason}"[:2_000],
                "requested_desired_capacity": current_capacity,
                "requested_shedding_level": None,
                "intent": ResponseIntent.HOLD,
            }
        )
        claim = await self._repository.claim(
            hold_command,
            previous_desired_capacity=current_capacity,
            execution_mode=self._capacity.execution_mode,
            requested_at=requested_at,
            effective_ceiling=effective_ceiling,
        )
        if not claim.claimed:
            return ResponseExecution(
                action=claim.action,
                capacity=_decision_from_action(claim.action),
                state=self._states.state,
                duplicate=True,
            )
        decision = CapacityDecision(
            requested=current_capacity,
            applied=None,
            ceiling=effective_ceiling,
            execution_mode=self._capacity.execution_mode,
            status=ActionStatus.SKIPPED,
        )
        action = await self._repository.update_outcome(
            idempotency_key=hold_command.idempotency_key,
            status=ActionStatus.SKIPPED,
            applied_desired_capacity=None,
            executed_at=requested_at,
            error_message=None,
            cooldown_until=None,
        )
        return ResponseExecution(action, decision, self._states.state)

    def _enter_command_state(self, command: ResponseCommand) -> None:
        if command.intent is ResponseIntent.PROTECT:
            self._states.transition(ControlEvent.CONFIRMED_HIGH)
        elif (
            command.intent is ResponseIntent.PREWARM
            and self._states.state is ControlState.NORMAL
        ):
            self._states.transition(ControlEvent.SCHEDULED_HORIZON)
        elif command.intent is ResponseIntent.DETECTOR:
            if command.mode is PredictionMode.REALTIME:
                self._states.transition(ControlEvent.CONFIRMED_HIGH)
            elif self._states.state is ControlState.NORMAL:
                self._states.transition(ControlEvent.SCHEDULED_HORIZON)

    async def _record_control_error(
        self, command: ResponseCommand, error_message: str
    ) -> None:
        try:
            await self._repository.record_control_error(
                idempotency_key=command.idempotency_key,
                error_message=error_message,
            )
        except Exception:
            return


_SUCCESS_STATUSES = {
    ActionStatus.DRY_RUN,
    ActionStatus.NOOP,
    ActionStatus.CAPPED,
    ActionStatus.SUCCEEDED,
    ActionStatus.RECONCILED,
}


def _decision_from_action(action: Any) -> CapacityDecision:
    return CapacityDecision(
        requested=action.requested_desired_capacity,
        applied=action.applied_desired_capacity,
        ceiling=action.max_instance_ceiling,
        execution_mode=ExecutionMode(action.execution_mode),
        status=ActionStatus(action.status),
        provider_request_id=action.provider_request_id,
        sanitized_error=action.error_message,
    )


def _with_audit_state(
    command: ResponseCommand,
    current_state: ControlState,
    effective_ceiling: int,
) -> ResponseCommand:
    if command.intent in {ResponseIntent.PROTECT} or (
        command.intent is ResponseIntent.DETECTOR
        and command.mode is PredictionMode.REALTIME
    ):
        intended_state = ControlState.PROTECT
    elif command.intent is ResponseIntent.PREWARM or (
        command.intent is ResponseIntent.DETECTOR
        and command.mode is PredictionMode.SCHEDULED
    ):
        intended_state = ControlState.PREWARM
    elif command.intent is ResponseIntent.RECOVER:
        intended_state = ControlState.RECOVERY
    else:
        intended_state = current_state
    payload = command.model_dump()
    payload["signal_evidence"] = {
        **command.signal_evidence,
        "control_state_before": current_state.value,
        "control_state_intended": intended_state.value,
        "effective_capacity_ceiling": effective_ceiling,
        "response_intent": command.intent.value,
    }
    return ResponseCommand.model_validate(payload)


__all__ = ["ResponseExecution", "ResponsePipeline"]
