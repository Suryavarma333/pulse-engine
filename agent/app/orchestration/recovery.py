from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.app.orchestration.response_pipeline import ResponseExecution, ResponsePipeline
from agent.app.orchestration.state_machine import ControlEvent, ControlStateMachine
from common.contracts import ResponseCommand
from common.enums import ActionStatus, ControlState, ResponseIntent, SheddingLevel
from common.time import ensure_utc


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    observed_at: datetime
    request_rate_rps: float
    high_load: bool
    current_capacity: int
    current_shedding_level: SheddingLevel

    def __post_init__(self) -> None:
        ensure_utc(self.observed_at, field_name="observed_at")
        if self.request_rate_rps < 0 or self.current_capacity < 0:
            raise ValueError("recovery rates and capacities must be nonnegative")


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    kind: str
    reason_code: str
    reasoning: str
    desired_capacity: int
    shedding_level: SheddingLevel | None
    confirmation_count: int
    execute: bool
    interrupted: bool = False
    final_step: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryCycleResult:
    decision: RecoveryDecision
    command: ResponseCommand
    execution: ResponseExecution


class RecoveryCoordinator:
    """Deterministic sustained-low, cooldown, and one-tier recovery policy."""

    def __init__(self, state_machine: ControlStateMachine) -> None:
        self._states = state_machine
        self._low_confirmations = 0
        self._cooldown_until: datetime | None = None

    @property
    def low_confirmations(self) -> int:
        return self._low_confirmations

    @property
    def cooldown_until(self) -> datetime | None:
        return self._cooldown_until

    def evaluate(
        self,
        observation: RecoveryObservation,
        *,
        command_template: ResponseCommand,
    ) -> RecoveryDecision:
        now = ensure_utc(observation.observed_at, field_name="observed_at")
        plan = command_template.recovery_plan
        if self._states.state is ControlState.FAILURE_SAFE:
            self._low_confirmations = 0
            return self._hold(observation, "recovery_failure_safe_hold")
        if observation.high_load:
            self._low_confirmations = 0
            if self._states.state is ControlState.PROTECT:
                return self._hold(observation, "recovery_high_load_hold")
            self._cooldown_until = None
            self._states.transition(ControlEvent.CONFIRMED_HIGH)
            requested_level = command_template.requested_shedding_level
            protection_level = max(
                observation.current_shedding_level,
                requested_level or SheddingLevel.DISABLE_RECOMMENDATIONS,
            )
            return RecoveryDecision(
                kind="protect",
                reason_code="renewed_high_load",
                reasoning=(
                    "Renewed high load interrupted recovery and requires immediate protection"
                ),
                desired_capacity=max(
                    observation.current_capacity,
                    command_template.requested_desired_capacity,
                ),
                shedding_level=protection_level,
                confirmation_count=0,
                execute=True,
                interrupted=True,
            )

        if self._states.state in {
            ControlState.NORMAL,
            ControlState.WATCH,
            ControlState.PREWARM,
        }:
            self._low_confirmations = 0
            return self._hold(observation, "recovery_not_active")

        if self._cooldown_until is not None:
            if now < self._cooldown_until:
                self._low_confirmations = 0
                if self._states.state is ControlState.COOLDOWN:
                    self._states.transition(ControlEvent.SUSTAINED_LOW)
                return self._hold(observation, "recovery_cooldown_active")
            self._cooldown_until = None
            if self._states.state is ControlState.COOLDOWN:
                self._states.transition(ControlEvent.COOLDOWN_EXPIRED)

        if observation.request_rate_rps > plan.low_threshold_rps:
            self._low_confirmations = 0
            return self._hold(observation, "recovery_load_not_low")

        self._low_confirmations += 1
        if self._low_confirmations < plan.confirmation_count:
            return self._hold(observation, "recovery_confirmation_pending")

        if self._states.state is ControlState.PROTECT:
            self._states.transition(ControlEvent.SUSTAINED_LOW)
        desired = max(
            plan.capacity_floor,
            observation.current_capacity - plan.decrement_step,
        )
        tier = SheddingLevel(
            max(
                int(SheddingLevel.NORMAL),
                int(observation.current_shedding_level) - 1,
            )
        )
        final_step = desired == plan.capacity_floor and tier is SheddingLevel.NORMAL
        return RecoveryDecision(
            kind="recover",
            reason_code="sustained_low_recovery_step",
            reasoning=(
                "Sustained low load satisfied the exact confirmation requirement; "
                "reduce capacity and protection by one bounded step"
            ),
            desired_capacity=desired,
            shedding_level=tier,
            confirmation_count=self._low_confirmations,
            execute=True,
            final_step=final_step,
        )

    def complete(
        self,
        decision: RecoveryDecision,
        *,
        succeeded: bool,
        completed_at: datetime,
        cooldown_seconds: int,
    ) -> None:
        completed_at = ensure_utc(completed_at, field_name="completed_at")
        if not decision.execute or decision.kind != "recover" or not succeeded:
            return
        self._low_confirmations = 0
        if decision.final_step:
            if self._states.state is ControlState.RECOVERY:
                self._states.transition(ControlEvent.FULLY_RECOVERED)
            self._cooldown_until = None
            return
        if self._states.state is ControlState.RECOVERY:
            self._states.transition(ControlEvent.RECOVERY_STEP)
        self._cooldown_until = completed_at + timedelta(seconds=cooldown_seconds)

    def _hold(
        self, observation: RecoveryObservation, reason_code: str
    ) -> RecoveryDecision:
        return RecoveryDecision(
            kind="hold",
            reason_code=reason_code,
            reasoning=f"Recovery held safely: {reason_code}",
            desired_capacity=observation.current_capacity,
            shedding_level=None,
            confirmation_count=self._low_confirmations,
            execute=False,
        )


