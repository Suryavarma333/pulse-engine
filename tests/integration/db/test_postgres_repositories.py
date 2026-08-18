from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from common.contracts import RecoveryPlan, ResponseCommand
from common.enums import ExecutionMode, PredictionMode
from db.base import Base
from db.models import ScalingAction
from db.repositories.scaling_actions import ScalingActionRepository

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
