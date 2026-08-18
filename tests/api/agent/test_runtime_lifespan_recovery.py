from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from agent.app.config import AgentSettings
from agent.app.detection.realtime import RealtimeDetector, RealtimeDetectorConfig
from agent.app.main import AgentComponents, create_app
from agent.app.metrics.collector import CollectedSignals
from agent.app.orchestration.state_machine import ControlStateMachine
from agent.app.services.predictions import PredictionService
from agent.app.workers.realtime import RealtimeWorker
from common.contracts import RecoveryPlan
from common.enums import ProviderStatus

NOW = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)


class RuntimeClock:
    def now(self) -> datetime:
        return NOW


class ToggleDatabase:
    def __init__(self, *, available: bool) -> None:
        self.available = available

    def session_factory(self) -> ToggleDatabaseSession:
        return ToggleDatabaseSession(self)

    async def dispose(self) -> None:
        return None


class ToggleDatabaseSession:
    def __init__(self, database: ToggleDatabase) -> None:
        self._database = database

    async def __aenter__(self) -> ToggleDatabaseSession:
        if not self._database.available:
            raise RuntimeError("database unavailable")
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, statement: Any) -> None:
        del statement
        if not self._database.available:
            raise RuntimeError("database unavailable")


class ToggleActiveRunProvider:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.lookup_attempts = 0
        self.successful_lookups = 0

    async def current_demo_run_id(self, *, environment: str):
        assert environment == "test"
        self.lookup_attempts += 1
        if not self.available:
            raise RuntimeError("active run database lookup failed")
        self.successful_lookups += 1
        return None

    async def record_reactive_comparator(self, **values: Any) -> None:
        del values
        return None


class HealthyCollector:
    def __init__(self) -> None:
        self.cycles = 0

    async def collect(self, *, now: datetime, demo_run_id=None) -> CollectedSignals:
        del now
        self.cycles += 1
        observed_at = NOW + timedelta(milliseconds=self.cycles)
        return CollectedSignals(
            observed_at=observed_at,
            origin_observed_at=observed_at,
            origin_request_rate_rps=10,
            request_count=10,
            concurrent_requests=1,
            error_rate=0,
            p50_latency_ms=1,
            p95_latency_ms=2,
            p99_latency_ms=3,
            checkout_p99_latency_ms=2,
            checkout_success_rate=1,
            load_shedding_level=0,
            optional_values={},
            provider_health={"demo_app": {"status": "healthy"}},
            demo_run_id=demo_run_id,
        )

    async def persist(self, snapshot):
        snapshot.id = self.cycles
        return snapshot


class UnusedPredictionWriter:
    async def create_with_points(self, prediction, points):
        del points
        return prediction

    async def record_reactive_comparator(self, **values: Any) -> None:
        del values
        return None


def _wait_until(predicate, *, detail: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {detail}")


def _realtime_worker(
    *,
    clock: RuntimeClock,
    collector: HealthyCollector,
    active_runs: ToggleActiveRunProvider,
) -> RealtimeWorker:
    detector = RealtimeDetector(
        RealtimeDetectorConfig(
            environment="test",
            minimum_samples=2,
            maximum_samples=10,
            confirmation_count=1,
        )
    )
    predictions = PredictionService(
        UnusedPredictionWriter(),
        target_resource="pulse-test-asg",
        maximum_ceiling=3,
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=2,
            cooldown_seconds=30,
            decrement_step=1,
            capacity_floor=1,
        ),
    )
    return RealtimeWorker(
        collector=collector,
        detector=detector,
        prediction_service=predictions,
        clock=clock,
        poll_seconds=0.01,
        active_run_provider=active_runs,
        environment="test",
    )


def test_production_lifespan_realtime_worker_survives_database_outages() -> None:
    settings = AgentSettings(
        environment="test",
        control_token="test-control-token",
        metric_poll_seconds=0.01,
        maintenance_poll_seconds=0.01,
    )
    clock = RuntimeClock()
    database = ToggleDatabase(available=False)
    active_runs = ToggleActiveRunProvider(available=False)
    collector = HealthyCollector()
    realtime = _realtime_worker(
        clock=clock,
        collector=collector,
        active_runs=active_runs,
    )
    components = AgentComponents(
        settings=settings,
        clock=clock,
        simulated_signal_buffer=SimpleNamespace(),
        operator_store=SimpleNamespace(),
        demo_runs=active_runs,
        feedback_service=SimpleNamespace(),
        capacity_adapter=SimpleNamespace(),
        state_machine=ControlStateMachine(),
        recovery_coordinator=SimpleNamespace(low_confirmations=0),
        workers=[realtime],
        database=database,
        db_ready=False,
    )
    app = create_app(components=components, start_workers=True)

    with TestClient(app) as client:
        task = app.state.worker_tasks_by_name["realtime"]
        _wait_until(
            lambda: active_runs.lookup_attempts >= 2
            and realtime.health.detail == "active_run_lookup_failed:RuntimeError",
            detail="initial active-run lookup failure",
        )
        assert task.done() is False
        initial_failure = client.get("/health")
        assert initial_failure.status_code == 503
        realtime_health = next(
            item for item in initial_failure.json()["workers"] if item["name"] == "realtime"
        )
        assert realtime_health == {
            "name": "realtime",
            "status": "unavailable",
            "detail": "active_run_lookup_failed:RuntimeError",
        }

        database.available = True
        active_runs.available = True
        _wait_until(
            lambda: app.state.db_ready
            and realtime.health.status is ProviderStatus.HEALTHY
            and collector.cycles > 0,
            detail="initial database recovery",
        )
        assert client.get("/health").status_code == 200

        database.available = False
        active_runs.available = False
        _wait_until(
            lambda: not app.state.db_ready
            and realtime.health.detail == "active_run_lookup_failed:RuntimeError",
            detail="runtime database outage",
        )
        outage_cycles = collector.cycles
        assert app.state.worker_tasks_by_name["realtime"] is task
        assert task.done() is False
        runtime_failure = client.get("/health")
        assert runtime_failure.status_code == 503
        assert runtime_failure.json()["database"] == {"ready": False}

        database.available = True
        active_runs.available = True
        _wait_until(
            lambda: app.state.db_ready
            and realtime.health.status is ProviderStatus.HEALTHY
            and collector.cycles > outage_cycles,
            detail="runtime database recovery",
        )
        assert app.state.worker_tasks_by_name["realtime"] is task
        assert task.done() is False
        recovered = client.get("/health")
        assert recovered.status_code == 200
        assert recovered.json()["ready"] is True
        recovered_realtime = next(
            item for item in recovered.json()["workers"] if item["name"] == "realtime"
        )
        assert recovered_realtime["status"] == "healthy"
        assert recovered_realtime["detail"] == "cycle_complete"
