from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from agent.app.aws.autoscaling import CapacityObservation, sanitize_provider_error
from common.enums import ActionStatus
from common.time import ensure_utc


class ReconciliationAdapter(Protocol):
    target_resource: str

    async def read_capacity(self, *, observed_at: datetime) -> CapacityObservation: ...


class ReconciliationRepository(Protocol):
    async def list_reconciliation_candidates(
        self,
        *,
        requested_before: datetime,
        limit: int,
    ) -> list[Any]: ...

    async def update_outcome(self, **values: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    idempotency_key: str
    previous_status: ActionStatus
    status: ActionStatus
    observed_capacity: int | None
    matched_requested_target: bool
    error: str | None = None


class ActionReconciler:
    """Resolves stale planned/unknown actions by observing provider state only."""

    def __init__(
        self,
        *,
        repository: ReconciliationRepository,
        capacity_adapter: ReconciliationAdapter,
        minimum_age_seconds: int = 30,
        batch_size: int = 50,
    ) -> None:
        if not 0 <= minimum_age_seconds <= 86_400:
            raise ValueError("minimum_age_seconds must be between 0 and 86400")
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        self._repository = repository
        self._capacity = capacity_adapter
        self._minimum_age = timedelta(seconds=minimum_age_seconds)
        self._batch_size = batch_size

    async def reconcile_due(self, *, now: datetime) -> list[ReconciliationResult]:
        now = ensure_utc(now, field_name="now")
        actions = await self._repository.list_reconciliation_candidates(
            requested_before=now - self._minimum_age,
            limit=self._batch_size,
        )
        return [await self.reconcile(action, now=now) for action in actions]

    async def reconcile(self, action: Any, *, now: datetime) -> ReconciliationResult:
        now = ensure_utc(now, field_name="now")
        previous_status = ActionStatus(action.status)
        if action.target_resource != self._capacity.target_resource:
            error = "reconciliation_target_mismatch"
            await self._mark_unknown(action, now=now, error=error)
            return ReconciliationResult(
                action.idempotency_key,
                previous_status,
                ActionStatus.UNKNOWN,
                None,
                False,
                error,
            )
        try:
            observation = await self._capacity.read_capacity(observed_at=now)
        except Exception as exc:
            error = sanitize_provider_error(exc)
            await self._mark_unknown(action, now=now, error=error)
            return ReconciliationResult(
                action.idempotency_key,
                previous_status,
                ActionStatus.UNKNOWN,
                None,
                False,
                error,
            )

        expected = min(action.requested_desired_capacity, action.max_instance_ceiling)
        observed = observation.state.desired
        matched = observed == expected
        applied = observed if observed <= action.max_instance_ceiling else None
        error = None
        if not matched:
            error = f"provider_state_differs:expected={expected}:observed={observed}"
        if applied is None:
            error = f"observed_capacity_exceeds_ceiling:{observed}"
        await self._repository.update_outcome(
            idempotency_key=action.idempotency_key,
            status=ActionStatus.RECONCILED,
            applied_desired_capacity=applied,
            executed_at=now,
            provider_request_id=observation.provider_request_id,
            error_message=error,
            cooldown_until=action.cooldown_until,
            reconciled_at=now,
        )
        return ReconciliationResult(
            action.idempotency_key,
            previous_status,
            ActionStatus.RECONCILED,
            observed,
            matched,
            error,
        )

    async def _mark_unknown(self, action: Any, *, now: datetime, error: str) -> None:
        await self._repository.update_outcome(
            idempotency_key=action.idempotency_key,
            status=ActionStatus.UNKNOWN,
            applied_desired_capacity=None,
            executed_at=now,
            provider_request_id=action.provider_request_id,
            error_message=error,
            cooldown_until=action.cooldown_until,
        )


__all__ = ["ActionReconciler", "ReconciliationResult"]
