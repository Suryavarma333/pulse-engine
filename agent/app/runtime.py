from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.app.config import AgentSettings
from agent.app.metrics.providers.simulated import SimulatedSignalBuffer
from common.time import Clock, SystemClock


@dataclass(slots=True)
class AgentRuntime:
    """Single owner for resources shared by the API and worker assembly."""

    settings: AgentSettings
    clock: Clock
    simulated_signals: SimulatedSignalBuffer
    managed_clients: list[Any]

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
            managed_clients=[],
        )

    def manage(self, client: Any) -> Any:
        """Register an async client that must be closed with the runtime."""

        self.managed_clients.append(client)
        return client

    async def close(self) -> None:
        for client in reversed(self.managed_clients):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        self.managed_clients.clear()


__all__ = ["AgentRuntime"]
