from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from agent.app.orchestration.response_pipeline import ResponseExecution
from agent.app.services.shedding_client import SheddingControlResult
from agent.app.workers.maintenance import (
    ControlMaintenanceWorker,
    DependencyReadiness,
    MaintenanceObservation,
)
from common.contracts import CapacityDecision, RecoveryPlan, ResponseCommand
from common.enums import (
    ActionStatus,
    ControlState,
    ExecutionMode,
    PredictionMode,
    ProviderStatus,
    ResponseRetryKind,
    SheddingLevel,
)

NOW = datetime(2026, 8, 18, 17, 0, tzinfo=UTC)


class Clock:
    def now(self):
        return NOW


def command() -> ResponseCommand:
    return ResponseCommand(
        idempotency_key="maintenance:retry:1",
        correlation_id=uuid4(),
        mode=PredictionMode.REALTIME,
        target_resource="pulse-asg",
        requested_desired_capacity=3,
        maximum_ceiling=3,
        requested_shedding_level=SheddingLevel.DISABLE_RECOMMENDATIONS,
        reason_code="surge",
        reasoning="Retry the persisted response safely",
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=2,
            cooldown_seconds=0,
            decrement_step=1,
            capacity_floor=1,
        ),
    )


class Reconciler:
    async def reconcile_due(self, *, now):
        return [SimpleNamespace(id="reconciled")]


class Actions:
    def __init__(self, pending=None):
        self.pending = list(pending or [])

    async def has_unresolved(self, *, target_resource):
        assert target_resource == "pulse-asg"
        return False

    async def list_pending_shedding(self, *, limit, max_attempts):
        assert limit == 10 and max_attempts == 3
        return self.pending[:limit]


class Retries:
    def __init__(self, rows):
        self.rows = rows
        self.succeeded = []
        self.failed = []
        self.rescheduled = []

    async def list_due(self, *, now, limit):
        assert now == NOW and limit == 10
        return self.rows

    async def mark_succeeded(self, retry_id, *, now):
        self.succeeded.append((retry_id, now))

    async def mark_failed(self, retry_id, *, now, error_message):
        self.failed.append((retry_id, now, error_message))

    async def reschedule(self, retry_id, *, next_attempt_at, error_message):
        self.rescheduled.append((retry_id, next_attempt_at, error_message))

    async def has_pending(self):
        succeeded = {item[0] for item in self.succeeded}
        failed = {item[0] for item in self.failed}
        return any(row.id not in succeeded | failed for row in self.rows)


class Retention:
    def __init__(self):
        self.calls = []

    async def delete_expired(self, *, cutoff, limit):
        self.calls.append((cutoff, limit))
        return 7


class Pipeline:
    def __init__(self):
        self.recovered = []
        self.maintenance_recovery = []
        self.shedding = []
        self.dispatched = []

    def recover_dependencies(self, *, target):
        self.recovered.append(target)

    async def retry_shedding(self, command, *, level):
        self.shedding.append((command, level))
        return SheddingControlResult(True, level, "event", {"checkout": "normal"})

    async def resume_shedding(self, action):
        command_value = ResponseCommand.model_validate(action.response_command)
        return await self.retry_shedding(
            command_value,
            level=SheddingLevel(action.requested_shedding_level),
        )

    async def execute(self, command, **values):
        self.dispatched.append((command, values))
        return ResponseExecution(
            action=None,
            capacity=CapacityDecision(
                requested=command.requested_desired_capacity,
                applied=command.requested_desired_capacity,
                ceiling=command.maximum_ceiling,
                execution_mode=ExecutionMode.DRY_RUN,
                status=ActionStatus.DRY_RUN,
            ),
            state=ControlState.PROTECT,
        )


async def _record_recovery(observed, pipeline):
    pipeline.maintenance_recovery.append(observed)


def retry(kind: ResponseRetryKind, *, retry_id: str):
    value = command()
    return SimpleNamespace(
        id=retry_id,
        retry_kind=kind.value,
        command_payload=value.model_dump(mode="json"),
        desired_shedding_level=(
            int(SheddingLevel.DISABLE_RECOMMENDATIONS)
            if kind is ResponseRetryKind.SHEDDING
            else None
        ),
        attempts=0,
    )


