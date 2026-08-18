from __future__ import annotations

from dataclasses import dataclass

from agent.app.config import AgentSettings
from agent.app.metrics.providers.simulated import SimulatedSignalBuffer
from common.time import Clock, SystemClock


@dataclass(slots=True)
class AgentRuntime:
    """Phase-safe runtime resources shared by the internal API and worker assembly."""

    settings: AgentSettings
    clock: Clock
    simulated_signals: SimulatedSignalBuffer

    @classmethod
    def create(
        cls, settings: AgentSettings, *, clock: Clock | None = None
    ) -> AgentRuntime:
        return cls(
            settings=settings,
            clock=clock or SystemClock(),
            simulated_signals=SimulatedSignalBuffer(
                capacity=settings.signal_buffer_capacity,
                max_ttl_seconds=settings.simulated_signal_max_ttl_seconds,
                future_tolerance_seconds=settings.simulated_signal_future_tolerance_seconds,
            ),
        )


__all__ = ["AgentRuntime"]
