from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from agent.app.orchestration.reconciliation import ActionReconciler
from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.services.shedding_client import SheddingControlError
from common.contracts import ResponseCommand, WorkerHealth
from common.enums import (
    ActionStatus,
    ControlState,
    ProviderStatus,
    ResponseRetryKind,
    SheddingLevel,
)
from common.time import Clock


@dataclass(frozen=True, slots=True)
class MaintenanceObservation:
    current_capacity: int
    current_shedding_level: SheddingLevel
    request_rate_rps: float
    observed_at: datetime | None = None
    snapshot_id: int | None = None


@dataclass(frozen=True, slots=True)
class DependencyReadiness:
    ready: bool
    recovered_target: ControlState
    detail: str


class RetryStore(Protocol):
    async def list_due(self, *, now: datetime, limit: int) -> list[Any]: ...

    async def mark_succeeded(self, retry_id: object, *, now: datetime) -> Any: ...

    async def mark_failed(
        self, retry_id: object, *, now: datetime, error_message: str
    ) -> Any: ...

    async def reschedule(
        self,
        retry_id: object,
        *,
        next_attempt_at: datetime,
        error_message: str,
    ) -> Any: ...

    async def has_pending(self) -> bool: ...


class ActionStateStore(Protocol):
    async def has_unresolved(self, *, target_resource: str) -> bool: ...

    async def list_pending_shedding(
        self, *, limit: int, max_attempts: int
    ) -> list[Any]: ...


class SnapshotRetentionStore(Protocol):
    async def delete_expired(self, *, cutoff: datetime, limit: int) -> int: ...


ObservationProvider = Callable[[], Awaitable[MaintenanceObservation]]
DependencyProbe = Callable[[], Awaitable[DependencyReadiness]]
RecoveryHandler = Callable[[MaintenanceObservation], Awaitable[None]]


