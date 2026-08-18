from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from agent.app.aws.autoscaling import AutoScalingCapacityAdapter
from agent.app.detection.realtime import RealtimeDetector, RealtimeDetectorConfig
from agent.app.metrics.collector import CollectedSignals
from agent.app.orchestration.realtime_lifecycle import (
    RealtimeControlLifecycle,
    RealtimeControlObservation,
)
from agent.app.orchestration.recovery import RecoveryCoordinator, RecoveryWorker
from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlStateMachine
from agent.app.services.predictions import PredictionService
from agent.app.services.shedding_client import SheddingControlResult
from agent.app.workers.realtime import RealtimeWorker
from common.contracts import RecoveryPlan
from common.enums import (
    ActionStatus,
    ControlState,
    ExecutionMode,
    SheddingLevel,
)

NOW = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.current = NOW

    def now(self):
        return self.current


def signals(second: int, rate: float, run_id) -> CollectedSignals:
    at = NOW + timedelta(seconds=second)
    return CollectedSignals(
        observed_at=at,
        origin_observed_at=at,
        origin_request_rate_rps=rate,
        request_count=int(rate),
        concurrent_requests=1,
        error_rate=0,
        p50_latency_ms=1,
        p95_latency_ms=2,
        p99_latency_ms=3,
        checkout_p99_latency_ms=2,
        checkout_success_rate=1,
        load_shedding_level=0,
        optional_values={"edge_request_rate_rps": rate + 10},
        provider_health={
            "demo_app": {"status": "healthy"},
            "simulated": {"status": "simulated"},
        },
        demo_run_id=run_id,
    )


class Collector:
    def __init__(self, values):
        self.values = iter(values)
        self.ident = 0

    async def collect(self, *, now, demo_run_id):
        return next(self.values)

    async def persist(self, snapshot):
        self.ident += 1
        snapshot.id = self.ident
        return snapshot


class Predictions:
    def __init__(self):
        self.rows = []

    async def create_with_points(self, prediction, points):
        self.rows.append(prediction)
        return prediction

    async def record_reactive_comparator(self, **values):
        return None


class Actions:
    def __init__(self):
        self.rows = {}

    async def has_unresolved(self, **values):
        return False

    async def claim(self, command, **values):
        existing = self.rows.get(command.idempotency_key)
        if existing is not None:
            return SimpleNamespace(action=existing, claimed=False)
        action = SimpleNamespace(
            idempotency_key=command.idempotency_key,
            requested_desired_capacity=command.requested_desired_capacity,
            applied_desired_capacity=None,
            max_instance_ceiling=values["effective_ceiling"],
            execution_mode=values["execution_mode"].value,
            status=ActionStatus.PLANNED.value,
            provider_request_id=None,
            error_message=None,
            cooldown_until=None,
        )
        self.rows[command.idempotency_key] = action
        return SimpleNamespace(action=action, claimed=True)

    async def update_outcome(self, **values):
        action = self.rows[values["idempotency_key"]]
        action.status = values["status"].value
        action.applied_desired_capacity = values["applied_desired_capacity"]
        return action

    async def record_control_error(self, **values):
        raise AssertionError("assembled lifecycle should not fail tier control")


class Shedding:
    def __init__(self):
        self.level = SheddingLevel.NORMAL
        self.levels = []

    async def apply(self, *, command, level):
        self.level = level
        self.levels.append(level)
        return SheddingControlResult(
            True,
            level,
            str(uuid4()),
            {"checkout": "normal"},
        )


class StateMachine(ControlStateMachine):
    def __init__(self):
        super().__init__()
        self.states = []

    def transition(self, event, *, recovered_target=None):
        result = super().transition(event, recovered_target=recovered_target)
        if result.changed:
            self.states.append(result.current)
        return result


class ActiveRun:
    def __init__(self, run_id):
        self.run_id = run_id

    async def current_demo_run_id(self, *, environment):
        assert environment == "test"
        return self.run_id

    async def record_reactive_comparator(self, **values):
        return self


def test_assembled_realtime_runtime_enters_watch_and_fully_unwinds() -> None:
    run_id = uuid4()
    clock = Clock()
    collector = Collector(
        [
            signals(0, 10, run_id),
            signals(1, 10, run_id),
            signals(2, 25, run_id),
            signals(3, 50, run_id),
            signals(4, 1, run_id),
            signals(5, 1, run_id),
        ]
    )
    predictions = PredictionService(
        Predictions(),
        target_resource="pulse-asg",
        maximum_ceiling=3,
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=1,
            cooldown_seconds=0,
            decrement_step=1,
            capacity_floor=1,
        ),
    )
    capacity = AutoScalingCapacityAdapter(
        execution_mode=ExecutionMode.DRY_RUN,
        target_resource="pulse-asg",
        region=None,
        global_ceiling=3,
        simulated_capacity=1,
    )
    shedding = Shedding()
    states = StateMachine()
    pipeline = ResponsePipeline(
        repository=Actions(),
        capacity_adapter=capacity,
        shedding_client=shedding,
        state_machine=states,
        global_ceiling=3,
    )
    recovery = RecoveryWorker(
        coordinator=RecoveryCoordinator(states),
        response_pipeline=pipeline,
    )

    async def observe():
        current = await capacity.read_capacity(observed_at=clock.now())
        return RealtimeControlObservation(
            observed_at=clock.now(),
            current_capacity=current.state.desired,
            current_shedding_level=shedding.level,
        )

    lifecycle = RealtimeControlLifecycle(
        state_machine=states,
        recovery_worker=recovery,
        observation_provider=observe,
    )

    async def handle(command):
        current = await capacity.read_capacity(observed_at=clock.now())
        await pipeline.execute(
            command,
            current_capacity=current.state.desired,
            current_shedding_level=shedding.level,
            requested_at=clock.now(),
        )

    worker = RealtimeWorker(
        collector=collector,  # type: ignore[arg-type]
        detector=RealtimeDetector(
            RealtimeDetectorConfig(
                environment="test",
                baseline_window_seconds=60,
                minimum_samples=3,
                maximum_samples=10,
                acceleration_threshold_rps2=2,
                entry_ratio_threshold=1.5,
                exit_ratio_threshold=1.1,
                confidence_threshold=0.55,
                confirmation_count=2,
                reactive_load_threshold_rps=100,
                forecast_horizon_seconds=2,
                rps_per_instance=20,
                maximum_capacity=3,
            )
        ),
        prediction_service=predictions,
        clock=clock,
        poll_seconds=1,
        command_handler=handle,
        decision_handler=lifecycle.handle,
        active_run_provider=ActiveRun(run_id),
        environment="test",
    )

    async def run():
        for second in range(6):
            clock.current = NOW + timedelta(seconds=second)
            await worker.run_once()

    asyncio.run(run())

    assert states.states == [
        ControlState.WATCH,
        ControlState.PROTECT,
        ControlState.RECOVERY,
        ControlState.COOLDOWN,
        ControlState.RECOVERY,
        ControlState.NORMAL,
    ]
    assert capacity._simulated_capacity == 1
    assert shedding.levels == [
        SheddingLevel.DISABLE_RECOMMENDATIONS,
        SheddingLevel.NORMAL,
    ]
