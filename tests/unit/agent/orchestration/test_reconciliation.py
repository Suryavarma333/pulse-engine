from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agent.app.aws.autoscaling import CapacityObservation
from agent.app.orchestration.reconciliation import ActionReconciler
from common.contracts import CapacityState
from common.enums import ActionStatus, ProviderStatus

NOW = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)


def action(*, status=ActionStatus.PLANNED, requested=3, ceiling=3):
    return SimpleNamespace(
        idempotency_key="action:1",
        status=status.value,
        target_resource="pulse-asg",
        requested_desired_capacity=requested,
        max_instance_ceiling=ceiling,
        provider_request_id=None,
        cooldown_until=None,
    )


class FakeRepository:
    def __init__(self, actions):
        self.actions = actions
        self.cutoff = None
        self.limit = None
        self.updates = []

    async def list_reconciliation_candidates(self, *, requested_before, limit):
        self.cutoff = requested_before
        self.limit = limit
        return self.actions

    async def update_outcome(self, **values):
        self.updates.append(values)
        return values


class FakeAdapter:
    target_resource = "pulse-asg"

    def __init__(self, *, desired=3, failure=None):
        self.desired = desired
        self.failure = failure

    async def read_capacity(self, *, observed_at):
        if self.failure is not None:
            raise self.failure
        return CapacityObservation(
            CapacityState(
                desired=self.desired,
                in_service=self.desired,
                pending=0,
                observed_at=observed_at,
                provider_status=ProviderStatus.HEALTHY,
            ),
            provider_request_id="describe-id",
        )


def test_matching_planned_action_is_reconciled_without_mutation() -> None:
    repository = FakeRepository([action()])
    reconciler = ActionReconciler(
        repository=repository,
        capacity_adapter=FakeAdapter(),
        minimum_age_seconds=30,
        batch_size=25,
    )
    results = asyncio.run(reconciler.reconcile_due(now=NOW))
    assert repository.cutoff == NOW - timedelta(seconds=30)
    assert repository.limit == 25
    assert results[0].status is ActionStatus.RECONCILED
    assert results[0].matched_requested_target is True
    assert repository.updates[0]["provider_request_id"] == "describe-id"
    assert repository.updates[0]["reconciled_at"] == NOW


def test_unknown_provider_read_remains_unknown_and_sanitizes_error() -> None:
    repository = FakeRepository([])
    reconciler = ActionReconciler(
        repository=repository,
        capacity_adapter=FakeAdapter(failure=RuntimeError("credential=secret-value")),
    )
    result = asyncio.run(reconciler.reconcile(action(status=ActionStatus.UNKNOWN), now=NOW))
    assert result.status is ActionStatus.UNKNOWN
    assert "secret-value" not in result.error
    assert repository.updates[0]["status"] is ActionStatus.UNKNOWN


def test_different_provider_state_is_audited_as_reconciled_observation() -> None:
    repository = FakeRepository([])
    reconciler = ActionReconciler(
        repository=repository,
        capacity_adapter=FakeAdapter(desired=2),
    )
    result = asyncio.run(reconciler.reconcile(action(requested=3), now=NOW))
    assert result.status is ActionStatus.RECONCILED
    assert result.observed_capacity == 2
    assert result.matched_requested_target is False
    assert result.error == "provider_state_differs:expected=3:observed=2"
    assert repository.updates[0]["applied_desired_capacity"] == 2


def test_target_mismatch_stays_unknown_without_provider_read() -> None:
    candidate = action()
    candidate.target_resource = "other-asg"
    repository = FakeRepository([])
    reconciler = ActionReconciler(
        repository=repository,
        capacity_adapter=FakeAdapter(),
    )
    result = asyncio.run(reconciler.reconcile(candidate, now=NOW))
    assert result.status is ActionStatus.UNKNOWN
    assert result.error == "reconciliation_target_mismatch"
