from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from common.enums import ControlState


class ControlEvent(StrEnum):
    QUALIFYING_EVIDENCE = "qualifying_evidence"
    SCHEDULED_HORIZON = "scheduled_horizon"
    CONFIRMED_HIGH = "confirmed_high"
    EVIDENCE_CLEARED = "evidence_cleared"
    SUSTAINED_LOW = "sustained_low"
    RECOVERY_STEP = "recovery_step"
    FULLY_RECOVERED = "fully_recovered"
    COOLDOWN_EXPIRED = "cooldown_expired"
    EVENT_CANCELLED_LOW = "event_cancelled_low"
    FAULT = "fault"
    DEPENDENCIES_RECOVERED = "dependencies_recovered"


@dataclass(frozen=True, slots=True)
class StateTransition:
    previous: ControlState
    current: ControlState
    event: ControlEvent
    changed: bool


class InvalidStateTransition(ValueError):
    """The requested control event is unsafe from the current state."""


class ControlStateMachine:
    """Executable safety state machine shared by both detector modes."""

    def __init__(self, state: ControlState = ControlState.NORMAL) -> None:
        self._state = state
        self._cooldown_resume_state = ControlState.RECOVERY

    @property
    def state(self) -> ControlState:
        return self._state

    def transition(
        self,
        event: ControlEvent,
        *,
        recovered_target: ControlState | None = None,
    ) -> StateTransition:
        previous = self._state
        if event is ControlEvent.FAULT:
            target = ControlState.FAILURE_SAFE
        elif event is ControlEvent.CONFIRMED_HIGH:
            target = ControlState.PROTECT
        elif previous is ControlState.NORMAL:
            target = self._from_normal(event)
        elif previous is ControlState.WATCH:
            target = self._from_watch(event)
        elif previous is ControlState.PREWARM:
            target = self._from_prewarm(event)
        elif previous is ControlState.PROTECT:
            target = self._from_protect(event)
        elif previous is ControlState.RECOVERY:
            target = self._from_recovery(event)
        elif previous is ControlState.COOLDOWN:
            target = self._from_cooldown(event)
        else:
            target = self._from_failure_safe(event, recovered_target)

        self._state = target
        return StateTransition(previous, target, event, previous is not target)

    def _from_normal(self, event: ControlEvent) -> ControlState:
        if event is ControlEvent.QUALIFYING_EVIDENCE:
            return ControlState.WATCH
        if event is ControlEvent.SCHEDULED_HORIZON:
            return ControlState.PREWARM
        if event in {ControlEvent.EVIDENCE_CLEARED, ControlEvent.FULLY_RECOVERED}:
            return ControlState.NORMAL
        raise InvalidStateTransition(f"{event.value} is invalid from normal")

    def _from_watch(self, event: ControlEvent) -> ControlState:
        if event is ControlEvent.EVIDENCE_CLEARED:
            return ControlState.NORMAL
        if event is ControlEvent.QUALIFYING_EVIDENCE:
            return ControlState.WATCH
        raise InvalidStateTransition(f"{event.value} is invalid from watch")

    def _from_prewarm(self, event: ControlEvent) -> ControlState:
        if event is ControlEvent.SCHEDULED_HORIZON:
            return ControlState.PREWARM
        if event is ControlEvent.EVENT_CANCELLED_LOW:
            return ControlState.RECOVERY
        raise InvalidStateTransition(f"{event.value} is invalid from prewarm")

    def _from_protect(self, event: ControlEvent) -> ControlState:
        if event is ControlEvent.SUSTAINED_LOW:
            return ControlState.RECOVERY
        if event is ControlEvent.QUALIFYING_EVIDENCE:
            return ControlState.PROTECT
        raise InvalidStateTransition(f"{event.value} is invalid from protect")

    def _from_recovery(self, event: ControlEvent) -> ControlState:
        if event is ControlEvent.RECOVERY_STEP:
            self._cooldown_resume_state = ControlState.RECOVERY
            return ControlState.COOLDOWN
        if event is ControlEvent.FULLY_RECOVERED:
            return ControlState.NORMAL
        if event is ControlEvent.SUSTAINED_LOW:
            return ControlState.RECOVERY
        raise InvalidStateTransition(f"{event.value} is invalid from recovery")

    def _from_cooldown(self, event: ControlEvent) -> ControlState:
        if event is ControlEvent.COOLDOWN_EXPIRED:
            return self._cooldown_resume_state
        if event is ControlEvent.SUSTAINED_LOW:
            return ControlState.COOLDOWN
        raise InvalidStateTransition(f"{event.value} is invalid from cooldown")

    @staticmethod
    def _from_failure_safe(
        event: ControlEvent,
        recovered_target: ControlState | None,
    ) -> ControlState:
        if event is ControlEvent.FAULT:
            return ControlState.FAILURE_SAFE
        if event is not ControlEvent.DEPENDENCIES_RECOVERED:
            raise InvalidStateTransition(f"{event.value} is invalid from failure_safe")
        if recovered_target not in {
            ControlState.WATCH,
            ControlState.PROTECT,
            ControlState.RECOVERY,
        }:
            raise InvalidStateTransition(
                "dependency recovery requires watch, protect, or recovery target"
            )
        return recovered_target


__all__ = [
    "ControlEvent",
    "ControlStateMachine",
    "InvalidStateTransition",
    "StateTransition",
]
