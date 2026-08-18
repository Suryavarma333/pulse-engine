from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from common.contracts import WorkerHealth
from common.enums import ProviderStatus
from common.time import Clock

DependencyProbe = Callable[[], Awaitable[bool]]
ReadinessPublisher = Callable[[bool], None]


class DependencySupervisor:
    """Always-running readiness loop for mandatory runtime dependencies."""

    name = "dependency-supervisor"

    def __init__(
        self,
        *,
        probe: DependencyProbe,
        publish: ReadinessPublisher,
        clock: Clock,
        poll_seconds: float,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("dependency supervisor poll_seconds must be positive")
        self._probe = probe
        self._publish = publish
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._health = WorkerHealth(
            name=self.name,
            status=ProviderStatus.DEGRADED,
            checked_at=clock.now(),
            detail="not_started",
        )

    @property
    def health(self) -> WorkerHealth:
        return self._health

    async def run_once(self) -> bool:
        try:
            ready = bool(await self._probe())
        except Exception as exc:
            ready = False
            detail = f"database_probe_failed:{type(exc).__name__}"
        else:
            detail = "database_ready" if ready else "database_unavailable"
        self._health = WorkerHealth(
            name=self.name,
            status=ProviderStatus.HEALTHY if ready else ProviderStatus.UNAVAILABLE,
            checked_at=self._clock.now(),
            detail=detail,
        )
        self._publish(ready)
        return ready

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue


__all__ = ["DependencySupervisor"]
