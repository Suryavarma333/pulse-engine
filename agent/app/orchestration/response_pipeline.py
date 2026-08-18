from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

    async def read_capacity(self, *, observed_at: datetime) -> Any: ...

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

    async def mark_shedding_outcome(
        self,
        *,
        idempotency_key: str,
        succeeded: bool,
        error_message: str | None = None,
    ) -> Any: ...

    async def has_unresolved(self, *, target_resource: str) -> bool: ...

    async def update_outcome(self, **values: Any) -> Any: ...

    async def record_control_error(
        self,
        *,
        idempotency_key: str,
        error_message: str,
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


class _PriorityArbiter:
    """Serialize mutations while preferring protection over holds and recovery."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._waiters: list[tuple[int, int]] = []
        self._sequence = 0
        self._active = False

    @asynccontextmanager
    async def enter(self, priority: int) -> AsyncIterator[None]:
        async with self._condition:
            token = (priority, self._sequence)
            self._sequence += 1
            self._waiters.append(token)
        acquired = False
        try:
            # Let commands already ready in this event-loop turn enter the priority queue.
            await asyncio.sleep(0)
            async with self._condition:
                await self._condition.wait_for(
                    lambda: not self._active and token == min(self._waiters)
                )
                self._waiters.remove(token)
                self._active = True
                acquired = True
            yield
        finally:
            async with self._condition:
                if acquired:
                    self._active = False
                elif token in self._waiters:
                    self._waiters.remove(token)
                self._condition.notify_all()


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
        self._arbiter = _PriorityArbiter()
        self._active_requirements: dict[
            str, tuple[int, SheddingLevel, datetime | None]
        ] = {}

    async def execute(
        self,
        command: ResponseCommand,
        *,
        current_capacity: int,
        current_shedding_level: SheddingLevel,
        requested_at: datetime,
    ) -> ResponseExecution:
        async with self._arbiter.enter(_command_priority(command)):
            return await self._execute_serialized(
                command,
                current_capacity=current_capacity,
                current_shedding_level=current_shedding_level,
                requested_at=requested_at,
            )

    async def _execute_serialized(
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
        read_capacity = getattr(self._capacity, "read_capacity", None)
        if read_capacity is not None:
            try:
                authoritative = await read_capacity(observed_at=requested_at)
                current_capacity = authoritative.state.desired
            except Exception:
                self._states.transition(ControlEvent.FAULT)
                return await self._audit_hold_serialized(
                    command,
                    current_capacity=current_capacity,
                    requested_at=requested_at,
                    reason="authoritative_capacity_unavailable",
                )
        read_status = getattr(self._shedding, "read_status", None)
        if read_status is not None:
            try:
                current_shedding_level = (await read_status()).level
            except Exception:
                self._states.transition(ControlEvent.FAULT)
                return await self._audit_hold_serialized(
                    command,
                    current_capacity=current_capacity,
                    requested_at=requested_at,
                    reason="authoritative_tier_unavailable",
                )
        if self._states.state is ControlState.FAILURE_SAFE:
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="failure_safe_dependency_hold",
            )

        target = min(command.requested_desired_capacity, effective_ceiling)
        requested_level = command.requested_shedding_level
        active_capacity, active_level, stale_recovery = self._merge_active_requirement(
            command,
            capacity=target,
            level=requested_level,
            requested_at=requested_at,
        )
        if stale_recovery:
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="recovery_superseded_by_newer_protection",
            )
        if command.intent is ResponseIntent.RECOVER and target < active_capacity:
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="recovery_blocked_by_active_capacity_requirement",
            )
        if (
            command.intent is ResponseIntent.RECOVER
            and requested_level is not None
            and requested_level < active_level
        ):
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="recovery_blocked_by_active_protection_requirement",
            )
        if target < current_capacity and command.intent is not ResponseIntent.RECOVER:
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="non_recovery_capacity_decrease_blocked",
            )
        if (
            requested_level is not None
            and requested_level < current_shedding_level
            and command.intent is not ResponseIntent.RECOVER
        ):
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason="non_recovery_protection_decrease_blocked",
            )
        if target < current_capacity and await self._repository.has_unresolved(
            target_resource=command.target_resource
        ):
            return await self._audit_hold_serialized(
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
            duplicate = ResponseExecution(
                action=claim.action,
                capacity=_decision_from_action(claim.action),
                state=self._states.state,
                duplicate=True,
            )
            return await self._resume_duplicate_tier(
                duplicate,
                current_shedding_level=current_shedding_level,
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

        (
            action,
            shedding_result,
            shedding_error,
            shedding_retry_scheduled,
        ) = await self._apply_shedding_stage(
            action=action,
            command=command,
            current_shedding_level=current_shedding_level,
            capacity_safe=decision.status in _SUCCESS_STATUSES,
            requested_at=requested_at,
        )

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
        async with self._arbiter.enter(_command_priority(command)):
            try:
                read_status = getattr(self._shedding, "read_status", None)
                if read_status is not None:
                    current = await read_status()
                    if current.level == level:
                        await self._repository.mark_shedding_outcome(
                            idempotency_key=command.idempotency_key,
                            succeeded=True,
                        )
                        return SheddingControlResult(
                            changed=False,
                            level=level,
                            event_id=None,
                            endpoint_policies={},
                        )
                result = await self._shedding.apply(command=command, level=level)
                await self._repository.mark_shedding_outcome(
                    idempotency_key=command.idempotency_key,
                    succeeded=True,
                )
                return result
            except SheddingControlError as exc:
                await self._mark_tier_pending(
                    command,
                    f"shedding_control_failed:{type(exc).__name__}",
                )
                self._states.transition(ControlEvent.FAULT)
                raise

    async def resume_shedding(self, action: Any) -> SheddingControlResult | None:
        """Resume an incomplete claimed tier stage after restart without capacity work."""

        if (
            getattr(action, "shedding_status", None) != "pending"
            or getattr(action, "requested_shedding_level", None) is None
        ):
            return None
        command = ResponseCommand.model_validate(action.response_command)
        return await self.retry_shedding(
            command,
            level=SheddingLevel(action.requested_shedding_level),
        )

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
        async with self._arbiter.enter(_command_priority(command)):
            return await self._audit_hold_serialized(
                command,
                current_capacity=current_capacity,
                requested_at=requested_at,
                reason=reason,
            )

    async def _audit_hold_serialized(
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
        elif command.intent is ResponseIntent.PREWARM:
            if self._states.state is not ControlState.PROTECT:
                self._states.transition(ControlEvent.SCHEDULED_HORIZON)
        elif command.intent is ResponseIntent.DETECTOR:
            if command.mode is PredictionMode.REALTIME:
                self._states.transition(ControlEvent.CONFIRMED_HIGH)
            elif self._states.state is not ControlState.PROTECT:
                self._states.transition(ControlEvent.SCHEDULED_HORIZON)

    async def _resume_duplicate_tier(
        self,
        execution: ResponseExecution,
        *,
        current_shedding_level: SheddingLevel,
    ) -> ResponseExecution:
        action = execution.action
        requested = getattr(action, "requested_shedding_level", None)
        if getattr(action, "shedding_status", None) != "pending" or requested is None:
            return execution
        persisted_command = ResponseCommand.model_validate(action.response_command)
        level = SheddingLevel(requested)
        if level == current_shedding_level:
            action = await self._repository.mark_shedding_outcome(
                idempotency_key=persisted_command.idempotency_key,
                succeeded=True,
            )
            return ResponseExecution(
                action=action,
                capacity=execution.capacity,
                state=execution.state,
                duplicate=True,
            )
        try:
            result = await self._shedding.apply(command=persisted_command, level=level)
            action = await self._repository.mark_shedding_outcome(
                idempotency_key=persisted_command.idempotency_key,
                succeeded=True,
            )
            return ResponseExecution(
                action=action,
                capacity=execution.capacity,
                state=execution.state,
                duplicate=True,
                shedding=result,
            )
        except SheddingControlError as exc:
            error = f"shedding_control_failed:{type(exc).__name__}"
            await self._mark_tier_pending(persisted_command, error)
            self._states.transition(ControlEvent.FAULT)
            return ResponseExecution(
                action=action,
                capacity=execution.capacity,
                state=self._states.state,
                duplicate=True,
                shedding_error=error,
            )

    async def _apply_shedding_stage(
        self,
        *,
        action: Any,
        command: ResponseCommand,
        current_shedding_level: SheddingLevel,
        capacity_safe: bool,
        requested_at: datetime,
    ) -> tuple[Any, SheddingControlResult | None, str | None, bool]:
        requested_level = command.requested_shedding_level
        if requested_level is None:
            return action, None, None, False
        if requested_level == current_shedding_level:
            action = await self._repository.mark_shedding_outcome(
                idempotency_key=command.idempotency_key,
                succeeded=True,
            )
            return action, None, None, False
        increasing_protection = requested_level > current_shedding_level
        if not increasing_protection and not capacity_safe:
            error = "shedding_reduction_held_after_capacity_failure"
            await self._mark_tier_pending(command, error)
            return action, None, error, False
        try:
            result = await self._shedding.apply(command=command, level=requested_level)
            action = await self._repository.mark_shedding_outcome(
                idempotency_key=command.idempotency_key,
                succeeded=True,
            )
            return action, result, None, False
        except SheddingControlError as exc:
            error = f"shedding_control_failed:{type(exc).__name__}"
            await self._mark_tier_pending(command, error)
            scheduled = False
            if exc.retryable and self._retries is not None:
                try:
                    await self._retries.schedule_shedding(
                        command,
                        level=requested_level,
                        now=requested_at,
                        error_message=error,
                    )
                    scheduled = True
                except Exception:
                    scheduled = False
            self._states.transition(ControlEvent.FAULT)
            return action, None, error, scheduled

    async def _mark_tier_pending(
        self, command: ResponseCommand, error_message: str
    ) -> None:
        await self._record_control_error(command, error_message)
        try:
            await self._repository.mark_shedding_outcome(
                idempotency_key=command.idempotency_key,
                succeeded=False,
                error_message=error_message,
            )
        except Exception:
            return

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

    def _merge_active_requirement(
        self,
        command: ResponseCommand,
        *,
        capacity: int,
        level: SheddingLevel | None,
        requested_at: datetime,
    ) -> tuple[int, SheddingLevel, bool]:
        key = _requirement_key(command)
        previous_requirement = self._active_requirements.get(
            key,
            (capacity, SheddingLevel.NORMAL, None),
        )
        previous_capacity, previous_level, last_protection_at = previous_requirement
        stale_recovery = False
        if command.intent is ResponseIntent.RECOVER:
            stale_recovery = (
                last_protection_at is not None and requested_at <= last_protection_at
            )
            requirement = (
                previous_capacity if stale_recovery else capacity,
                previous_level if stale_recovery or level is None else level,
                last_protection_at,
            )
        elif command.intent is ResponseIntent.HOLD:
            requirement = (previous_capacity, previous_level, last_protection_at)
        else:
            requirement = (
                max(previous_capacity, capacity),
                max(previous_level, level or SheddingLevel.NORMAL),
                (
                    requested_at
                    if last_protection_at is None
                    else max(last_protection_at, requested_at)
                ),
            )
        fully_recovered = (
            command.intent is ResponseIntent.RECOVER
            and not stale_recovery
            and capacity <= command.recovery_plan.capacity_floor
            and level is SheddingLevel.NORMAL
        )
        if fully_recovered:
            self._active_requirements.pop(key, None)
        else:
            self._active_requirements[key] = requirement
        capacities = [
            item[0] for item in self._active_requirements.values()
        ] or [capacity]
        levels = [item[1] for item in self._active_requirements.values()] or [
            level or SheddingLevel.NORMAL
        ]
        return max(capacities), max(levels), stale_recovery


_SUCCESS_STATUSES = {
    ActionStatus.DRY_RUN,
    ActionStatus.NOOP,
    ActionStatus.CAPPED,
    ActionStatus.SUCCEEDED,
    ActionStatus.RECONCILED,
}


def _command_priority(command: ResponseCommand) -> int:
    if command.intent is ResponseIntent.RECOVER:
        return 2
    if command.intent is ResponseIntent.HOLD:
        return 1
    return 0


def _requirement_key(command: ResponseCommand) -> str:
    if command.scheduled_event_id is not None:
        return f"scheduled:{command.scheduled_event_id}"
    return f"mode:{command.mode.value}"


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
