from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from agent.app.services.ramp_planner import RampPlan, RampPlanner, RampPoint
from common.contracts import RecoveryPlan, ResponseCommand, WorkerHealth
from common.enums import PredictionMode, ProviderStatus, ResponseIntent, SheddingLevel
from common.time import Clock

_SCHEDULE_NAMESPACE = UUID("a4404649-83db-4fa5-8b95-b0efb713bb5d")


class ScheduledEventReader(Protocol):
    async def list_actionable(
        self,
        *,
        now: datetime,
        lookahead_seconds: int,
        recovery_lookbehind_seconds: int = 86_400,
        statuses: tuple[str, ...] = ("active", "cancelled"),
        limit: int = 200,
    ) -> list[Any]: ...


class ScheduledResponsePipeline(Protocol):
    async def execute(
        self,
        command: ResponseCommand,
        *,
        current_capacity: int,
        current_shedding_level: SheddingLevel,
        requested_at: datetime,
    ) -> Any: ...

    async def audit_hold(
        self,
        command: ResponseCommand,
        *,
        current_capacity: int,
        requested_at: datetime,
        reason: str,
    ) -> Any: ...


class ScheduledPredictionService(Protocol):
    async def ensure_scheduled(self, event: Any, **values: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ScheduledControlObservation:
    current_capacity: int
    current_shedding_level: SheddingLevel
    demo_run_id: UUID | None = None
    request_rate_rps: float | None = None
    observed_at: datetime | None = None
    snapshot_id: int | None = None


ObservationProvider = Callable[[], Awaitable[ScheduledControlObservation]]
RecoveryHandler = Callable[[Any, ScheduledControlObservation, datetime], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ScheduledEventCycle:
    event_id: UUID
    status: str
    due_count: int
    obsolete_skipped: int
    executed_key: str | None
    next_due_at: datetime | None
    recovery_requested: bool


@dataclass(frozen=True, slots=True)
class ScheduledCycleResult:
    events: tuple[ScheduledEventCycle, ...]
    held: bool
    reason: str


class ScheduledWorker:
    """Polls scheduled events, catches up to only the latest safe point, and reuses response."""

    name = "scheduled"

    def __init__(
        self,
        *,
        reader: ScheduledEventReader,
        planner: RampPlanner,
        response_pipeline: ScheduledResponsePipeline,
        observation_provider: ObservationProvider,
        recovery_plan: RecoveryPlan,
        target_resource: str,
        clock: Clock,
        poll_seconds: float,
        lookahead_seconds: int,
        near_event_protection_seconds: int,
        recovery_handler: RecoveryHandler | None = None,
        prediction_service: ScheduledPredictionService | None = None,
        environment: str = "local",
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if lookahead_seconds < 60:
            raise ValueError("lookahead_seconds must be at least 60")
        if near_event_protection_seconds < 0:
            raise ValueError("near_event_protection_seconds must be nonnegative")
        if not target_resource:
            raise ValueError("target_resource is required")
        self._reader = reader
        self._planner = planner
        self._pipeline = response_pipeline
        self._observe = observation_provider
        self._recovery_plan = recovery_plan
        self._target = target_resource
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._lookahead_seconds = lookahead_seconds
        self._near_event_seconds = near_event_protection_seconds
        self._recovery_handler = recovery_handler
        self._predictions = prediction_service
        self._environment = environment
        self._lock = asyncio.Lock()
        self._health = WorkerHealth(
            name=self.name,
            status=ProviderStatus.DEGRADED,
            checked_at=clock.now(),
            detail="not_started",
        )
        self._next_due_at: datetime | None = None

    @property
    def health(self) -> WorkerHealth:
        return self._health

    @property
    def next_due_at(self) -> datetime | None:
        return self._next_due_at

    async def run_once(self) -> ScheduledCycleResult:
        if self._lock.locked():
            return ScheduledCycleResult((), True, "cycle_already_running")
        async with self._lock:
            now = self._clock.now()
            try:
                events = await self._reader.list_actionable(
                    now=now,
                    lookahead_seconds=self._lookahead_seconds,
                )
                cycles = []
                for event in events:
                    # Each event receives a fresh authoritative capacity/tier observation;
                    # distinct overlapping commands are serialized by the response arbiter.
                    observation = await self._observe()
                    cycles.append(
                        await self._process_event(
                            event,
                            observation=observation,
                            now=now,
                        )
                    )
            except Exception as exc:
                self._set_health(
                    ProviderStatus.UNAVAILABLE, f"scheduled_cycle_failed:{type(exc).__name__}"
                )
                return ScheduledCycleResult((), True, "scheduled_cycle_failed")
            next_due = [cycle.next_due_at for cycle in cycles if cycle.next_due_at]
            self._next_due_at = min(next_due) if next_due else None
            self._set_health(ProviderStatus.HEALTHY, f"events_evaluated:{len(cycles)}")
            return ScheduledCycleResult(tuple(cycles), False, "cycle_complete")

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    async def _process_event(
        self,
        event: Any,
        *,
        observation: ScheduledControlObservation,
        now: datetime,
    ) -> ScheduledEventCycle:
        plan = self._planner.plan(event)
        status = str(event.status)
        recovery_requested = status == "cancelled" or now >= event.ends_at
        if recovery_requested:
            await self._audit_recovery_handoff(event, plan, observation, now)
            if self._recovery_handler is not None:
                await self._recovery_handler(event, observation, now)
            return ScheduledEventCycle(
                event_id=event.id,
                status=status,
                due_count=0,
                obsolete_skipped=0,
                executed_key=None,
                next_due_at=None,
                recovery_requested=True,
            )
        if status != "active":
            return ScheduledEventCycle(event.id, status, 0, 0, None, None, False)

        due = plan.due(now)
        next_point = plan.next_after(now)
        if not due:
            return ScheduledEventCycle(
                event.id,
                status,
                0,
                0,
                None,
                None if next_point is None else next_point.due_at,
                False,
            )

        prediction_id = None
        if self._predictions is not None:
            persisted = await self._predictions.ensure_scheduled(
                event,
                plan=plan,
                demo_run_id=observation.demo_run_id,
                environment=self._environment,
                created_at=now,
            )
            prediction_id = persisted.prediction.id
        for obsolete in due[:-1]:
            command = self._command(
                event,
                plan,
                obsolete,
                observation,
                level=None,
                prediction_id=prediction_id,
            )
            await self._pipeline.audit_hold(
                command,
                current_capacity=observation.current_capacity,
                requested_at=now,
                reason="scheduled_obsolete_point_skipped",
            )
        latest = due[-1]
        command = self._command(
            event,
            plan,
            latest,
            observation,
            level=None,
            prediction_id=prediction_id,
        )
        await self._pipeline.execute(
            command,
            current_capacity=observation.current_capacity,
            current_shedding_level=observation.current_shedding_level,
            requested_at=now,
        )
        await self._apply_near_event_protection(
            event, plan, latest, observation, now, prediction_id=prediction_id
        )
        return ScheduledEventCycle(
            event.id,
            status,
            len(due),
            max(0, len(due) - 1),
            latest.idempotency_key,
            None if next_point is None else next_point.due_at,
            False,
        )

    async def _apply_near_event_protection(
        self,
        event: Any,
        plan: RampPlan,
        latest: RampPoint,
        observation: ScheduledControlObservation,
        now: datetime,
        *,
        prediction_id: UUID | None,
    ) -> None:
        seconds_until_start = (event.starts_at - now).total_seconds()
        if not 0 <= seconds_until_start <= self._near_event_seconds:
            return
        protection = RampPoint(
            due_at=now,
            offset_seconds=latest.offset_seconds,
            desired_capacity=max(observation.current_capacity, latest.desired_capacity),
            requested_capacity=max(observation.current_capacity, latest.requested_capacity),
            idempotency_key=(
                f"scheduled:{event.id}:protect:"
                f"{max(observation.current_capacity, latest.desired_capacity)}"
            ),
        )
        command = self._command(
            event,
            plan,
            protection,
            observation,
            level=SheddingLevel.DISABLE_RECOMMENDATIONS,
            prediction_id=prediction_id,
        )
        await self._pipeline.execute(
            command,
            current_capacity=observation.current_capacity,
            current_shedding_level=observation.current_shedding_level,
            requested_at=now,
        )

    async def _audit_recovery_handoff(
        self,
        event: Any,
        plan: RampPlan,
        observation: ScheduledControlObservation,
        now: datetime,
    ) -> None:
        point = RampPoint(
            due_at=now,
            offset_seconds=0,
            desired_capacity=observation.current_capacity,
            requested_capacity=observation.current_capacity,
            idempotency_key=(
                f"scheduled:{event.id}:recovery:{observation.current_capacity}"
            ),
        )
        command = self._command(event, plan, point, observation, level=None)
        await self._pipeline.audit_hold(
            command,
            current_capacity=observation.current_capacity,
            requested_at=now,
            reason="scheduled_recovery_delegated",
        )

    def _command(
        self,
        event: Any,
        plan: RampPlan,
        point: RampPoint,
        observation: ScheduledControlObservation,
        *,
        level: SheddingLevel | None,
        prediction_id: UUID | None = None,
    ) -> ResponseCommand:
        correlation = uuid5(_SCHEDULE_NAMESPACE, str(event.id))
        return ResponseCommand(
            command_id=uuid5(_SCHEDULE_NAMESPACE, point.idempotency_key),
            idempotency_key=point.idempotency_key,
            correlation_id=correlation,
            demo_run_id=observation.demo_run_id,
            prediction_id=prediction_id,
            scheduled_event_id=event.id,
            mode=PredictionMode.SCHEDULED,
            intent=ResponseIntent.PREWARM,
            target_resource=self._target,
            requested_desired_capacity=point.desired_capacity,
            maximum_ceiling=plan.effective_ceiling,
            requested_shedding_level=level,
            reason_code="scheduled_prewarm_ramp",
            reasoning=(
                f"Execute scheduled event {event.name} ramp at offset "
                f"{point.offset_seconds} seconds"
            ),
            signal_evidence={
                "scheduled_event_id": str(event.id),
                "event_status": str(event.status),
                "event_timezone": event.timezone,
                "event_starts_at": event.starts_at.isoformat(),
                "ramp_offset_seconds": point.offset_seconds,
                "ramp_requested_capacity": point.requested_capacity,
                "ramp_applied_target": point.desired_capacity,
                "event_effective_ceiling": plan.effective_ceiling,
            },
            recovery_plan=self._recovery_plan,
        )

    def _set_health(self, status: ProviderStatus, detail: str) -> None:
        self._health = WorkerHealth(
            name=self.name,
            status=status,
            checked_at=self._clock.now(),
            detail=detail,
        )


__all__ = [
    "ObservationProvider",
    "RecoveryHandler",
    "ScheduledControlObservation",
    "ScheduledCycleResult",
    "ScheduledEventCycle",
    "ScheduledWorker",
]