class RecoveryWorker:
    """Audits every recovery hold and routes every step through ResponsePipeline."""

    def __init__(
        self,
        *,
        coordinator: RecoveryCoordinator,
        response_pipeline: ResponsePipeline,
    ) -> None:
        self._coordinator = coordinator
        self._pipeline = response_pipeline
        self._lock = asyncio.Lock()

    async def run_once(
        self,
        observation: RecoveryObservation,
        *,
        command_template: ResponseCommand,
    ) -> RecoveryCycleResult:
        async with self._lock:
            return await self._run_once(
                observation,
                command_template=command_template,
            )

    async def _run_once(
        self,
        observation: RecoveryObservation,
        *,
        command_template: ResponseCommand,
    ) -> RecoveryCycleResult:
        decision = self._coordinator.evaluate(
            observation, command_template=command_template
        )
        command = _recovery_command(command_template, observation, decision)
        if decision.execute:
            execution = await self._pipeline.execute(
                command,
                current_capacity=observation.current_capacity,
                current_shedding_level=observation.current_shedding_level,
                requested_at=observation.observed_at,
            )
        else:
            execution = await self._pipeline.audit_hold(
                command,
                current_capacity=observation.current_capacity,
                requested_at=observation.observed_at,
                reason=decision.reason_code,
            )
        succeeded = (
            execution.capacity.status
            in {
                ActionStatus.DRY_RUN,
                ActionStatus.NOOP,
                ActionStatus.CAPPED,
                ActionStatus.SUCCEEDED,
                ActionStatus.RECONCILED,
            }
            and execution.shedding_error is None
        )
        self._coordinator.complete(
            decision,
            succeeded=succeeded,
            completed_at=observation.observed_at,
            cooldown_seconds=command.recovery_plan.cooldown_seconds,
        )
        return RecoveryCycleResult(decision, command, execution)


def _recovery_command(
    template: ResponseCommand,
    observation: RecoveryObservation,
    decision: RecoveryDecision,
) -> ResponseCommand:
    timestamp_key = int(observation.observed_at.timestamp() * 1_000_000)
    payload = template.model_dump()
    evidence = {
        **template.signal_evidence,
        "recovery_observed_rps": observation.request_rate_rps,
        "recovery_confirmation_count": decision.confirmation_count,
        "recovery_decision": decision.kind,
        "recovery_interrupted": decision.interrupted,
    }
    payload.update(
        {
            "idempotency_key": (
                f"recovery:{template.correlation_id}:{timestamp_key}:{decision.reason_code}"
            ),
            "requested_desired_capacity": decision.desired_capacity,
            "requested_shedding_level": decision.shedding_level,
            "intent": (
                ResponseIntent.PROTECT
                if decision.kind == "protect"
                else ResponseIntent.RECOVER
                if decision.kind == "recover"
                else ResponseIntent.HOLD
            ),
            "reason_code": decision.reason_code,
            "reasoning": decision.reasoning,
            "signal_evidence": evidence,
        }
    )
    return ResponseCommand.model_validate(payload)


__all__ = [
    "RecoveryCoordinator",
    "RecoveryCycleResult",
    "RecoveryDecision",
    "RecoveryObservation",
    "RecoveryWorker",
]
