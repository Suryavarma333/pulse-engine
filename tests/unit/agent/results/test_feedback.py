from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.app.metrics.collector import CollectedSignals
from agent.app.services.feedback import (
    EvaluationBundle,
    FeedbackEvaluator,
    FeedbackService,
    FeedbackWorker,
)
from common.contracts import CapacityState
from common.enums import DemoRunStatus, ProviderStatus
from db.models import DemoRun, ScalingAction, SurgePrediction, TrafficSnapshot

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def run(**overrides) -> DemoRun:
    values = {
        "id": uuid4(),
        "idempotency_key": "run-1",
        "scenario_name": "sudden-spike",
        "mode": "realtime",
        "environment": "local",
        "baseline_type": "pulse",
        "execution_mode": "dry_run",
        "configuration": {"rps_per_instance": 25, "minimum_desired_capacity": 1},
        "thresholds": {"onset_ratio": 1.2},
        "started_at": NOW,
        "ended_at": NOW + timedelta(minutes=3),
        "status": "pending_evaluation",
        "reactive_comparator_crossed_at": NOW + timedelta(seconds=90),
        "formula_version": "v1",
        "locust_summary": {
            "request_count": 1000,
            "failure_count": 10,
            "checkout": {
                "attempts": 100,
                "successes": 99,
                "p99_latency_ms": 123,
            },
        },
        "result_summary": {},
        "warnings": {},
    }
    values.update(overrides)
    return DemoRun(**values)


def snapshot(index: int, rate: float, capacity: int | None, tier: int = 0) -> TrafficSnapshot:
    return TrafficSnapshot(
        id=index + 1,
        environment="local",
        demo_run_id=None,
        observed_at=NOW + timedelta(seconds=index * 60),
        window_seconds=60,
        origin_request_rate_rps=rate,
        baseline_request_rate_rps=10,
        request_count=100,
        concurrent_requests=10,
        request_rate_change_rps=0,
        request_acceleration_rps2=0,
        checkout_p99_latency_ms=100 + index,
        checkout_success_rate=0.99,
        error_rate=0.01,
        asg_desired_capacity=capacity,
        asg_in_service_capacity=capacity,
        load_shedding_level=tier,
        reactive_comparator_crossed=False,
        signal_details={},
    )


def prediction(**overrides) -> SurgePrediction:
    values = {
        "id": uuid4(),
        "mode": "realtime",
        "environment": "local",
        "correlation_id": uuid4(),
        "model_name": "rolling",
        "model_version": "v1",
        "created_at": NOW + timedelta(seconds=30),
        "basis_window_start": NOW,
        "basis_window_end": NOW + timedelta(seconds=30),
        "predicted_start_at": NOW + timedelta(seconds=30),
        "predicted_peak_at": NOW + timedelta(seconds=100),
        "baseline_rps": 10,
        "predicted_peak_rps": 80,
        "predicted_multiplier": 8,
        "recommended_capacity": 3,
        "confidence": 0.9,
        "trigger_type": "acceleration",
        "reasoning": "test",
        "signal_evidence": {},
        "status": "active",
        "reactive_comparator_crossed_at": NOW + timedelta(seconds=90),
        "formula_version": "v1",
    }
    values.update(overrides)
    return SurgePrediction(**values)


def test_formula_fixture_reconciles_exact_traceable_metrics() -> None:
    snapshots = (
        snapshot(0, 10, 1),
        snapshot(1, 50, 2, tier=1),
        snapshot(2, 100, 3, tier=1),
        snapshot(3, 10, 1),
    )
    action = ScalingAction(id=uuid4())
    outcome = FeedbackEvaluator().evaluate(
        run(),
        EvaluationBundle(snapshots, (prediction(),), (action,), ()),
    )

    assert outcome.actual_start_at == NOW + timedelta(minutes=1)
    assert outcome.actual_peak_at == NOW + timedelta(minutes=2)
    assert outcome.actual_peak_rps == 100
    assert outcome.metrics.checkout_p99_latency_ms == 123
    assert outcome.metrics.checkout_success_rate == pytest.approx(0.99)
    assert outcome.metrics.detection_lead_seconds == 60
    assert outcome.metrics.prediction_error_pct == pytest.approx(20)
    assert outcome.metrics.provisioning_efficiency_pct == 100
    assert outcome.metrics.overprovisioned_instance_minutes == 0
    assert outcome.metrics.underprovisioned_seconds == 60
    assert outcome.metrics.error_rate == pytest.approx(0.01)
    assert outcome.metrics.recovery_duration_seconds == 60
    assert outcome.summary["references"]["action_ids"] == [str(action.id)]
    assert outcome.summary["formula_version"] == "v1"
    assert "paired_reactive_baseline_unavailable" in outcome.metrics.warnings


