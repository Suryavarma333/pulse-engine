from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import QueryWindow, ResponseCommand, validate_evidence
from common.enums import ActionStatus, ExecutionMode
from common.time import ensure_utc
from db.models import ScalingAction
from db.repositories.base import Page, RepositoryLimits


@dataclass(frozen=True, slots=True)
class ClaimResult:
    action: ScalingAction
    claimed: bool


class ScalingActionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def claim(
        self,
        command: ResponseCommand,
        *,
        previous_desired_capacity: int,
        execution_mode: ExecutionMode,
        requested_at: datetime,
        effective_ceiling: int | None = None,
    ) -> ClaimResult:
        if previous_desired_capacity < 0:
            raise ValueError("previous_desired_capacity must be nonnegative")
        requested_at = ensure_utc(requested_at, field_name="requested_at")
        ceiling = command.maximum_ceiling if effective_ceiling is None else effective_ceiling
        if not 1 <= ceiling <= command.maximum_ceiling:
            raise ValueError("effective_ceiling must be positive and within the command ceiling")
        action_id = uuid4()
        values = {
            "id": action_id,
            "correlation_id": command.correlation_id,
            "demo_run_id": command.demo_run_id,
            "prediction_id": command.prediction_id,
            "trigger_snapshot_id": command.trigger_snapshot_id,
            "scheduled_event_id": command.scheduled_event_id,
            "idempotency_key": command.idempotency_key,
            "requested_at": requested_at,
            "action_type": "set_desired_capacity",
            "execution_mode": execution_mode.value,
            "target_resource": command.target_resource,
            "previous_desired_capacity": previous_desired_capacity,
            "requested_desired_capacity": command.requested_desired_capacity,
            "max_instance_ceiling": ceiling,
            "status": ActionStatus.PLANNED.value,
            "reason_code": command.reason_code,
            "reasoning": command.reasoning,
            "signal_evidence": validate_evidence(command.signal_evidence),
        }
        statement = (
            insert(ScalingAction)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ScalingAction.idempotency_key])
            .returning(ScalingAction.id)
        )
        async with self._session_factory() as session, session.begin():
            inserted_id = (await session.execute(statement)).scalar_one_or_none()
            if inserted_id is not None:
                action = await session.get(ScalingAction, inserted_id)
                if action is None:
                    raise RuntimeError("claimed scaling action could not be reloaded")
                return ClaimResult(action=action, claimed=True)
            existing = await session.scalar(
                select(ScalingAction).where(
                    ScalingAction.idempotency_key == command.idempotency_key
                )
            )
            if existing is None:
                raise RuntimeError("conflicting scaling action could not be loaded")
            return ClaimResult(action=existing, claimed=False)

    async def has_unresolved(self, *, target_resource: str) -> bool:
        async with self._session_factory() as session:
            unresolved = await session.scalar(
                select(ScalingAction.id)
                .where(
                    ScalingAction.target_resource == target_resource,
                    ScalingAction.status.in_(
                        [ActionStatus.PLANNED.value, ActionStatus.UNKNOWN.value]
                    ),
                )
                .limit(1)
            )
        return unresolved is not None

    async def list_reconciliation_candidates(
        self,
        *,
        requested_before: datetime,
        limit: int = 50,
    ) -> list[ScalingAction]:
        requested_before = ensure_utc(requested_before, field_name="requested_before")
        limit = min(max(limit, 1), self._limits.max_rows)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ScalingAction)
                .where(
                    ScalingAction.requested_at <= requested_before,
                    or_(
                        ScalingAction.status == ActionStatus.PLANNED.value,
                        ScalingAction.status == ActionStatus.UNKNOWN.value,
                    ),
                )
                .order_by(ScalingAction.requested_at, ScalingAction.id)
                .limit(limit)
            )
        return list(rows.all())

    async def update_outcome(
        self,
        *,
        idempotency_key: str,
        status: ActionStatus,
        applied_desired_capacity: int | None,
        executed_at: datetime,
        provider_request_id: str | None = None,
        error_message: str | None = None,
        cooldown_until: datetime | None = None,
        reconciled_at: datetime | None = None,
    ) -> ScalingAction:
        executed_at = ensure_utc(executed_at, field_name="executed_at")
        if applied_desired_capacity is not None and applied_desired_capacity < 0:
            raise ValueError("applied_desired_capacity must be nonnegative")
        if cooldown_until is not None:
            cooldown_until = ensure_utc(cooldown_until, field_name="cooldown_until")
        if reconciled_at is not None:
            reconciled_at = ensure_utc(reconciled_at, field_name="reconciled_at")
        async with self._session_factory() as session, session.begin():
            action = await session.scalar(
                select(ScalingAction)
                .where(ScalingAction.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if action is None:
                raise LookupError(f"scaling action {idempotency_key!r} was not found")
            if (
                applied_desired_capacity is not None
                and applied_desired_capacity > action.max_instance_ceiling
            ):
                raise ValueError("applied capacity must not exceed the persisted ceiling")
            await session.execute(
                update(ScalingAction)
                .where(ScalingAction.id == action.id)
                .values(
                    status=status.value,
                    applied_desired_capacity=applied_desired_capacity,
                    executed_at=executed_at,
                    provider_request_id=provider_request_id,
                    error_message=error_message,
                    cooldown_until=cooldown_until,
                    reconciled_at=reconciled_at,
                )
            )
            await session.refresh(action)
            return action

    async def record_control_error(
        self,
        *,
        idempotency_key: str,
        error_message: str,
    ) -> ScalingAction:
        error_message = error_message[:2_000]
        async with self._session_factory() as session, session.begin():
            action = await session.scalar(
                select(ScalingAction)
                .where(ScalingAction.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if action is None:
                raise LookupError(f"scaling action {idempotency_key!r} was not found")
            existing = f"{action.error_message}; " if action.error_message else ""
            action.error_message = f"{existing}{error_message}"[:2_000]
            await session.flush()
            return action

    async def list_bounded(
        self,
        *,
        window: QueryWindow,
        demo_run_id: UUID | None = None,
        correlation_id: UUID | None = None,
        status: ActionStatus | None = None,
    ) -> Page[ScalingAction]:
        self._limits.validate_window(window)
        statement = (
            select(ScalingAction)
            .where(
                ScalingAction.requested_at >= window.start,
                ScalingAction.requested_at <= window.end,
            )
            .order_by(ScalingAction.requested_at.desc(), ScalingAction.id.desc())
            .limit(window.limit + 1)
        )
        if demo_run_id is not None:
            statement = statement.where(ScalingAction.demo_run_id == demo_run_id)
        if correlation_id is not None:
            statement = statement.where(ScalingAction.correlation_id == correlation_id)
        if status is not None:
            statement = statement.where(ScalingAction.status == status.value)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return Page(items=rows[: window.limit], has_more=len(rows) > window.limit)
