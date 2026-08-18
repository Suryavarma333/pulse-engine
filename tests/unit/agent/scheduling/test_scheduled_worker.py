from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from agent.app.services.ramp_planner import RampPlanner
from agent.app.workers.scheduled import ScheduledControlObservation, ScheduledWorker
from common.contracts import RecoveryPlan
from common.enums import SheddingLevel
from db.models import ScheduledEvent

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now=NOW) -> None:
        self.value = now

    def now(self):
        return self.value


class Reader:
    def __init__(self, events, gate=None) -> None:
        self.events = events
        self.gate = gate
        self.calls = 0

    async def list_actionable(self, **kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.events


class Pipeline:
    def __init__(self) -> None:
        self.executed = []
        self.held = []

    async def execute(self, command, **kwargs):
        self.executed.append((command, kwargs))
        return object()

    async def audit_hold(self, command, **kwargs):
        self.held.append((command, kwargs))
        return object()


def event(**overrides) -> ScheduledEvent:
    values = {
        "id": uuid4(),
        "name": "Diwali sale",
        "event_type": "sale",
        "starts_at": NOW + timedelta(seconds=120),
        "ends_at": NOW + timedelta(hours=1),
        "timezone": "Asia/Kolkata",
        "expected_multiplier": 4,
        "prewarm_lead_seconds": 600,
        "peak_lead_seconds": 60,
        "scale_down_duration_seconds": 600,
        "minimum_desired_capacity": 1,
        "peak_desired_capacity": 4,
        "max_instances_override": 3,
        "ramp_profile": {},
        "confidence": 0.9,
        "source": "test",
        "status": "active",
    }
    values.update(overrides)
    return ScheduledEvent(**values)


def worker(
    reader,
    pipeline,
    *,
    recovery_handler=None,
    clock=None,
    prediction_service=None,
) -> ScheduledWorker:
    async def observe():
        return ScheduledControlObservation(
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            request_rate_rps=1,
        )

    return ScheduledWorker(
        reader=reader,
        planner=RampPlanner(global_ceiling=3, derived_steps=4),
        response_pipeline=pipeline,
        observation_provider=observe,
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=2,
            cooldown_seconds=30,
            decrement_step=1,
            capacity_floor=1,
        ),
        target_resource="pulse-asg",
        clock=clock or Clock(),
        poll_seconds=1,
        lookahead_seconds=3600,
        near_event_protection_seconds=300,
        recovery_handler=recovery_handler,
        prediction_service=prediction_service,
    )


def test_downtime_catchup_audits_obsolete_points_and_executes_only_latest_due() -> None:
    scheduled = event()
    pipeline = Pipeline()
    prediction_id = uuid4()

    class Predictions:
        calls = 0

        async def ensure_scheduled(self, event, **values):
            self.calls += 1
            return SimpleNamespace(prediction=SimpleNamespace(id=prediction_id))

    predictions = Predictions()
    service = worker(
        Reader([scheduled]), pipeline, prediction_service=predictions
    )

    result = asyncio.run(service.run_once())
    cycle = result.events[0]

    assert not result.held
    assert cycle.due_count == 3
    assert cycle.obsolete_skipped == 2
    assert [item[1]["reason"] for item in pipeline.held] == [
        "scheduled_obsolete_point_skipped",
        "scheduled_obsolete_point_skipped",
    ]
    assert len(pipeline.executed) == 2  # latest capacity plus separate near-event tier protection
    latest, protection = [item[0] for item in pipeline.executed]
    assert latest.idempotency_key == cycle.executed_key
    assert protection.requested_shedding_level is SheddingLevel.DISABLE_RECOMMENDATIONS
    assert latest.maximum_ceiling == protection.maximum_ceiling == 3
    assert latest.prediction_id == protection.prediction_id == prediction_id
    assert latest.signal_evidence["ramp_requested_capacity"] >= latest.requested_desired_capacity

    asyncio.run(service.run_once())
    assert predictions.calls == 2
    assert [item[0].idempotency_key for item in pipeline.executed[:2]] == [
        item[0].idempotency_key for item in pipeline.executed[2:]
    ]


def test_future_event_reports_next_point_without_response() -> None:
    scheduled = event(starts_at=NOW + timedelta(hours=2), ends_at=NOW + timedelta(hours=3))
    pipeline = Pipeline()
    result = asyncio.run(worker(Reader([scheduled]), pipeline).run_once())
    assert result.events[0].due_count == 0
    assert result.events[0].next_due_at is not None
    assert pipeline.executed == []
    assert pipeline.held == []


def test_cancelled_or_finished_event_delegates_shared_recovery_without_capacity_drop() -> None:
    calls = []

    async def recover(event, observation, now):
        calls.append((event.id, observation.current_capacity, now))

    for scheduled in (
        event(status="cancelled"),
        event(starts_at=NOW - timedelta(hours=2), ends_at=NOW - timedelta(minutes=1)),
    ):
        pipeline = Pipeline()
        result = asyncio.run(
            worker(Reader([scheduled]), pipeline, recovery_handler=recover).run_once()
        )
        assert result.events[0].recovery_requested
        assert pipeline.executed == []
        assert pipeline.held[0][1]["reason"] == "scheduled_recovery_delegated"
    assert len(calls) == 2


def test_overlapping_cycle_is_held_and_worker_health_recovers() -> None:
    gate = asyncio.Event()
    reader = Reader([], gate=gate)
    service = worker(reader, Pipeline())

    async def scenario():
        first = asyncio.create_task(service.run_once())
        await asyncio.sleep(0)
        second = await service.run_once()
        gate.set()
        completed = await first
        return second, completed

    held, completed = asyncio.run(scenario())
    assert held.held and held.reason == "cycle_already_running"
    assert not completed.held
    assert service.health.status.value == "healthy"


def test_invalid_event_fails_safe_without_exposing_exception_detail() -> None:
    scheduled = event(timezone="Invalid/Timezone")
    service = worker(Reader([scheduled]), Pipeline())
    result = asyncio.run(service.run_once())
    assert result.held and result.reason == "scheduled_cycle_failed"
    assert service.health.status.value == "unavailable"
    assert "ValueError" in service.health.detail
