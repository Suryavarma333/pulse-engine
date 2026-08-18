from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from common.contracts import RecoveryPlan, ResponseCommand
from common.enums import ExecutionMode, PredictionMode
from db.base import Base
from db.models import ResponseRetry, ScalingAction, TrafficSnapshot
from db.repositories.response_retries import ResponseRetryRepository
from db.repositories.scaling_actions import ScalingActionRepository
from db.repositories.snapshots import SnapshotRepository

TEST_DATABASE_URL = os.getenv("PULSE_TEST_DATABASE_URL")


def _assert_dedicated_test_database(url: str) -> None:
    database_name = urlparse(url.replace("postgresql+psycopg", "postgresql", 1)).path.lower()
    if "test" not in database_name:
        raise RuntimeError(
            "PULSE_TEST_DATABASE_URL must name a dedicated database containing 'test'"
        )


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="PULSE_TEST_DATABASE_URL is not configured")
def test_postgres_concurrent_claim_is_exactly_once() -> None:
    assert TEST_DATABASE_URL is not None
    _assert_dedicated_test_database(TEST_DATABASE_URL)
    asyncio.run(_exercise_concurrent_claim(TEST_DATABASE_URL))


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="PULSE_TEST_DATABASE_URL is not configured")
def test_postgres_response_retries_and_bounded_retention_preserve_audit() -> None:
    assert TEST_DATABASE_URL is not None
    _assert_dedicated_test_database(TEST_DATABASE_URL)
    asyncio.run(_exercise_runtime_maintenance(TEST_DATABASE_URL))


async def _exercise_concurrent_claim(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = ScalingActionRepository(factory)
        correlation_id = uuid4()
        command = ResponseCommand(
            idempotency_key="integration:concurrent-claim",
            correlation_id=correlation_id,
            mode=PredictionMode.REALTIME,
            target_resource="pulse-test",
            requested_desired_capacity=3,
            maximum_ceiling=3,
            reason_code="integration_test",
            reasoning="Prove PostgreSQL unique claim behavior",
            recovery_plan=RecoveryPlan(
                low_threshold_rps=1,
                confirmation_count=2,
                cooldown_seconds=10,
                decrement_step=1,
                capacity_floor=1,
            ),
        )

        results = await asyncio.gather(
            repository.claim(
                command,
                previous_desired_capacity=1,
                execution_mode=ExecutionMode.DRY_RUN,
                requested_at=datetime.now(UTC),
            ),
            repository.claim(
                command,
                previous_desired_capacity=1,
                execution_mode=ExecutionMode.DRY_RUN,
                requested_at=datetime.now(UTC),
            ),
        )

        assert sorted(result.claimed for result in results) == [False, True]
        assert results[0].action.id == results[1].action.id
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(ScalingAction))
        assert count == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _exercise_runtime_maintenance(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        snapshots = SnapshotRepository(factory)
        retries = ResponseRetryRepository(factory)
        old = await snapshots.add(
            TrafficSnapshot(
                environment="test",
                observed_at=datetime.now(UTC) - timedelta(days=8),
                window_seconds=60,
                origin_request_rate_rps=10,
                baseline_request_rate_rps=10,
                request_count=10,
                concurrent_requests=1,
                request_rate_change_rps=0,
                request_acceleration_rps2=0,
                load_shedding_level=0,
                reactive_comparator_crossed=False,
                signal_details={},
            )
        )
        await snapshots.add(
            TrafficSnapshot(
                environment="test",
                observed_at=datetime.now(UTC),
                window_seconds=60,
                origin_request_rate_rps=10,
                baseline_request_rate_rps=10,
                request_count=10,
                concurrent_requests=1,
                request_rate_change_rps=0,
                request_acceleration_rps2=0,
                load_shedding_level=0,
                reactive_comparator_crossed=False,
                signal_details={},
            )
        )
        async with factory() as session, session.begin():
            session.add(
                ScalingAction(
                    correlation_id=uuid4(),
                    trigger_snapshot_id=old.id,
                    idempotency_key="maintenance:audit-preserved",
                    requested_at=datetime.now(UTC),
                    action_type="set_desired_capacity",
                    execution_mode="dry_run",
                    target_resource="pulse-test",
                    previous_desired_capacity=1,
                    requested_desired_capacity=2,
                    max_instance_ceiling=3,
                    status="dry_run",
                    reason_code="maintenance_test",
                    reasoning="Audit row must survive raw snapshot retention",
                    signal_evidence={},
                )
            )
        command = ResponseCommand(
            idempotency_key="maintenance:dispatch",
            correlation_id=uuid4(),
            mode=PredictionMode.REALTIME,
            target_resource="pulse-test",
            requested_desired_capacity=2,
            maximum_ceiling=3,
            reason_code="maintenance_test",
            reasoning="Persist retry separately from capacity claims",
            recovery_plan=RecoveryPlan(
                low_threshold_rps=1,
                confirmation_count=2,
                cooldown_seconds=10,
                decrement_step=1,
                capacity_floor=1,
            ),
        )
        now = datetime.now(UTC)
        first = await retries.schedule_dispatch(
            command,
            now=now,
            error_message="temporary",
        )
        duplicate = await retries.schedule_dispatch(
            command,
            now=now,
            error_message="temporary",
        )
        assert first.id == duplicate.id
        assert len(await retries.list_due(now=now, limit=10)) == 1
        await retries.mark_succeeded(first.id, now=now)

        deleted = await snapshots.delete_expired(
            cutoff=now - timedelta(days=7),
            limit=1,
        )
        async with factory() as session:
            action = await session.scalar(select(ScalingAction))
            retry_count = await session.scalar(select(func.count()).select_from(ResponseRetry))
            snapshot_count = await session.scalar(
                select(func.count()).select_from(TrafficSnapshot)
            )
        assert deleted == 1
        assert snapshot_count == 1
        assert action is not None and action.trigger_snapshot_id is None
        assert retry_count == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