def test_maintenance_restores_dependencies_retries_bounded_work_and_prunes() -> None:
    retries = Retries(
        [
            retry(ResponseRetryKind.DISPATCH, retry_id="dispatch"),
            retry(ResponseRetryKind.SHEDDING, retry_id="shedding"),
        ]
    )
    retention = Retention()
    pipeline = Pipeline()

    async def observation():
        return MaintenanceObservation(1, SheddingLevel.NORMAL, 1.0)

    async def probe():
        return DependencyReadiness(True, ControlState.RECOVERY, "dependencies_ready")

    worker = ControlMaintenanceWorker(
        reconciler=Reconciler(),  # type: ignore[arg-type]
        retries=retries,
        actions=Actions(),
        response_pipeline=pipeline,  # type: ignore[arg-type]
        observation_provider=observation,
        dependency_probe=probe,
        retention_store=retention,
        target_resource="pulse-asg",
        clock=Clock(),  # type: ignore[arg-type]
        poll_seconds=1,
        batch_size=10,
        retry_max_attempts=3,
        retry_backoff_seconds=2,
        retention_enabled=True,
        retention_seconds=600,
        retention_batch_size=25,
        recovery_handler=lambda observed: _record_recovery(observed, pipeline),
    )

    asyncio.run(worker.run_once())

    assert pipeline.recovered == [ControlState.RECOVERY]
    assert pipeline.maintenance_recovery == [
        MaintenanceObservation(1, SheddingLevel.NORMAL, 1.0)
    ]
    assert len(pipeline.dispatched) == 1
    assert pipeline.shedding[0][0].idempotency_key == "maintenance:retry:1"
    assert [item[0] for item in retries.succeeded] == ["dispatch", "shedding"]
    assert retention.calls == [(NOW - timedelta(seconds=600), 25)]
    assert worker.health.status is ProviderStatus.HEALTHY
    assert "reconciled=1" in worker.health.detail


def test_maintenance_never_restores_failure_safe_while_dependency_is_unready() -> None:
    retries = Retries([])
    pipeline = Pipeline()
    retention = Retention()

    async def observation():
        return MaintenanceObservation(1, SheddingLevel.NORMAL, 1.0)

    async def probe():
        return DependencyReadiness(False, ControlState.WATCH, "database_unavailable")

    worker = ControlMaintenanceWorker(
        reconciler=Reconciler(),  # type: ignore[arg-type]
        retries=retries,
        actions=Actions(),
        response_pipeline=pipeline,  # type: ignore[arg-type]
        observation_provider=observation,
        dependency_probe=probe,
        retention_store=retention,
        target_resource="pulse-asg",
        clock=Clock(),  # type: ignore[arg-type]
        poll_seconds=1,
        batch_size=10,
        retry_max_attempts=3,
        retry_backoff_seconds=2,
        retention_enabled=False,
        retention_seconds=600,
        retention_batch_size=25,
    )

    asyncio.run(worker.run_once())

    assert pipeline.recovered == []
    assert retention.calls == []
    assert worker.health.status is ProviderStatus.DEGRADED


def test_maintenance_resumes_atomic_tier_outbox_after_restart() -> None:
    pending = SimpleNamespace(
        response_command=command().model_dump(mode="json"),
        requested_shedding_level=int(SheddingLevel.DISABLE_RECOMMENDATIONS),
    )
    pipeline = Pipeline()

    async def observation():
        return MaintenanceObservation(3, SheddingLevel.NORMAL, 20.0)

    async def probe():
        return DependencyReadiness(True, ControlState.PROTECT, "dependencies_ready")

    worker = ControlMaintenanceWorker(
        reconciler=Reconciler(),  # type: ignore[arg-type]
        retries=Retries([]),
        actions=Actions([pending]),
        response_pipeline=pipeline,  # type: ignore[arg-type]
        observation_provider=observation,
        dependency_probe=probe,
        retention_store=Retention(),
        target_resource="pulse-asg",
        clock=Clock(),  # type: ignore[arg-type]
        poll_seconds=1,
        batch_size=10,
        retry_max_attempts=3,
        retry_backoff_seconds=2,
        retention_enabled=False,
        retention_seconds=600,
        retention_batch_size=25,
    )

    asyncio.run(worker.run_once())

    assert len(pipeline.shedding) == 1
    assert pipeline.dispatched == []
    assert "tier_outbox_completed=1" in worker.health.detail


def test_retention_failure_degrades_maintenance_without_unbounded_retry_loop() -> None:
    class FailedRetention:
        async def delete_expired(self, *, cutoff, limit):
            raise RuntimeError("database unavailable")

    async def observation():
        return MaintenanceObservation(1, SheddingLevel.NORMAL, 1.0)

    async def probe():
        return DependencyReadiness(True, ControlState.WATCH, "dependencies_ready")

    worker = ControlMaintenanceWorker(
        reconciler=Reconciler(),  # type: ignore[arg-type]
        retries=Retries([]),
        actions=Actions(),
        response_pipeline=Pipeline(),  # type: ignore[arg-type]
        observation_provider=observation,
        dependency_probe=probe,
        retention_store=FailedRetention(),
        target_resource="pulse-asg",
        clock=Clock(),  # type: ignore[arg-type]
        poll_seconds=1,
        batch_size=10,
        retry_max_attempts=3,
        retry_backoff_seconds=2,
        retention_enabled=True,
        retention_seconds=600,
        retention_batch_size=25,
    )

    asyncio.run(worker.run_once())

    assert worker.health.status is ProviderStatus.DEGRADED
    assert worker.health.detail == "maintenance_failed:RuntimeError"
