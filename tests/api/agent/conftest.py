from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent.app.main import AgentComponents, create_app
from agent.app.orchestration.state_machine import ControlStateMachine
from agent.app.services.feedback import FeedbackEvaluator
from common.contracts import CapacityState, DemoRunSpec, WorkerHealth
from common.enums import ProviderStatus
from db.models import (
    DemoRun,
    LoadSheddingEvent,
    ScalingAction,
    ScheduledEvent,
    SurgePrediction,
    SurgePredictionPoint,
    TrafficSnapshot,
)
from db.repositories.demo_runs import IdempotencyConflict
from db.repositories.operator import CursorPage, InvalidCursor, PredictionOverlay

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class Clock:
    def now(self):
        return NOW + timedelta(minutes=10)


class Capacity:
    async def read_capacity(self, *, observed_at):
        return SimpleNamespace(
            state=CapacityState(
                desired=2,
                in_service=1,
                pending=1,
                observed_at=observed_at,
                provider_status=ProviderStatus.SIMULATED,
            )
        )


class Worker:
    name = "fake"
    health = WorkerHealth(
        name="fake", status=ProviderStatus.HEALTHY, checked_at=NOW, detail="ok"
    )


def snapshot() -> TrafficSnapshot:
    return TrafficSnapshot(
        id=1,
        environment="local",
        observed_at=NOW,
        window_seconds=60,
        origin_request_rate_rps=25,
        baseline_request_rate_rps=10,
        request_count=100,
        concurrent_requests=5,
        request_rate_change_rps=15,
        request_acceleration_rps2=2,
        checkout_p99_latency_ms=120,
        checkout_success_rate=0.99,
        error_rate=0.01,
        asg_desired_capacity=2,
        asg_in_service_capacity=1,
        load_shedding_level=1,
        reactive_comparator_crossed=False,
        signal_details={"provider_health": {"demo_app": {"status": "healthy"}}},
    )


def prediction() -> SurgePrediction:
    return SurgePrediction(
        id=uuid4(),
        mode="realtime",
        environment="local",
        correlation_id=uuid4(),
        model_name="rolling",
        model_version="v1",
        created_at=NOW,
        basis_window_start=NOW - timedelta(minutes=1),
        basis_window_end=NOW,
        predicted_start_at=NOW,
        predicted_peak_at=NOW + timedelta(minutes=1),
        baseline_rps=10,
        predicted_peak_rps=50,
        predicted_multiplier=5,
        recommended_capacity=2,
        confidence=0.9,
        trigger_type="acceleration",
        reasoning="accelerating",
        signal_evidence={},
        status="active",
        formula_version="v1",
    )


def action() -> ScalingAction:
    return ScalingAction(
        id=uuid4(),
        correlation_id=uuid4(),
        idempotency_key="action-1",
        requested_at=NOW,
        action_type="set_desired_capacity",
        execution_mode="dry_run",
        target_resource="pulse-asg",
        previous_desired_capacity=1,
        requested_desired_capacity=2,
        applied_desired_capacity=2,
        max_instance_ceiling=3,
        status="dry_run",
        reason_code="test",
        reasoning="test action",
        signal_evidence={},
    )


def shedding() -> LoadSheddingEvent:
    return LoadSheddingEvent(
        id=uuid4(),
        correlation_id=uuid4(),
        environment="local",
        started_at=NOW,
        from_level=0,
        to_level=1,
        changed_by="agent",
        reason_code="test",
        reasoning="test protection",
        signal_evidence={},
        policy_snapshot={"checkout": "normal"},
        application_status="applied",
    )


def event() -> ScheduledEvent:
    return ScheduledEvent(
        id=uuid4(),
        name="Diwali",
        event_type="sale",
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=2),
        timezone="Asia/Kolkata",
        expected_multiplier=4,
        prewarm_lead_seconds=1800,
        peak_lead_seconds=300,
        scale_down_duration_seconds=1800,
        minimum_desired_capacity=1,
        peak_desired_capacity=3,
        max_instances_override=3,
        ramp_profile={},
        confidence=0.9,
        source="test",
        status="active",
    )


def demo_run(**overrides) -> DemoRun:
    values = {
        "id": uuid4(),
        "idempotency_key": "run-existing",
        "scenario_name": "sudden",
        "mode": "realtime",
        "environment": "local",
        "baseline_type": "pulse",
        "execution_mode": "dry_run",
        "configuration": {"users": 10},
        "thresholds": {"ratio": 1.5},
        "started_at": NOW,
        "ended_at": NOW + timedelta(minutes=5),
        "status": "completed",
        "formula_version": "v1",
        "locust_summary": {},
        "result_summary": {"references": {"snapshot_ids": [1]}},
        "warnings": {"items": []},
    }
    values.update(overrides)
    return DemoRun(**values)


