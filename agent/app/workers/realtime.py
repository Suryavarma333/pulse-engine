from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent.app.detection.realtime import RealtimeDecision, RealtimeDetector
from agent.app.metrics.collector import CompositeSignalCollector, RequiredSignalUnavailable
from agent.app.services.predictions import PersistedPrediction, PredictionService
from common.contracts import ResponseCommand, WorkerHealth
from common.enums import ProviderStatus
from common.time import Clock

CommandHandler = Callable[[ResponseCommand], Awaitable[None]]


class ActiveRunProvider(Protocol):
    async def current_demo_run_id(self) -> UUID | None: ...


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    decision: RealtimeDecision | None
    prediction: PersistedPrediction | None
    command: ResponseCommand | None
    held: bool
    reason: str


class RealtimeWorker:
    """Runs one bounded collection/evaluation transaction at a time."""

    name = "realtime"

    def __init__(
        self,
        *,
        collector: CompositeSignalCollector,
        detector: RealtimeDetector,
        prediction_service: PredictionService,
        clock: Clock,
        poll_seconds: float,
        command_handler: CommandHandler | None = None,
        active_run_provider: ActiveRunProvider | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._collector = collector
        self._detector = detector
        self._predictions = prediction_service
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._command_handler = command_handler
        self._active_run_provider = active_run_provider
        self._cycle_lock = asyncio.Lock()
        self._health = WorkerHealth(
            name=self.name,
            status=ProviderStatus.DEGRADED,
            checked_at=self._clock.now(),
            detail="not_started",
        )

    @property
    def health(self) -> WorkerHealth:
        return self._health

    async def run_once(self) -> WorkerCycleResult:
        if self._cycle_lock.locked():
            return WorkerCycleResult(
                decision=None,
                prediction=None,
                command=None,
                held=True,
                reason="cycle_already_running",
            )
        async with self._cycle_lock:
            now = self._clock.now()
            demo_run_id = (
                await self._active_run_provider.current_demo_run_id()
                if self._active_run_provider is not None
                else None
            )
            try:
                signals = await self._collector.collect(now=now, demo_run_id=demo_run_id)
            except RequiredSignalUnavailable:
                self._set_health(ProviderStatus.DEGRADED, "required_origin_unavailable")
                return WorkerCycleResult(None, None, None, True, "required_origin_unavailable")
            except Exception as exc:
                self._set_health(
                    ProviderStatus.DEGRADED, f"collection_failed:{type(exc).__name__}"
                )
                return WorkerCycleResult(None, None, None, True, "collection_failed")

            checkpoint = self._detector.checkpoint()
            try:
                decision = self._detector.evaluate(signals)
                snapshot = await self._collector.persist(decision.snapshot)
            except Exception as exc:
                self._detector.restore(checkpoint)
                self._set_health(
                    ProviderStatus.UNAVAILABLE, f"persistence_failed:{type(exc).__name__}"
                )
                return WorkerCycleResult(None, None, None, True, "persistence_failed")

            prediction: PersistedPrediction | None = None
            command: ResponseCommand | None = None
            if decision.trigger and decision.candidate is not None:
                try:
                    prediction = await self._predictions.persist_realtime(
                        decision.candidate, trigger_snapshot_id=snapshot.id
                    )
                    command = prediction.command
                except Exception as exc:
                    self._detector.restore(checkpoint)
                    self._set_health(
                        ProviderStatus.UNAVAILABLE,
                        f"prediction_persistence_failed:{type(exc).__name__}",
                    )
                    return WorkerCycleResult(
                        decision, None, None, True, "prediction_persistence_failed"
                    )
                if self._command_handler is not None:
                    try:
                        await self._command_handler(command)
                    except Exception as exc:
                        self._set_health(
                            ProviderStatus.DEGRADED,
                            f"command_handoff_failed:{type(exc).__name__}",
                        )
                        return WorkerCycleResult(
                            decision, prediction, command, True, "command_handoff_failed"
                        )

            self._set_health(ProviderStatus.HEALTHY, "cycle_complete")
            return WorkerCycleResult(decision, prediction, command, False, decision.reason_code)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    def _set_health(self, status: ProviderStatus, detail: str) -> None:
        self._health = WorkerHealth(
            name=self.name,
            status=status,
            checked_at=self._clock.now(),
            detail=detail,
        )


__all__ = ["ActiveRunProvider", "CommandHandler", "RealtimeWorker", "WorkerCycleResult"]
