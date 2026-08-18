from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent.app.services.feedback import FeedbackService
from common.contracts import DemoRunSpec, QueryWindow
from common.enums import DemoRunStatus, ExecutionMode, PredictionMode
from db.base import Base
from db.models import ScheduledEvent, SurgePrediction, SurgePredictionPoint, TrafficSnapshot
from db.repositories.demo_runs import DemoRunRepository
from db.repositories.evaluation import EvaluationDataRepository
from db.repositories.operator import OperatorQueryRepository
from db.repositories.predictions import PredictionRepository
from db.repositories.scheduled_events import ScheduledEventRepository
from db.repositories.snapshots import SnapshotRepository

TEST_DATABASE_URL = os.getenv("PULSE_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _assert_dedicated_test_database(url: str) -> None:
    database_name = urlparse(url.replace("postgresql+psycopg", "postgresql", 1)).path.lower()
    if "test" not in database_name:
        raise RuntimeError(
            "PULSE_TEST_DATABASE_URL must name a dedicated database containing 'test'"
        )


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="PULSE_TEST_DATABASE_URL is not configured")
def test_phase5_run_event_cursor_and_feedback_persistence() -> None:
    assert TEST_DATABASE_URL is not None
    _assert_dedicated_test_database(TEST_DATABASE_URL)
    asyncio.run(_exercise(TEST_DATABASE_URL))


async def _exercise(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        runs = DemoRunRepository(factory)
        snapshots = SnapshotRepository(factory)
        predictions = PredictionRepository(factory)
        events = ScheduledEventRepository(factory)
        operator = OperatorQueryRepository(factory)
        spec = DemoRunSpec(
            idempotency_key="phase5-integration-run",
            scenario_name="sudden-spike",
            mode=PredictionMode.REALTIME,
            environment="local",
            baseline_type="pulse",
            execution_mode=ExecutionMode.DRY_RUN,
            configuration={"rps_per_instance": 25, "minimum_desired_capacity": 1},
            thresholds={"onset_ratio": 1.2},
            started_at=NOW,
        )
        run, created = await runs.start(spec)
        duplicate, duplicate_created = await runs.start(spec)
        assert created and not duplicate_created and duplicate.id == run.id
        assert await runs.current_demo_run_id(environment="local") == run.id

        scheduled = await events.add(
            ScheduledEvent(
                id=uuid4(),
                name="Diwali integration",
                event_type="sale",
                starts_at=NOW + timedelta(minutes=30),
                ends_at=NOW + timedelta(hours=1),
                timezone="Asia/Kolkata",
                expected_multiplier=4,
                prewarm_lead_seconds=1800,
                peak_lead_seconds=300,
                scale_down_duration_seconds=1800,
                minimum_desired_capacity=1,
                peak_desired_capacity=3,
                max_instances_override=3,
                ramp_profile={},
                confidence=0.9,
                source="test",
                status="active",
            )
        )
        actionable = await events.list_actionable(now=NOW, lookahead_seconds=3600)
        assert [item.id for item in actionable] == [scheduled.id]

        rates = (10.0, 50.0, 100.0, 10.0)
        capacities = (1, 2, 3, 1)
        for index, (rate, capacity) in enumerate(zip(rates, capacities, strict=True)):
            await snapshots.add(
                TrafficSnapshot(
                    environment="local",
                    demo_run_id=run.id,
                    observed_at=NOW + timedelta(minutes=index),
                    window_seconds=60,
                    origin_request_rate_rps=rate,
                    baseline_request_rate_rps=10,
                    request_count=100,
                    concurrent_requests=10,
                    request_rate_change_rps=0,
                    request_acceleration_rps2=0,
                    checkout_p99_latency_ms=100,
                    checkout_success_rate=0.99,
                    error_rate=0.01,
                    asg_desired_capacity=capacity,
                    asg_in_service_capacity=capacity,
                    load_shedding_level=0 if index in {0, 3} else 1,
                    reactive_comparator_crossed=index == 2,
                    signal_details={},
                )
            )
        prediction_id = uuid4()
        await predictions.create_with_points(
            SurgePrediction(
                id=prediction_id,
                mode="realtime",
                demo_run_id=run.id,
                environment="local",
                correlation_id=uuid4(),
                model_name="rolling",
                model_version="v1",
                created_at=NOW + timedelta(seconds=30),
                basis_window_start=NOW,
                basis_window_end=NOW + timedelta(seconds=30),
                predicted_start_at=NOW + timedelta(seconds=30),
                predicted_peak_at=NOW + timedelta(minutes=2),
                baseline_rps=10,
                predicted_peak_rps=80,
                predicted_multiplier=8,
                recommended_capacity=3,
                confidence=0.9,
                trigger_type="acceleration",
                reasoning="integration",
                signal_evidence={},
                status="active",
                reactive_comparator_crossed_at=NOW + timedelta(seconds=90),
                formula_version="v1",
            ),
            [
                SurgePredictionPoint(
                    prediction_id=prediction_id,
                    point_at=NOW + timedelta(minutes=2),
                    predicted_rps=80,
                    predicted_capacity=3,
                )
            ],
        )
        window = QueryWindow(
            start=NOW - timedelta(minutes=1),
            end=NOW + timedelta(minutes=5),
            limit=2,
        )
        first_page = await operator.snapshots(environment="local", window=window)
        assert len(first_page.items) == 2 and first_page.next_cursor
        second_page = await operator.snapshots(
            environment="local", window=window, cursor=first_page.next_cursor
        )
        assert len(second_page.items) == 2
        assert {item.id for item in first_page.items}.isdisjoint(
            {item.id for item in second_page.items}
        )

        run = await runs.complete(
            run_id=run.id,
            status=DemoRunStatus.PENDING_EVALUATION,
            ended_at=NOW + timedelta(minutes=3),
            locust_summary={
                "request_count": 1000,
                "failure_count": 10,
                "checkout": {"attempts": 100, "successes": 99, "p99_latency_ms": 123},
            },
        )
        service = FeedbackService(
            runs=runs,
            data_source=EvaluationDataRepository(factory),
            predictions=predictions,
        )
        evaluated = await service.evaluate_run(run.id)
        assert evaluated.status == DemoRunStatus.COMPLETED.value
        assert evaluated.formula_version == "v1"
        assert evaluated.checkout_success_rate == pytest.approx(0.99)
        assert evaluated.detection_lead_seconds == pytest.approx(60)
        assert evaluated.result_summary["references"]["snapshot_ids"]
        stored_prediction, _ = await predictions.get_with_points(prediction_id)
        assert stored_prediction.status == "completed"
        assert stored_prediction.actual_peak_rps == 100
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
