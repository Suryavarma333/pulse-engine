from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent.app.services.predictions import PredictionService
from agent.app.services.ramp_planner import RampPlanner
from common.contracts import RecoveryPlan
from common.enums import PredictionMode
from db.models import ScheduledEvent

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class Writer:
    def __init__(self) -> None:
        self.prediction = None
        self.points = []
        self.creates = 0

    async def latest_for_scheduled_event(self, scheduled_event_id):
        if self.prediction and self.prediction.scheduled_event_id == scheduled_event_id:
            return self.prediction
        return None

    async def get_with_points(self, prediction_id, *, point_limit=1000):
        return self.prediction, self.points

    async def create_with_points(self, prediction, points):
        self.creates += 1
        self.prediction = prediction
        self.points = list(points)
        return prediction


def test_scheduled_prediction_and_points_are_persisted_once_and_reused() -> None:
    event = ScheduledEvent(
        id=uuid4(),
        name="Diwali",
        event_type="sale",
        starts_at=NOW + timedelta(minutes=10),
        ends_at=NOW + timedelta(hours=1),
        timezone="Asia/Kolkata",
        expected_multiplier=4,
        baseline_rps=10,
        expected_peak_rps=40,
        prewarm_lead_seconds=600,
        peak_lead_seconds=60,
        scale_down_duration_seconds=600,
        minimum_desired_capacity=1,
        peak_desired_capacity=3,
        max_instances_override=3,
        ramp_profile={},
        confidence=0.9,
        source="test",
        status="active",
    )
    plan = RampPlanner(global_ceiling=3).plan(event)
    writer = Writer()
    service = PredictionService(
        writer,
        target_resource="pulse-asg",
        maximum_ceiling=3,
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=2,
            cooldown_seconds=30,
            decrement_step=1,
            capacity_floor=1,
        ),
    )

    first = asyncio.run(
        service.ensure_scheduled(
            event,
            plan=plan,
            demo_run_id=None,
            environment="local",
            created_at=NOW,
        )
    )
    second = asyncio.run(
        service.ensure_scheduled(
            event,
            plan=plan,
            demo_run_id=None,
            environment="local",
            created_at=NOW + timedelta(seconds=5),
        )
    )

    assert writer.creates == 1
    assert first.prediction.id == second.prediction.id
    assert first.prediction.mode == PredictionMode.SCHEDULED.value
    assert first.prediction.scheduled_event_id == event.id
    assert first.prediction.predicted_peak_rps == 40
    assert first.points[-1].predicted_capacity == 3
    assert first.command.prediction_id == first.prediction.id
    assert first.command.scheduled_event_id == event.id
    assert first.command.maximum_ceiling == 3
