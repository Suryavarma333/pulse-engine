from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent.app.detection.realtime import RealtimeDetector, RealtimeDetectorConfig
from agent.app.metrics.collector import CollectedSignals, RequiredSignalUnavailable
from agent.app.services.predictions import PredictionService
from agent.app.workers.realtime import RealtimeWorker
from common.contracts import RecoveryPlan
from common.enums import ProviderStatus

NOW = datetime(2026, 8, 18, 13, 0, tzinfo=UTC)


class ManualClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


def sample(second: int, rate: float, *, demo_run_id=None) -> CollectedSignals:
    observed_at = NOW + timedelta(seconds=second)
    return CollectedSignals(
        observed_at=observed_at,
        origin_observed_at=observed_at,
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
        optional_values={"edge_request_rate_rps": rate + 5, "queue_depth": rate},
        provider_health={
            "demo_app": {"status": "healthy"},
            "simulated": {"status": "simulated"},
        },
        demo_run_id=demo_run_id,
    )


class FakeCollector:
    def __init__(self, samples, events, *, fail_persist_at: int | None = None) -> None:
        self.samples = iter(samples)
        self.events = events
        self.persist_attempt = 0
        self.fail_persist_at = fail_persist_at

    async def collect(self, *, now, demo_run_id=None):
        self.events.append("collect")
        value = next(self.samples)
        if isinstance(value, Exception):
            raise value
        return value

    async def persist(self, snapshot):
        self.persist_attempt += 1
        if self.persist_attempt == self.fail_persist_at:
            self.events.append("snapshot_failed")
            raise RuntimeError("database unavailable")
        snapshot.id = sum(event == "snapshot" for event in self.events) + 1
        self.events.append("snapshot")
        return snapshot


class MemoryPredictionWriter:
    def __init__(self, events) -> None:
        self.events = events
        self.predictions = []
        self.points = []

    async def create_with_points(self, prediction, points):
        self.events.append("prediction")
        self.predictions.append(prediction)
        self.points.extend(points)
        return prediction

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
        if prediction is not None:
            prediction.reactive_comparator_crossed_at = crossed_at
            prediction.signal_evidence = {
                **prediction.signal_evidence,
                "reactive_comparator_crossed_at": crossed_at.isoformat(),
            }
            self.events.append("comparator")
        return prediction


def make_worker(
    collector,
    writer,
    clock,
    events,
    commands,
    *,
    active_run_provider=None,
    command_handler_error: bool = False,
) -> RealtimeWorker:
    detector = RealtimeDetector(
        RealtimeDetectorConfig(
            environment="test",
            baseline_window_seconds=60,
            minimum_samples=3,
            maximum_samples=10,
            acceleration_threshold_rps2=2,
            entry_ratio_threshold=1.5,
            exit_ratio_threshold=1.1,
            confidence_threshold=0.65,
            confirmation_count=1,
            reactive_load_threshold_rps=50,
            forecast_horizon_seconds=10,
            rps_per_instance=20,
            maximum_capacity=3,
        )
    )
    predictions = PredictionService(
        writer,
        target_resource="pulse-test-asg",
        maximum_ceiling=3,
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=3,
            cooldown_seconds=30,
            decrement_step=1,
            capacity_floor=1,
        ),
    )

    async def command_handler(command):
        events.append("command")
        if command_handler_error:
            raise RuntimeError("response pipeline unavailable")
        commands.append(command)

    return RealtimeWorker(
        collector=collector,
        detector=detector,
        prediction_service=predictions,
        clock=clock,
        poll_seconds=1,
        command_handler=command_handler,
        active_run_provider=active_run_provider,
    )


class ActiveRunRecorder:
    def __init__(self, run_id) -> None:
        self.run_id = run_id
        self.comparator_at = None

    async def current_demo_run_id(self):
        return self.run_id

    async def record_reactive_comparator(self, *, run_id, crossed_at):
        assert run_id == self.run_id
        self.comparator_at = crossed_at
        return self


def test_sudden_spike_persists_snapshot_prediction_points_and_command_before_comparator() -> None:
    events = []
    commands = []
    clock = ManualClock()
    writer = MemoryPredictionWriter(events)
    collector = FakeCollector([sample(0, 10), sample(1, 10), sample(2, 25)], events)
    worker = make_worker(collector, writer, clock, events, commands)

    async def run():
        results = []
        for second in (0, 1, 2):
            clock.current = NOW + timedelta(seconds=second)
            results.append(await worker.run_once())
        return results

    results = asyncio.run(run())

    trigger = results[-1]
    assert trigger.held is False
    assert trigger.prediction is not None
    assert trigger.command is commands[0]
    assert trigger.command.trigger_snapshot_id == 3
    assert trigger.command.idempotency_key == "realtime:test:3"
    assert writer.predictions[0].reactive_comparator_crossed_at is None
    assert len(writer.points) == 2
    assert events[-3:] == ["snapshot", "prediction", "command"]
    assert worker.health.status is ProviderStatus.HEALTHY


