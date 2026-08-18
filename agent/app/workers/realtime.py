from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent.app.detection.realtime import RealtimeDecision, RealtimeDetector
from agent.app.metrics.collector import CompositeSignalCollector, RequiredSignalUnavailable
from agent.app.services.predictions import PersistedPrediction, PredictionService
from common.contracts import ResponseCommand, WorkerHealth
from common.enums import ProviderStatus
from common.time import Clock

CommandHandler = Callable[[ResponseCommand], Awaitable[None]]
DecisionHandler = Callable[[RealtimeDecision, ResponseCommand | None], Awaitable[bool]]


class ActiveRunProvider(Protocol):
    async def current_demo_run_id(self, *, environment: str) -> UUID | None: ...

    async def record_reactive_comparator(
        self, *, run_id: UUID, crossed_at: datetime
    ) -> object | None: ...


class CommandRetryScheduler(Protocol):
    async def schedule_dispatch(
        self,
        command: ResponseCommand,
        *,
        now: datetime,
        error_message: str,
    ) -> object: ...


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
        decision_handler: DecisionHandler | None = None,
        active_run_provider: ActiveRunProvider | None = None,
        retry_scheduler: CommandRetryScheduler | None = None,
        environment: str = "local",
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._collector = collector
        self._detector = detector
        self._predictions = prediction_service
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._command_handler = command_handler
        self._decision_handler = decision_handler
        self._active_run_provider = active_run_provider
        self._retry_scheduler = retry_scheduler
        self._environment = environment
        self._active_run_id: UUID | None = None
        self._active_command: ResponseCommand | None = None
        self._recorded_comparator_at: datetime | None = None
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
                await self._active_run_provider.current_demo_run_id(
                    environment=self._environment
                )
                if self._active_run_provider is not None
                else None
            )
            if demo_run_id != self._active_run_id:
                self._detector.reset()
                self._recorded_comparator_at = None
                self._active_command = None
                self._active_run_id = demo_run_id
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
                    self._active_command = command
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
                        if self._retry_scheduler is not None:
                            try:
                                await self._retry_scheduler.schedule_dispatch(
                                    command,
                                    now=now,
                                    error_message=(
                                        f"command_handoff_failed:{type(exc).__name__}"
                                    ),
                                )
                            except Exception as retry_exc:
                                self._set_health(
                                    ProviderStatus.UNAVAILABLE,
                                    "command_retry_persistence_failed:"
                                    f"{type(retry_exc).__name__}",
                                )
                                return WorkerCycleResult(
                                    decision,
                                    prediction,
                                    command,
                                    True,
                                    "command_retry_persistence_failed",
                                )
                        self._set_health(
                            ProviderStatus.DEGRADED,
                            f"command_handoff_failed:{type(exc).__name__}",
                        )
                        return WorkerCycleResult(
                            decision, prediction, command, True, "command_handoff_failed"
                        )

            if self._decision_handler is not None:
                try:
                    fully_recovered = await self._decision_handler(
                        decision, self._active_command
                    )
                    if fully_recovered:
                        self._active_command = None
                except Exception as exc:
                    self._set_health(
                        ProviderStatus.DEGRADED,
                        f"control_lifecycle_failed:{type(exc).__name__}",
                    )
                    return WorkerCycleResult(
                        decision, prediction, command, True, "control_lifecycle_failed"
                    )

            comparator_error = await self._record_comparator(
                decision,
                prediction=prediction,
                demo_run_id=demo_run_id,
            )
            if comparator_error is not None:
                self._set_health(ProviderStatus.DEGRADED, comparator_error)
                return WorkerCycleResult(
                    decision, prediction, command, False, "comparator_persistence_degraded"
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

    async def _record_comparator(
        self,
        decision: RealtimeDecision,
        *,
        prediction: PersistedPrediction | None,
        demo_run_id: UUID | None,
    ) -> str | None:
        crossed_at = decision.reactive_comparator_crossed_at
        if crossed_at is None or crossed_at == self._recorded_comparator_at:
            return None
        try:
            prediction_recorded = (
                prediction is not None
                and prediction.prediction.reactive_comparator_crossed_at == crossed_at
            ) or await self._predictions.record_reactive_comparator(
                environment=decision.snapshot.environment,
                demo_run_id=demo_run_id,
                crossed_at=crossed_at,
            )
            run_recorded = demo_run_id is None
            if demo_run_id is not None and self._active_run_provider is not None:
                run_recorded = (
                    await self._active_run_provider.record_reactive_comparator(
                        run_id=demo_run_id,
                        crossed_at=crossed_at,
                    )
                    is not None
                )
            if prediction_recorded and run_recorded:
                self._recorded_comparator_at = crossed_at
            return None
        except Exception as exc:
            return f"comparator_persistence_failed:{type(exc).__name__}"


__all__ = [
    "ActiveRunProvider",
    "CommandHandler",
    "CommandRetryScheduler",
    "DecisionHandler",
    "RealtimeWorker",
    "WorkerCycleResult",
]
