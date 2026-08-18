from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlStateMachine
from agent.app.services.shedding_client import SheddingControlResult
from common.contracts import CapacityDecision, RecoveryPlan, ResponseCommand
from common.enums import (
    ActionStatus,
    ExecutionMode,
    PredictionMode,
    SheddingLevel,
)
from db.base import Base
from db.models import ScalingAction
from db.repositories.scaling_actions import ScalingActionRepository

TEST_DATABASE_URL = os.getenv("PULSE_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)


class CapacityAdapter:
    execution_mode = ExecutionMode.DRY_RUN
    target_resource = "pulse-test-asg"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, *, requested_capacity, effective_ceiling, observed_at, intent=None
    ):
        self.calls += 1
        await asyncio.sleep(0)
        return CapacityDecision(
            requested=requested_capacity,
            applied=effective_ceiling,
            ceiling=effective_ceiling,
            execution_mode=self.execution_mode,
            status=ActionStatus.CAPPED,
        )


class SheddingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def apply(self, *, command, level):
        self.calls += 1
        return SheddingControlResult(
            changed=True,
            level=level,
            event_id=str(uuid4()),
            endpoint_policies={"checkout": "normal"},
        )


def _assert_dedicated_test_database(url: str) -> None:
    database_name = urlparse(url.replace("postgresql+psycopg", "postgresql", 1)).path.lower()
    if "test" not in database_name:
        raise RuntimeError(
            "PULSE_TEST_DATABASE_URL must name a dedicated database containing 'test'"
        )


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="PULSE_TEST_DATABASE_URL is not configured")
def test_postgres_pipeline_claims_once_before_external_attempts() -> None:
    assert TEST_DATABASE_URL is not None
    _assert_dedicated_test_database(TEST_DATABASE_URL)
    asyncio.run(_exercise_pipeline_claim(TEST_DATABASE_URL))


async def _exercise_pipeline_claim(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = ScalingActionRepository(factory)
        capacity = CapacityAdapter()
        shedding = SheddingClient()
        pipeline = ResponsePipeline(
            repository=repository,
            capacity_adapter=capacity,
            shedding_client=shedding,
            state_machine=ControlStateMachine(),
            global_ceiling=3,
        )
        command = ResponseCommand(
            idempotency_key="integration:pipeline:once",
            correlation_id=uuid4(),
            mode=PredictionMode.REALTIME,
            target_resource="pulse-test-asg",
            requested_desired_capacity=8,
            maximum_ceiling=4,
            requested_shedding_level=SheddingLevel.DISABLE_RECOMMENDATIONS,
            reason_code="integration_test",
            reasoning="Prove planned-before-provider and concurrent idempotency",
            signal_evidence={"request_acceleration_rps2": 4.2},
            recovery_plan=RecoveryPlan(
                low_threshold_rps=1,
                confirmation_count=2,
                cooldown_seconds=10,
                decrement_step=1,
                capacity_floor=1,
            ),
        )
        results = await asyncio.gather(
            pipeline.execute(
                command,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
            pipeline.execute(
                command,
                current_capacity=1,
                current_shedding_level=SheddingLevel.NORMAL,
                requested_at=NOW,
            ),
        )
        assert capacity.calls == 1
        assert shedding.calls == 1
        assert sorted(result.duplicate for result in results) == [False, True]
        async with factory() as session:
            persisted = await session.scalar(select(ScalingAction))
        assert persisted is not None
        assert persisted.status == ActionStatus.CAPPED.value
        assert persisted.max_instance_ceiling == 3
        assert persisted.applied_desired_capacity == 3
        assert persisted.signal_evidence["control_state_intended"] == "protect"
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
