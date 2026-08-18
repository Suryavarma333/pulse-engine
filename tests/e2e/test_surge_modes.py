from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from agent.app.aws.autoscaling import AutoScalingCapacityAdapter
from agent.app.detection.realtime import RealtimeDetector, RealtimeDetectorConfig
from agent.app.metrics.collector import CollectedSignals
from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlStateMachine
from agent.app.services.predictions import PredictionService
from agent.app.services.ramp_planner import RampPlanner
from agent.app.services.shedding_client import SheddingControlResult
from agent.app.workers.scheduled import ScheduledControlObservation, ScheduledWorker
from common.contracts import RecoveryPlan
from common.enums import ActionStatus, ExecutionMode, SheddingLevel
from db.models import ScheduledEvent
from demo_app.app.config import AppSettings
from demo_app.app.main import create_app
from demo_app.app.shedding.store import InMemoryLoadSheddingStore
from load_tests.lib.models import build_scenario_plan

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
RECOVERY = RecoveryPlan(
    low_threshold_rps=2,
    confirmation_count=2,
    cooldown_seconds=30,
    decrement_step=1,
    capacity_floor=1,
)


class MemoryActions:
    def __init__(self) -> None:
        self.actions: dict[str, SimpleNamespace] = {}

    async def has_unresolved(self, *, target_resource: str) -> bool:
        return False

    async def claim(self, command, **values):
        existing = self.actions.get(command.idempotency_key)
        if existing is not None:
            return SimpleNamespace(action=existing, claimed=False)
        action = SimpleNamespace(
            id=uuid4(),
            idempotency_key=command.idempotency_key,
            requested_desired_capacity=command.requested_desired_capacity,
            applied_desired_capacity=None,
            max_instance_ceiling=values["effective_ceiling"],
            execution_mode=values["execution_mode"].value,
            status=ActionStatus.PLANNED.value,
            provider_request_id=None,
            error_message=None,
            target_resource=command.target_resource,
            cooldown_until=None,
            signal_evidence=command.signal_evidence,
            response_command=command.model_dump(mode="json"),
            requested_shedding_level=(
                None
                if command.requested_shedding_level is None
                else int(command.requested_shedding_level)
            ),
            shedding_status=(
                "not_requested"
                if command.requested_shedding_level is None
                else "pending"
            ),
        )
        self.actions[command.idempotency_key] = action
        return SimpleNamespace(action=action, claimed=True)

    async def update_outcome(self, **values):
        action = self.actions[values["idempotency_key"]]
        action.status = values["status"].value
        action.applied_desired_capacity = values["applied_desired_capacity"]
        action.provider_request_id = values.get("provider_request_id")
        action.error_message = values.get("error_message")
        action.cooldown_until = values.get("cooldown_until")
        return action

    async def record_control_error(self, **values):
        action = self.actions[values["idempotency_key"]]
        action.error_message = values["error_message"]
        return action

    async def mark_shedding_outcome(self, **values):
        action = self.actions[values["idempotency_key"]]
        action.shedding_status = "succeeded" if values["succeeded"] else "pending"
        action.shedding_error = values.get("error_message")
        return action


class DemoSheddingClient:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    async def apply(self, *, command, level):
        response = self.client.post(
            "/internal/load-shedding",
            headers={"X-Pulse-Control-Token": "e2e-control-token"},
            json={
                "level": int(level),
                "reason_code": command.reason_code,
                "reasoning": command.reasoning,
                "changed_by": "agent",
                "correlation_id": str(command.correlation_id),
                "prediction_id": (
                    None if command.prediction_id is None else str(command.prediction_id)
                ),
                "trigger_snapshot_id": command.trigger_snapshot_id,
                "demo_run_id": None if command.demo_run_id is None else str(command.demo_run_id),
                "signal_evidence": command.signal_evidence,
            },
        )
        response.raise_for_status()
        body = response.json()
        return SheddingControlResult(
            changed=body["changed"],
            level=SheddingLevel(body["level"]),
            event_id=body.get("event_id"),
            endpoint_policies=body["endpoint_policies"],
        )