def test_collector_production_snapshots_close_run_with_provisioning_metrics() -> None:
    def collected(index: int, rate: float, desired: int) -> TrafficSnapshot:
        at = NOW + timedelta(seconds=index * 30)
        signals = CollectedSignals(
            observed_at=at,
            origin_observed_at=at,
            origin_request_rate_rps=rate,
            request_count=int(rate * 30),
            concurrent_requests=2,
            error_rate=0,
            p50_latency_ms=10,
            p95_latency_ms=20,
            p99_latency_ms=30,
            checkout_p99_latency_ms=20,
            checkout_success_rate=1,
            load_shedding_level=0,
            optional_values={},
            provider_health={"demo_app": {"status": "healthy"}},
            capacity_state=CapacityState(
                desired=desired,
                in_service=desired,
                pending=0,
                observed_at=at,
                provider_status=ProviderStatus.SIMULATED,
            ),
            capacity_per_instance_rps=20,
        )
        result = signals.to_snapshot(
            window_seconds=60,
            baseline_request_rate_rps=10,
            request_rate_change_rps=0,
            request_acceleration_rps2=0,
            queue_growth_per_second=None,
            reactive_comparator_crossed=False,
            evidence={"environment": "local"},
        )
        result.id = index + 1
        return result

    item = run(
        configuration={
            "settings_snapshot_version": "v1",
            "rps_per_instance": 20,
            "minimum_desired_capacity": 1,
        }
    )
    outcome = FeedbackEvaluator().evaluate(
        item,
        EvaluationBundle(
            (collected(0, 20, 1), collected(1, 55, 3), collected(2, 10, 1)),
            (prediction(),),
            (),
            (),
        ),
    )

    assert outcome.metrics.provisioning_efficiency_pct is not None
    assert outcome.metrics.overprovisioned_instance_minutes is not None
    assert outcome.metrics.underprovisioned_seconds is not None


def test_missing_comparator_incomplete_and_zero_denominators_are_explicit() -> None:
    incomplete = run(
        status=DemoRunStatus.RUNNING.value,
        ended_at=None,
        reactive_comparator_crossed_at=None,
        locust_summary={
            "request_count": 0,
            "failure_count": 0,
            "checkout": {"attempts": 0, "successes": 0},
        },
    )
    snapshots = (snapshot(0, 0, None), snapshot(1, 0, None))
    outcome = FeedbackEvaluator().evaluate(
        incomplete,
        EvaluationBundle(
            snapshots,
            (prediction(reactive_comparator_crossed_at=None, predicted_peak_rps=0),),
            (),
            (),
        ),
    )

    warnings = outcome.metrics.warnings
    assert outcome.metrics.detection_lead_seconds is None
    assert outcome.metrics.prediction_error_pct is None
    assert outcome.metrics.checkout_success_rate is None
    assert outcome.metrics.provisioning_efficiency_pct is None
    assert outcome.metrics.error_rate is None
    assert "incomplete_run" in warnings
    assert "reactive_comparator_unavailable" in warnings
    assert "prediction_error_zero_denominator" in warnings
    assert "checkout_success_zero_denominator" in warnings
    assert "provisioning_intervals_unavailable" in warnings
    assert "error_rate_zero_denominator" in warnings
    assert outcome.summary["comparability"]["complete"] is False


class RunStore:
    def __init__(self, item) -> None:
        self.item = item
        self.complete_calls = []

    async def get(self, run_id):
        return self.item if self.item.id == run_id else None

    async def complete(self, **values):
        self.complete_calls.append(values)
        self.item.status = values["status"].value
        self.item.result_summary = values.get("result_summary", {})
        return self.item

    async def list_pending_evaluation(self, *, limit=50):
        return [self.item]


class DataSource:
    def __init__(self, bundle) -> None:
        self.bundle = bundle

    async def load_for_run(self, run, *, limit):
        return self.bundle


class PredictionStore:
    def __init__(self) -> None:
        self.calls = []

    async def update_evaluation(self, **values):
        self.calls.append(values)
        return values


class Clock:
    def __init__(self, value) -> None:
        self.value = value

    def now(self):
        return self.value


def test_feedback_service_persists_versioned_summary_and_prediction_evaluation() -> None:
    item = run()
    runs = RunStore(item)
    predictions = PredictionStore()
    service = FeedbackService(
        runs=runs,
        data_source=DataSource(
            EvaluationBundle(
                (snapshot(0, 10, 1), snapshot(1, 50, 2)),
                (prediction(),),
                (),
                (),
            )
        ),
        predictions=predictions,
    )

    result = asyncio.run(service.evaluate_run(item.id))
    assert result.status == "completed"
    assert runs.complete_calls[0]["result_summary"]["formula_version"] == "v1"
    assert predictions.calls[0]["actual_peak_rps"] == 50

    with pytest.raises(LookupError, match="not found"):
        asyncio.run(service.evaluate_run(uuid4()))


def test_feedback_worker_defers_until_horizon_then_evaluates() -> None:
    item = run(ended_at=NOW)
    runs = RunStore(item)
    calls = []

    class Service:
        async def evaluate_run(self, run_id):
            calls.append(run_id)

    worker = FeedbackWorker(
        runs=runs,
        service=Service(),
        clock=Clock(NOW + timedelta(seconds=10)),
        horizon_seconds=30,
    )
    deferred = asyncio.run(worker.run_once())
    assert deferred.deferred_run_ids == (item.id,)
    assert calls == []

    worker._clock = Clock(NOW + timedelta(seconds=31))
    evaluated = asyncio.run(worker.run_once())
    assert evaluated.evaluated_run_ids == (item.id,)
    assert calls == [item.id]
    assert worker.status.value == "healthy"


def test_feedback_worker_surfaces_failure_as_unavailable() -> None:
    class BrokenRuns:
        async def list_pending_evaluation(self, *, limit=50):
            raise RuntimeError("database down")

    worker = FeedbackWorker(
        runs=BrokenRuns(),
        service=SimpleNamespace(),
        clock=Clock(NOW),
        horizon_seconds=0,
    )
    with pytest.raises(RuntimeError, match="database down"):
        asyncio.run(worker.run_once())
    assert worker.status.value == "unavailable"
    assert "RuntimeError" in worker.detail
