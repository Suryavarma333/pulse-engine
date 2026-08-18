from __future__ import annotations

from datetime import UTC, datetime

from agent.app.config import AgentSettings
from agent.app.runtime import AgentRuntime

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class Clock:
    def now(self):
        return NOW


def test_runtime_assembles_bounded_signal_resources_with_injected_clock() -> None:
    settings = AgentSettings(
        signal_buffer_capacity=7,
        simulated_signal_max_ttl_seconds=45,
        simulated_signal_future_tolerance_seconds=2,
    )
    clock = Clock()
    runtime = AgentRuntime.create(settings, clock=clock)
    assert runtime.settings is settings
    assert runtime.clock is clock
    assert runtime.simulated_signals.capacity == 7
    assert runtime.simulated_signals.max_ttl_seconds == 45
    assert runtime.simulated_signals.future_tolerance_seconds == 2


def test_runtime_defaults_to_a_system_clock() -> None:
    runtime = AgentRuntime.create(AgentSettings())
    assert runtime.clock.now().tzinfo is not None