class ControlMaintenanceWorker:
    """Bounded reconciliation, retry, dependency-restoration, and retention loop."""

    name = "maintenance"

    def __init__(
        self,
        *,
        reconciler: ActionReconciler,
        retries: RetryStore,
        actions: ActionStateStore,
        response_pipeline: ResponsePipeline,
        observation_provider: ObservationProvider,
        dependency_probe: DependencyProbe,
        retention_store: SnapshotRetentionStore,
        target_resource: str,
        clock: Clock,
        poll_seconds: float,
        batch_size: int,
        retry_max_attempts: int,
        retry_backoff_seconds: int,
        retention_enabled: bool,
        retention_seconds: int,
        retention_batch_size: int,
        recovery_handler: RecoveryHandler | None = None,
    ) -> None:
        if poll_seconds <= 0 or retry_backoff_seconds <= 0:
            raise ValueError("maintenance intervals must be positive")
        if not 1 <= batch_size <= 1_000 or not 1 <= retry_max_attempts <= 20:
            raise ValueError("maintenance batch or retry attempts are invalid")
        self._reconciler = reconciler
        self._retries = retries
        self._actions = actions
        self._pipeline = response_pipeline
        self._observe = observation_provider
        self._probe = dependency_probe
        self._retention = retention_store
        self._target = target_resource
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._retry_max_attempts = retry_max_attempts
        self._retry_backoff = timedelta(seconds=retry_backoff_seconds)
        self._retention_enabled = retention_enabled
        self._retention_age = timedelta(seconds=retention_seconds)
        self._retention_batch_size = retention_batch_size
        self._recovery_handler = recovery_handler
        self._lock = asyncio.Lock()
        self._health = WorkerHealth(
            name=self.name,
            status=ProviderStatus.DEGRADED,
            checked_at=clock.now(),
            detail="not_started",
        )

    @property
    def health(self) -> WorkerHealth:
        return self._health

    async def run_once(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            now = self._clock.now()
            try:
                reconciled = await self._reconciler.reconcile_due(now=now)
                readiness = await self._probe()
                unresolved = await self._actions.has_unresolved(
                    target_resource=self._target
                )
                if readiness.ready and not unresolved:
                    self._pipeline.recover_dependencies(
                        target=readiness.recovered_target
                    )
                completed = 0
                tier_outbox_completed = 0
                if readiness.ready and not unresolved:
                    pending_tiers = await self._actions.list_pending_shedding(
                        limit=self._batch_size,
                        max_attempts=self._retry_max_attempts,
                    )
                    for action in pending_tiers:
                        try:
                            await self._pipeline.resume_shedding(action)
                            tier_outbox_completed += 1
                        except Exception:
                            # The durable action remains pending with an incremented bounded
                            # attempt count; a process restart can resume it without capacity work.
                            continue
                    retries = await self._retries.list_due(
                        now=now,
                        limit=self._batch_size,
                    )
                    for retry in retries:
                        if await self._process_retry(retry, now=now):
                            completed += 1
                    if (
                        self._recovery_handler is not None
                        and readiness.recovered_target
                        in {ControlState.PROTECT, ControlState.RECOVERY}
                        and not await self._retries.has_pending()
                    ):
                        await self._recovery_handler(await self._observe())
                deleted = 0
                if self._retention_enabled:
                    deleted = await self._retention.delete_expired(
                        cutoff=now - self._retention_age,
                        limit=self._retention_batch_size,
                    )
            except Exception as exc:
                self._set_health(
                    ProviderStatus.DEGRADED,
                    f"maintenance_failed:{type(exc).__name__}",
                )
                return
            status = ProviderStatus.HEALTHY if readiness.ready else ProviderStatus.DEGRADED
            self._set_health(
                status,
                f"{readiness.detail};reconciled={len(reconciled)};"
                f"tier_outbox_completed={tier_outbox_completed};"
                f"retries_completed={completed};snapshots_deleted={deleted}",
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    async def _process_retry(self, retry: Any, *, now: datetime) -> bool:
        command = ResponseCommand.model_validate(retry.command_payload)
        try:
            if ResponseRetryKind(retry.retry_kind) is ResponseRetryKind.SHEDDING:
                if retry.desired_shedding_level is None:
                    raise ValueError("shedding retry is missing a target tier")
                await self._pipeline.retry_shedding(
                    command,
                    level=SheddingLevel(retry.desired_shedding_level),
                )
            else:
                observation = await self._observe()
                execution = await self._pipeline.execute(
                    command,
                    current_capacity=observation.current_capacity,
                    current_shedding_level=observation.current_shedding_level,
                    requested_at=now,
                )
                if execution.capacity.status not in _COMPLETED_CAPACITY_STATUSES:
                    raise RuntimeError(
                        f"dispatch_retry_capacity_{execution.capacity.status.value}"
                    )
                if execution.outcome_persistence_error is not None:
                    raise RuntimeError("dispatch_retry_outcome_persistence_failed")
            await self._retries.mark_succeeded(retry.id, now=now)
            return True
        except SheddingControlError as exc:
            return await self._retry_or_fail(
                retry,
                now=now,
                error_message=f"shedding_retry_failed:{type(exc).__name__}",
                retryable=exc.retryable,
            )
        except Exception as exc:
            return await self._retry_or_fail(
                retry,
                now=now,
                error_message=f"response_retry_failed:{type(exc).__name__}",
                retryable=True,
            )

    async def _retry_or_fail(
        self,
        retry: Any,
        *,
        now: datetime,
        error_message: str,
        retryable: bool,
    ) -> bool:
        if not retryable or retry.attempts + 1 >= self._retry_max_attempts:
            await self._retries.mark_failed(
                retry.id,
                now=now,
                error_message=error_message,
            )
            return False
        delay = self._retry_backoff * (2 ** min(retry.attempts, 6))
        await self._retries.reschedule(
            retry.id,
            next_attempt_at=now + delay,
            error_message=error_message,
        )
        return False

    def _set_health(self, status: ProviderStatus, detail: str) -> None:
        self._health = WorkerHealth(
            name=self.name,
            status=status,
            checked_at=self._clock.now(),
            detail=detail[:500],
        )


_COMPLETED_CAPACITY_STATUSES = {
    ActionStatus.DRY_RUN,
    ActionStatus.NOOP,
    ActionStatus.CAPPED,
    ActionStatus.SUCCEEDED,
    ActionStatus.RECONCILED,
}


__all__ = [
    "ControlMaintenanceWorker",
    "DependencyReadiness",
    "MaintenanceObservation",
]
