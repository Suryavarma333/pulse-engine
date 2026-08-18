from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from common.contracts import QueryWindow, RecoveryPlan, ResponseCommand
from common.enums import ExecutionMode, PredictionMode
from db.models import ScalingAction
from db.repositories.base import RepositoryLimits, bounded_limit
from db.repositories.scaling_actions import ScalingActionRepository

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Context:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ClaimSession:
    def __init__(self, action: ScalingAction, *, inserted: bool):
        self.action = action
        self.inserted = inserted
        self.statement = None

    def begin(self):
        return _Context(self)

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.action.id if self.inserted else None)

    async def get(self, model, identifier):
        assert model is ScalingAction
        assert identifier == self.action.id
        return self.action

    async def scalar(self, statement):
        return self.action


class _Factory:
    def __init__(self, sessions: list[_ClaimSession]):
        self.sessions = iter(sessions)

    def __call__(self):
        return _Context(next(self.sessions))


def command() -> ResponseCommand:
    return ResponseCommand(
        idempotency_key="realtime:test:snapshot-1",
        correlation_id=uuid4(),
        mode=PredictionMode.REALTIME,
        target_resource="pulse-demo",
        requested_desired_capacity=4,
        maximum_ceiling=3,
        reason_code="confirmed_acceleration",
        reasoning="Three consecutive accelerating samples",
        signal_evidence={"request_acceleration_rps2": 5.5},
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=3,
            cooldown_seconds=120,
            decrement_step=1,
            capacity_floor=1,
        ),
    )


def action_for(command: ResponseCommand) -> ScalingAction:
    return ScalingAction(
        id=uuid4(),
        correlation_id=command.correlation_id,
        idempotency_key=command.idempotency_key,
        requested_at=NOW,
        action_type="set_desired_capacity",
        execution_mode="dry_run",
        target_resource=command.target_resource,
        previous_desired_capacity=1,
        requested_desired_capacity=command.requested_desired_capacity,
        max_instance_ceiling=command.maximum_ceiling,
        status="planned",
        reason_code=command.reason_code,
        reasoning=command.reasoning,
        signal_evidence=command.signal_evidence,
    )


def test_transactional_claim_returns_existing_action_for_duplicate_key() -> None:
    response_command = command()
    action = action_for(response_command)
    winner_session = _ClaimSession(action, inserted=True)
    duplicate_session = _ClaimSession(action, inserted=False)
    repository = ScalingActionRepository(_Factory([winner_session, duplicate_session]))  # type: ignore[arg-type]

    winner = asyncio.run(
        repository.claim(
            response_command,
            previous_desired_capacity=1,
            execution_mode=ExecutionMode.DRY_RUN,
            requested_at=NOW,
        )
    )
    duplicate = asyncio.run(
        repository.claim(
            response_command,
            previous_desired_capacity=1,
            execution_mode=ExecutionMode.DRY_RUN,
            requested_at=NOW,
        )
    )

    assert winner.claimed is True
    assert duplicate.claimed is False
    assert duplicate.action.id == winner.action.id
    compiled = str(winner_session.statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in compiled


def test_repository_limits_reject_unbounded_queries() -> None:
    limits = RepositoryLimits(max_rows=100, max_window=timedelta(hours=1))
    with pytest.raises(ValueError, match="query limit"):
        limits.validate_window(QueryWindow(start=NOW, end=NOW + timedelta(minutes=1), limit=101))
    with pytest.raises(ValueError, match="query window"):
        limits.validate_window(QueryWindow(start=NOW, end=NOW + timedelta(hours=2), limit=10))


def test_bounded_limit_never_exceeds_repository_cap() -> None:
    assert bounded_limit(500, 200) == 200
    with pytest.raises(ValueError, match="positive"):
        bounded_limit(0, 200)
