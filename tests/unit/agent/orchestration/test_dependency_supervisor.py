from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from agent.app.workers.dependencies import DependencySupervisor
from common.enums import ProviderStatus

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class AdvancingClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class SequenceProbe:
    def __init__(self, values: list[bool | Exception]) -> None:
        self.values = iter(values)

    async def __call__(self) -> bool:
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def supervisor(values: list[bool | Exception], published: list[bool]):
    return DependencySupervisor(
        probe=SequenceProbe(values),
        publish=published.append,
        clock=AdvancingClock(),
        poll_seconds=1,
    )


def test_initial_database_failure_recovers_without_process_replacement() -> None:
    published: list[bool] = []
    worker = supervisor([False, True], published)

    first = asyncio.run(worker.run_once())
    first_health = worker.health
    second = asyncio.run(worker.run_once())

    assert first is False
    assert first_health.status is ProviderStatus.UNAVAILABLE
    assert first_health.detail == "database_unavailable"
    assert second is True
    assert worker.health.status is ProviderStatus.HEALTHY
    assert worker.health.detail == "database_ready"
    assert published == [False, True]


def test_runtime_database_outage_and_recovery_update_readiness_each_cycle() -> None:
    published: list[bool] = []
    worker = supervisor([True, RuntimeError("lost connection"), True], published)

    states = [asyncio.run(worker.run_once()) for _ in range(3)]

    assert states == [True, False, True]
    assert published == [True, False, True]
    assert worker.health.status is ProviderStatus.HEALTHY
