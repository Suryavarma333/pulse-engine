from __future__ import annotations

import pytest

from agent.app.orchestration.state_machine import (
    ControlEvent,
    ControlStateMachine,
    InvalidStateTransition,
)
from common.enums import ControlState


def test_realtime_state_path_covers_watch_protect_recovery_cooldown_normal() -> None:
    machine = ControlStateMachine()
    assert machine.transition(ControlEvent.QUALIFYING_EVIDENCE).current is ControlState.WATCH
    assert machine.transition(ControlEvent.CONFIRMED_HIGH).current is ControlState.PROTECT
    assert machine.transition(ControlEvent.SUSTAINED_LOW).current is ControlState.RECOVERY
    assert machine.transition(ControlEvent.RECOVERY_STEP).current is ControlState.COOLDOWN
    assert machine.transition(ControlEvent.COOLDOWN_EXPIRED).current is ControlState.RECOVERY
    assert machine.transition(ControlEvent.FULLY_RECOVERED).current is ControlState.NORMAL


def test_scheduled_and_failure_safe_paths_are_executable() -> None:
    machine = ControlStateMachine()
    assert machine.transition(ControlEvent.SCHEDULED_HORIZON).current is ControlState.PREWARM
    assert machine.transition(ControlEvent.EVENT_CANCELLED_LOW).current is ControlState.RECOVERY
    assert machine.transition(ControlEvent.FAULT).current is ControlState.FAILURE_SAFE
    assert (
        machine.transition(
            ControlEvent.DEPENDENCIES_RECOVERED,
            recovered_target=ControlState.RECOVERY,
        ).current
        is ControlState.RECOVERY
    )


def test_high_load_interrupts_recovery_and_cooldown_immediately() -> None:
    machine = ControlStateMachine(ControlState.RECOVERY)
    machine.transition(ControlEvent.RECOVERY_STEP)
    assert machine.state is ControlState.COOLDOWN
    assert machine.transition(ControlEvent.CONFIRMED_HIGH).current is ControlState.PROTECT


def test_invalid_transition_fails_closed() -> None:
    machine = ControlStateMachine()
    with pytest.raises(InvalidStateTransition):
        machine.transition(ControlEvent.RECOVERY_STEP)
    machine.transition(ControlEvent.FAULT)
    with pytest.raises(InvalidStateTransition):
        machine.transition(
            ControlEvent.DEPENDENCIES_RECOVERED,
            recovered_target=ControlState.NORMAL,
        )