class PredictionWriter:
    def __init__(self) -> None:
        self.predictions = []
        self.points = []

    async def create_with_points(self, prediction, points):
        self.predictions.append(prediction)
        self.points.extend(points)
        return prediction

    async def latest_for_scheduled_event(self, scheduled_event_id):
        return next(
            (
                item
                for item in reversed(self.predictions)
                if item.scheduled_event_id == scheduled_event_id
            ),
            None,
        )

    async def get_with_points(self, prediction_id, *, point_limit=1_000):
        prediction = next(
            (item for item in self.predictions if item.id == prediction_id), None
        )
        points = [item for item in self.points if item.prediction_id == prediction_id]
        return prediction, points[:point_limit]

    async def record_reactive_comparator(
        self, *, environment, demo_run_id, crossed_at
    ):
        prediction = next(
            (
                item
                for item in reversed(self.predictions)
                if item.environment == environment and item.demo_run_id == demo_run_id
            ),
            None,
        )
        if prediction is not None and prediction.reactive_comparator_crossed_at is None:
            prediction.reactive_comparator_crossed_at = crossed_at
        return prediction


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


def signals(second: int, origin_rate: float, **optional: float) -> CollectedSignals:
    observed_at = NOW + timedelta(seconds=second)
    return CollectedSignals(
        observed_at=observed_at,
        origin_observed_at=observed_at,
        origin_request_rate_rps=origin_rate,
        request_count=max(1, int(origin_rate)),
        concurrent_requests=1,
        error_rate=0,
        p50_latency_ms=2,
        p95_latency_ms=4,
        p99_latency_ms=6,
        checkout_p99_latency_ms=4,
        checkout_success_rate=1,
        load_shedding_level=0,
        optional_values=optional,
        provider_health={
            "demo_app": {"status": "healthy"},
            "simulated": {"status": "simulated"},
        },
    )


def pipeline(client: TestClient, actions: MemoryActions) -> ResponsePipeline:
    return ResponsePipeline(
        repository=actions,
        capacity_adapter=AutoScalingCapacityAdapter(
            execution_mode=ExecutionMode.DRY_RUN,
            target_resource="local-simulated-asg",
            region=None,
            global_ceiling=3,
            simulated_capacity=1,
        ),
        shedding_client=DemoSheddingClient(client),
        state_machine=ControlStateMachine(),
        global_ceiling=3,
    )


def assert_checkout_normal(client: TestClient) -> None:
    response = client.post("/checkout", json={"cart_id": "e2e", "item_count": 1})
    assert response.status_code == 200
    assert response.headers["X-Pulse-Endpoint-Mode"] == "normal"
    assert response.headers["X-Pulse-Protection"] == "critical-never-shed"


def test_sudden_spike_predicts_and_protects_before_reactive_comparator() -> None:
    plan = build_scenario_plan("sudden-spike", smoke=True)
    detector = RealtimeDetector(
        RealtimeDetectorConfig(
            environment="e2e",
            baseline_window_seconds=60,
            minimum_samples=3,
            maximum_samples=20,
            acceleration_threshold_rps2=2,
            entry_ratio_threshold=1.5,
            exit_ratio_threshold=1.1,
            confidence_threshold=0.65,
            confirmation_count=1,
            reactive_cpu_threshold_pct=70,
            reactive_load_threshold_rps=50,
            forecast_horizon_seconds=10,
            rps_per_instance=20,
            maximum_capacity=3,
        )
    )
    writer = PredictionWriter()
    predictions = PredictionService(
        writer,
        target_resource="local-simulated-asg",
        maximum_ceiling=3,
        recovery_plan=RECOVERY,
    )

    first, second, trigger_frame = plan.pulse_signals[:3]
    comparator_frame = next(
        frame
        for frame in plan.pulse_signals
        if (frame.cpu_utilization_pct or 0) >= 70
    )
    detector.evaluate(signals(first.at_seconds, 5, edge_request_rate_rps=5, queue_depth=2))
    detector.evaluate(signals(second.at_seconds, 10, edge_request_rate_rps=24, queue_depth=18))
    trigger = detector.evaluate(
        signals(
            trigger_frame.at_seconds,
            25,
            edge_request_rate_rps=46,
            queue_depth=55,
            cpu_utilization_pct=48,
        )
    )
    assert trigger.trigger and trigger.candidate is not None
    assert trigger.reactive_comparator_crossed_at is None
    trigger.snapshot.id = 1
    persisted = asyncio.run(predictions.persist_realtime(trigger.candidate, trigger_snapshot_id=1))

    settings = AppSettings(database_url=None, control_token="e2e-control-token")
    with TestClient(create_app(settings, store=InMemoryLoadSheddingStore())) as client:
        actions = MemoryActions()
        result = asyncio.run(
            pipeline(client, actions).execute(
                persisted.command,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=trigger_frame.at_seconds * timedelta(seconds=1) + NOW,
            )
        )
        assert result.capacity.status in {ActionStatus.DRY_RUN, ActionStatus.CAPPED}
        assert result.capacity.applied is not None and result.capacity.applied <= 3
        assert result.shedding is not None
        assert result.shedding.endpoint_policies["checkout"] == "normal"
        assert_checkout_normal(client)

    comparator = detector.evaluate(
        signals(
            comparator_frame.at_seconds,
            60,
            edge_request_rate_rps=60,
            queue_depth=80,
            cpu_utilization_pct=78,
        )
    )
    assert comparator.reactive_comparator_crossed_at is not None
    assert trigger.candidate.predicted_start_at < comparator.reactive_comparator_crossed_at
    recorded = asyncio.run(
        predictions.record_reactive_comparator(
            environment="e2e",
            demo_run_id=None,
            crossed_at=comparator.reactive_comparator_crossed_at,
        )
    )
    assert recorded is True
    assert (
        writer.predictions[0].reactive_comparator_crossed_at
        == comparator.reactive_comparator_crossed_at
    )


