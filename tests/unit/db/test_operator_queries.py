from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from common.contracts import QueryWindow
from db.models import (
    DemoRun,
    LoadSheddingEvent,
    ScalingAction,
    ScheduledEvent,
    SurgePrediction,
    SurgePredictionPoint,
    TrafficSnapshot,
)
from db.repositories.evaluation import EvaluationDataRepository
from db.repositories.operator import (
    InvalidCursor,
    OperatorQueryRepository,
    decode_cursor,
    encode_cursor,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
WINDOW = QueryWindow(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1), limit=1)


class Scalars:
    def __init__(self, rows) -> None:
        self.rows = rows

    def all(self):
        return self.rows


class Session:
    def __init__(self, queues=None, *, get_value=None) -> None:
        self.queues = list(queues or [])
        self.get_value = get_value
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def scalars(self, statement):
        self.statements.append(statement)
        return Scalars(self.queues.pop(0))

    async def scalar(self, statement):
        self.statements.append(statement)
        rows = self.queues.pop(0)
        return rows[0] if rows else None

    async def get(self, model, identifier):
        return self.get_value


class Factory:
    def __init__(self, session) -> None:
        self.session = session

    def __call__(self):
        return self.session


def rows():
    snapshot = TrafficSnapshot(id=2, environment="local", observed_at=NOW)
    prediction = SurgePrediction(
        id=uuid4(),
        environment="local",
        created_at=NOW,
        predicted_start_at=NOW,
        predicted_peak_at=NOW + timedelta(minutes=1),
    )
    action = ScalingAction(id=uuid4(), requested_at=NOW)
    shedding = LoadSheddingEvent(id=uuid4(), started_at=NOW)
    scheduled = ScheduledEvent(id=uuid4(), starts_at=NOW, ends_at=NOW + timedelta(hours=1))
    result = DemoRun(id=uuid4(), started_at=NOW, status="completed")
    return snapshot, prediction, action, shedding, scheduled, result


def test_cursor_round_trip_rejects_malformed_or_cross_endpoint_values() -> None:
    encoded = encode_cursor("snapshot", NOW, 42)
    timestamp, identifier = decode_cursor(encoded, expected_kind="snapshot")
    assert timestamp == NOW and identifier == "42"
    with pytest.raises(InvalidCursor, match="different endpoint"):
        decode_cursor(encoded, expected_kind="prediction")
    with pytest.raises(InvalidCursor, match="malformed"):
        decode_cursor("not-base64", expected_kind="snapshot")


def test_every_operator_list_applies_filters_and_produces_next_cursor() -> None:
    snapshot, prediction, action, shedding, scheduled, result = rows()

    async def scenario():
        pages = []
        for method_name, item, kwargs in (
            ("snapshots", snapshot, {"environment": "local", "demo_run_id": uuid4()}),
            (
                "predictions",
                prediction,
                {
                    "environment": "local",
                    "demo_run_id": uuid4(),
                    "mode": "realtime",
                    "status": "active",
                },
            ),
            (
                "scaling_actions",
                action,
                {
                    "status": "dry_run",
                    "execution_mode": "dry_run",
                    "correlation_id": uuid4(),
                    "prediction_id": uuid4(),
                    "demo_run_id": uuid4(),
                },
            ),
            (
                "shedding_events",
                shedding,
                {
                    "environment": "local",
                    "correlation_id": uuid4(),
                    "prediction_id": uuid4(),
                    "demo_run_id": uuid4(),
                    "active_only": True,
                },
            ),
            ("scheduled_events", scheduled, {"status": "active"}),
            (
                "result_runs",
                result,
                {
                    "environment": "local",
                    "scenario": "sudden",
                    "execution_mode": "dry_run",
                },
            ),
        ):
            session = Session([[item, item]])
            repository = OperatorQueryRepository(Factory(session))
            page = await getattr(repository, method_name)(window=WINDOW, **kwargs)
            assert page.items == (item,)
            assert page.next_cursor
            pages.append(page)
        return pages

    assert len(asyncio.run(scenario())) == 6


def test_operator_cursor_path_overlay_status_and_run_detail() -> None:
    snapshot, prediction, action, shedding, scheduled, result = rows()
    point = SurgePredictionPoint(
        prediction_id=prediction.id,
        point_at=NOW,
        predicted_rps=20,
        predicted_capacity=1,
    )

    async def scenario():
        cursor = encode_cursor("snapshot", NOW + timedelta(seconds=1), 3)
        cursor_session = Session([[snapshot]])
        cursor_repo = OperatorQueryRepository(Factory(cursor_session))
        page = await cursor_repo.snapshots(
            environment="local", window=WINDOW, cursor=cursor
        )

        overlay_session = Session(
            [[point], [action], [shedding], [snapshot]], get_value=prediction
        )
        overlay = await OperatorQueryRepository(Factory(overlay_session)).prediction_overlay(
            prediction.id, actual_window_seconds=60
        )

        status_session = Session(
            [[snapshot], [prediction], [action], [shedding], [scheduled]]
        )
        status = await OperatorQueryRepository(Factory(status_session)).status_rows(
            environment="local"
        )

        run_session = Session(get_value=result)
        detail = await OperatorQueryRepository(Factory(run_session)).run_detail(result.id)
        return page, overlay, status, detail

    page, overlay, status, detail = asyncio.run(scenario())
    assert page.items[0] is snapshot
    assert overlay.points == (point,)
    assert overlay.actual_snapshots == (snapshot,)
    assert status["scheduled_event"] is scheduled
    assert detail is result


def test_overlay_validation_missing_prediction_and_evaluation_bundle_loading() -> None:
    snapshot, prediction, action, shedding, _, result = rows()

    async def scenario():
        missing = await OperatorQueryRepository(
            Factory(Session(get_value=None))
        ).prediction_overlay(uuid4(), actual_window_seconds=60)
        with pytest.raises(ValueError, match="out of bounds"):
            await OperatorQueryRepository(
                Factory(Session(get_value=prediction))
            ).prediction_overlay(prediction.id, actual_window_seconds=90_000)
        evidence = await EvaluationDataRepository(
            Factory(Session([[snapshot], [prediction], [action], [shedding]]))
        ).load_for_run(result, limit=10_000)
        return missing, evidence

    missing, evidence = asyncio.run(scenario())
    assert missing is None
    assert evidence.snapshots == (snapshot,)
    assert evidence.predictions == (prediction,)
