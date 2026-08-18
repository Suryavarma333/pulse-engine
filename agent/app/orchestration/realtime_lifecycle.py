from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from agent.app.detection.realtime import RealtimeDecision
from agent.app.orchestration.recovery import (
    RecoveryObservation,
    RecoveryWorker,
)
from agent.app.orchestration.state_machine import ControlEvent, ControlStateMachine
from common.contracts import ResponseCommand
from common.enums import ControlState, SheddingLevel


@dataclass(frozen=True, slots=True)
class RealtimeControlObservation:
    observed_at: datetime
    current_capacity: int
    current_shedding_level: SheddingLevel


ObservationProvider = Callable[[], Awaitable[RealtimeControlObservation]]


class RealtimeControlLifecycle:
    """Connect detector decisions to WATCH and the shared recovery pipeline."""

    def __init__(
        self,
        *,
        state_machine: ControlStateMachine,
        recovery_worker: RecoveryWorker,
        observation_provider: ObservationProvider,
    ) -> None:
        self._states = state_machine
        self._recovery = recovery_worker
        self._observe = observation_provider

    async def handle(
        self,
        decision: RealtimeDecision,
        active_command: ResponseCommand | None,
    ) -> bool:
        state = self._states.state
        if decision.trigger:
            return False
        if state is ControlState.NORMAL and decision.qualifying:
            self._states.transition(ControlEvent.QUALIFYING_EVIDENCE)
            return False
        if state is ControlState.WATCH:
            self._states.transition(
                ControlEvent.QUALIFYING_EVIDENCE
                if decision.qualifying
                else ControlEvent.EVIDENCE_CLEARED
            )
            return False
        if active_command is None or state not in {
            ControlState.PROTECT,
            ControlState.RECOVERY,
            ControlState.COOLDOWN,
            ControlState.FAILURE_SAFE,
        }:
            return state is ControlState.NORMAL

        observed = await self._observe()
        request_rate = decision.snapshot.origin_request_rate_rps
        await self._recovery.run_once(
            RecoveryObservation(
                observed_at=observed.observed_at,
                request_rate_rps=request_rate,
                high_load=request_rate > active_command.recovery_plan.low_threshold_rps,
                current_capacity=observed.current_capacity,
                current_shedding_level=observed.current_shedding_level,
            ),
            command_template=active_command,
        )
        return self._states.state is ControlState.NORMAL


__all__ = [
    "ObservationProvider",
    "RealtimeControlLifecycle",
    "RealtimeControlObservation",
]