def test_required_origin_failure_is_a_degraded_no_mutation_hold() -> None:
    events = []
    commands = []
    clock = ManualClock()
    unavailable = RequiredSignalUnavailable({"demo_app": {"status": "unavailable"}})
    collector = FakeCollector([unavailable], events)
    writer = MemoryPredictionWriter(events)
    worker = make_worker(collector, writer, clock, events, commands)

    result = asyncio.run(worker.run_once())

    assert result.held is True
    assert result.reason == "required_origin_unavailable"
    assert commands == []
    assert writer.predictions == []
    assert worker.health.status is ProviderStatus.DEGRADED


def test_unexpected_collection_failure_is_a_degraded_no_mutation_hold() -> None:
    events = []
    commands = []
    clock = ManualClock()
    collector = FakeCollector([RuntimeError("provider failed")], events)
    writer = MemoryPredictionWriter(events)
    worker = make_worker(collector, writer, clock, events, commands)

    result = asyncio.run(worker.run_once())

    assert result.held is True
    assert result.reason == "collection_failed"
    assert commands == []
    assert writer.predictions == []
    assert worker.health.detail == "collection_failed:RuntimeError"


def test_persistence_failure_rolls_back_detector_confirmation_and_emits_no_command() -> None:
    events = []
    commands = []
    clock = ManualClock()
    writer = MemoryPredictionWriter(events)
    collector = FakeCollector(
        [sample(0, 10), sample(1, 10), sample(2, 25), sample(2, 25)],
        events,
        fail_persist_at=3,
    )
    worker = make_worker(collector, writer, clock, events, commands)

    async def run():
        for second in (0, 1):
            clock.current = NOW + timedelta(seconds=second)
            await worker.run_once()
        clock.current = NOW + timedelta(seconds=2)
        failed = await worker.run_once()
        retried = await worker.run_once()
        return failed, retried

    failed, retried = asyncio.run(run())

    assert failed.held is True
    assert failed.reason == "persistence_failed"
    assert len(commands) == 1
    assert retried.command is commands[0]
    assert events.count("prediction") == 1


def test_command_handoff_failure_preserves_the_persisted_prediction() -> None:
    events = []
    commands = []
    clock = ManualClock()
    writer = MemoryPredictionWriter(events)
    collector = FakeCollector([sample(0, 10), sample(1, 10), sample(2, 25)], events)
    worker = make_worker(
        collector,
        writer,
        clock,
        events,
        commands,
        command_handler_error=True,
    )

    async def run():
        result = None
        for second in (0, 1, 2):
            clock.current = NOW + timedelta(seconds=second)
            result = await worker.run_once()
        return result

    result = asyncio.run(run())

    assert result is not None
    assert result.held is True
    assert result.reason == "command_handoff_failed"
    assert result.prediction is not None
    assert len(writer.predictions) == 1
    assert commands == []
    assert worker.health.detail == "command_handoff_failed:RuntimeError"


def test_later_reactive_comparator_is_persisted_on_the_early_prediction() -> None:
    events = []
    commands = []
    clock = ManualClock()
    writer = MemoryPredictionWriter(events)
    run_id = uuid4()
    run_recorder = ActiveRunRecorder(run_id)
    collector = FakeCollector(
        [
            sample(0, 10, demo_run_id=run_id),
            sample(1, 10, demo_run_id=run_id),
            sample(2, 25, demo_run_id=run_id),
            sample(3, 60, demo_run_id=run_id),
        ],
        events,
    )
    worker = make_worker(
        collector,
        writer,
        clock,
        events,
        commands,
        active_run_provider=run_recorder,
    )

    async def run():
        results = []
        for second in (0, 1, 2, 3):
            clock.current = NOW + timedelta(seconds=second)
            results.append(await worker.run_once())
        return results

    results = asyncio.run(run())

    crossed_at = NOW + timedelta(seconds=3)
    assert results[2].prediction is not None
    assert results[2].prediction.prediction.reactive_comparator_crossed_at == crossed_at
    assert results[3].decision.reactive_comparator_crossed_at == crossed_at
    assert writer.predictions[0].signal_evidence["reactive_comparator_crossed_at"] == (
        crossed_at.isoformat()
    )
    assert run_recorder.comparator_at == crossed_at
    assert events.count("comparator") == 1