def test_scheduled_diwali_reaches_bounded_peak_once_before_start() -> None:
    starts_at = NOW + timedelta(seconds=10)
    event = ScheduledEvent(
        id=uuid4(),
        name="Diwali sale",
        event_type="commerce_event",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(seconds=20),
        timezone="Asia/Kolkata",
        expected_multiplier=8,
        baseline_rps=5,
        expected_peak_rps=30,
        prewarm_lead_seconds=8,
        peak_lead_seconds=2,
        scale_down_duration_seconds=60,
        minimum_desired_capacity=1,
        peak_desired_capacity=3,
        max_instances_override=3,
        ramp_profile={
            "points": [
                {"offset_seconds": -8, "desired_capacity": 1},
                {"offset_seconds": -4, "desired_capacity": 2},
                {"offset_seconds": -2, "desired_capacity": 3},
            ]
        },
        confidence=0.95,
        source="e2e",
        status="active",
    )
    planner = RampPlanner(global_ceiling=3, derived_steps=3)
    plan = planner.plan(event)
    assert plan.points[-1].due_at < event.starts_at
    assert plan.points[-1].desired_capacity == plan.effective_ceiling == 3

    class Reader:
        async def list_actionable(self, **values):
            return [event]

    async def observe():
        return ScheduledControlObservation(
            current_capacity=1,
            current_shedding_level=SheddingLevel.NORMAL,
            request_rate_rps=1,
        )

    settings = AppSettings(database_url=None, control_token="e2e-control-token")
    with TestClient(create_app(settings, store=InMemoryLoadSheddingStore())) as client:
        actions = MemoryActions()
        predictions = PredictionService(
            PredictionWriter(),
            target_resource="local-simulated-asg",
            maximum_ceiling=3,
            recovery_plan=RECOVERY,
        )
        worker = ScheduledWorker(
            reader=Reader(),
            planner=planner,
            response_pipeline=pipeline(client, actions),
            observation_provider=observe,
            recovery_plan=RECOVERY,
            target_resource="local-simulated-asg",
            clock=Clock(starts_at - timedelta(seconds=1)),
            poll_seconds=1,
            lookahead_seconds=60,
            near_event_protection_seconds=5,
            prediction_service=predictions,
            environment="e2e",
        )
        first = asyncio.run(worker.run_once())
        action_count = len(actions.actions)
        second = asyncio.run(worker.run_once())

        assert first.events[0].due_count == 3
        assert first.events[0].obsolete_skipped == 2
        assert first.events[0].executed_key == plan.points[-1].idempotency_key
        assert second.events[0].executed_key == first.events[0].executed_key
        assert len(actions.actions) == action_count
        assert actions.actions[plan.points[-1].idempotency_key].applied_desired_capacity == 3
        assert_checkout_normal(client)