class OperatorStore:
    def __init__(self) -> None:
        self.snapshot = snapshot()
        self.prediction = prediction()
        self.action = action()
        self.shedding = shedding()
        self.event = event()
        self.run = demo_run()
        self.invalid_cursor = False

    def _page(self, item, cursor=None):
        if cursor == "bad" or self.invalid_cursor:
            raise InvalidCursor("cursor is malformed")
        return CursorPage((item,), "next-page")

    async def status_rows(self, *, environment):
        return {
            "snapshot": self.snapshot,
            "prediction": self.prediction,
            "action": self.action,
            "shedding": self.shedding,
            "scheduled_event": self.event,
        }

    async def snapshots(self, **kwargs):
        return self._page(self.snapshot, kwargs.get("cursor"))

    async def predictions(self, **kwargs):
        return self._page(self.prediction, kwargs.get("cursor"))

    async def scaling_actions(self, **kwargs):
        return self._page(self.action, kwargs.get("cursor"))

    async def shedding_events(self, **kwargs):
        return self._page(self.shedding, kwargs.get("cursor"))

    async def scheduled_events(self, **kwargs):
        return self._page(self.event, kwargs.get("cursor"))

    async def result_runs(self, **kwargs):
        return self._page(self.run, kwargs.get("cursor"))

    async def run_detail(self, run_id):
        return self.run if run_id == self.run.id else None

    async def prediction_overlay(self, prediction_id, *, actual_window_seconds):
        if prediction_id != self.prediction.id:
            return None
        point = SurgePredictionPoint(
            prediction_id=self.prediction.id,
            point_at=NOW,
            predicted_rps=25,
            predicted_capacity=2,
        )
        return PredictionOverlay(
            self.prediction,
            (point,),
            (self.action,),
            (self.shedding,),
            (self.snapshot,),
        )


class RunStore:
    def __init__(self) -> None:
        self.by_id = {}
        self.by_key = {}

    async def start(self, spec: DemoRunSpec):
        existing = self.by_key.get(spec.idempotency_key)
        if existing is not None:
            if existing.scenario_name != spec.scenario_name:
                raise IdempotencyConflict("different run payload")
            return existing, False
        item = demo_run(
            id=uuid4(),
            idempotency_key=spec.idempotency_key,
            scenario_name=spec.scenario_name,
            mode=spec.mode.value,
            environment=spec.environment,
            baseline_type=spec.baseline_type,
            execution_mode=spec.execution_mode.value,
            configuration=spec.configuration,
            thresholds=spec.thresholds,
            started_at=spec.started_at,
            ended_at=None,
            status="running",
            locust_summary={},
            result_summary={},
        )
        self.by_id[item.id] = item
        self.by_key[spec.idempotency_key] = item
        return item, True

    async def get(self, run_id):
        return self.by_id.get(run_id)

    async def complete(self, **values):
        item = self.by_id[values["run_id"]]
        if item.status not in {"running", "pending_evaluation"}:
            raise IdempotencyConflict("already closed")
        item.status = values["status"].value
        item.ended_at = values["ended_at"]
        item.locust_summary = values["locust_summary"]
        item.notes = values.get("notes")
        item.result_summary = values.get("result_summary", {})
        return item


class FeedbackService:
    def __init__(self, runs: RunStore) -> None:
        self.runs = runs

    async def evaluate_run(self, run_id):
        item = self.runs.by_id[run_id]
        item.status = "completed"
        item.formula_version = FeedbackEvaluator.formula_version
        item.result_summary = {"metrics": {"checkout_success_rate": 1.0}}
        return item


@pytest.fixture
def app_fixture():
    from agent.app.config import AgentSettings

    settings = AgentSettings(
        control_token="test-control-token",
        query_max_rows=10,
        query_default_window_seconds=3600,
        query_max_window_seconds=86400,
        feedback_horizon_seconds=0,
    )
    operator = OperatorStore()
    runs = RunStore()
    components = AgentComponents(
        settings=settings,
        clock=Clock(),
        simulated_signal_buffer=SimpleNamespace(),
        operator_store=operator,
        demo_runs=runs,
        feedback_service=FeedbackService(runs),
        capacity_adapter=Capacity(),
        state_machine=ControlStateMachine(),
        recovery_coordinator=SimpleNamespace(low_confirmations=0),
        workers=[Worker()],
        db_ready=True,
    )
    app = create_app(components=components, start_workers=False)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            operator=operator,
            runs=runs,
            components=components,
        )


__all__ = ["NOW", "demo_run"]
