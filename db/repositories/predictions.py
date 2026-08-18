from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import QueryWindow, validate_evidence
from common.time import ensure_utc
from db.models import SurgePrediction, SurgePredictionPoint
from db.repositories.base import Page, RepositoryLimits


class PredictionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def create_with_points(
        self,
        prediction: SurgePrediction,
        points: Iterable[SurgePredictionPoint],
    ) -> SurgePrediction:
        if prediction.id is None:
            prediction.id = uuid4()
        materialized_points = list(points)
        for point in materialized_points:
            if point.prediction_id is None:
                point.prediction_id = prediction.id
            elif point.prediction_id != prediction.id:
                raise ValueError(
                    "every prediction point must reference the prediction being created"
                )
        if len(materialized_points) > 10_000:
            raise ValueError("a prediction may contain at most 10000 points")
        prediction.basis_window_start = ensure_utc(
            prediction.basis_window_start, field_name="basis_window_start"
        )
        prediction.basis_window_end = ensure_utc(
            prediction.basis_window_end, field_name="basis_window_end"
        )
        prediction.predicted_start_at = ensure_utc(
            prediction.predicted_start_at, field_name="predicted_start_at"
        )
        prediction.predicted_peak_at = ensure_utc(
            prediction.predicted_peak_at, field_name="predicted_peak_at"
        )
        if prediction.predicted_end_at is not None:
            prediction.predicted_end_at = ensure_utc(
                prediction.predicted_end_at, field_name="predicted_end_at"
            )
        if prediction.basis_window_end <= prediction.basis_window_start:
            raise ValueError("basis_window_end must be after basis_window_start")
        if prediction.predicted_peak_at < prediction.predicted_start_at:
            raise ValueError("predicted_peak_at must not precede predicted_start_at")
        if (
            prediction.predicted_end_at is not None
            and prediction.predicted_end_at < prediction.predicted_peak_at
        ):
            raise ValueError("predicted_end_at must not precede predicted_peak_at")
        prediction.signal_evidence = validate_evidence(prediction.signal_evidence)
        for point in materialized_points:
            point.point_at = ensure_utc(point.point_at, field_name="point_at")
        async with self._session_factory() as session, session.begin():
            session.add(prediction)
            session.add_all(materialized_points)
            await session.flush()
        return prediction

    async def list_bounded(
        self,
        *,
        window: QueryWindow,
        environment: str | None = None,
        demo_run_id: UUID | None = None,
        mode: str | None = None,
        status: str | None = None,
    ) -> Page[SurgePrediction]:
        self._limits.validate_window(window)
        statement = (
            select(SurgePrediction)
            .where(
                SurgePrediction.created_at >= window.start,
                SurgePrediction.created_at <= window.end,
            )
            .order_by(SurgePrediction.created_at.desc(), SurgePrediction.id.desc())
            .limit(window.limit + 1)
        )
        if environment is not None:
            statement = statement.where(SurgePrediction.environment == environment)
        if demo_run_id is not None:
            statement = statement.where(SurgePrediction.demo_run_id == demo_run_id)
        if mode is not None:
            statement = statement.where(SurgePrediction.mode == mode)
        if status is not None:
            statement = statement.where(SurgePrediction.status == status)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return Page(items=rows[: window.limit], has_more=len(rows) > window.limit)

    async def get_with_points(
        self, prediction_id: UUID, *, point_limit: int = 1_000
    ) -> tuple[SurgePrediction | None, list[SurgePredictionPoint]]:
        if point_limit < 1:
            raise ValueError("point_limit must be positive")
        point_limit = min(point_limit, self._limits.max_rows)
        async with self._session_factory() as session:
            prediction = await session.get(SurgePrediction, prediction_id)
            if prediction is None:
                return None, []
            statement = (
                select(SurgePredictionPoint)
                .where(SurgePredictionPoint.prediction_id == prediction_id)
                .order_by(SurgePredictionPoint.point_at.asc())
                .limit(point_limit)
            )
            points = list((await session.scalars(statement)).all())
        return prediction, points

    async def latest_for_scheduled_event(
        self, scheduled_event_id: UUID
    ) -> SurgePrediction | None:
        statement = (
            select(SurgePrediction)
            .where(
                SurgePrediction.scheduled_event_id == scheduled_event_id,
                SurgePrediction.mode == "scheduled",
                SurgePrediction.status.in_(["active", "evaluating", "completed"]),
            )
            .order_by(SurgePrediction.created_at.desc(), SurgePrediction.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def record_reactive_comparator(
        self,
        *,
        environment: str,
        demo_run_id: UUID | None,
        crossed_at: datetime,
    ) -> SurgePrediction | None:
        crossed_at = ensure_utc(crossed_at, field_name="crossed_at")
        statement = (
            select(SurgePrediction)
            .where(
                SurgePrediction.environment == environment,
                SurgePrediction.mode == "realtime",
                SurgePrediction.status.in_(["active", "evaluating"]),
            )
            .order_by(SurgePrediction.created_at.desc(), SurgePrediction.id.desc())
            .limit(1)
            .with_for_update()
        )
        if demo_run_id is None:
            statement = statement.where(SurgePrediction.demo_run_id.is_(None))
        else:
            statement = statement.where(SurgePrediction.demo_run_id == demo_run_id)
        async with self._session_factory() as session, session.begin():
            prediction = await session.scalar(statement)
            if prediction is None:
                return None
            if prediction.reactive_comparator_crossed_at is None:
                prediction.reactive_comparator_crossed_at = crossed_at
                prediction.signal_evidence = validate_evidence(
                    {
                        **prediction.signal_evidence,
                        "reactive_comparator_crossed_at": crossed_at.isoformat(),
                    }
                )
                await session.flush()
            return prediction

    async def update_evaluation(
        self,
        *,
        prediction_id: UUID,
        actual_start_at: datetime | None,
        actual_peak_at: datetime | None,
        actual_peak_rps: float | None,
        detection_lead_seconds: float | None,
        prediction_error_pct: float | None,
        overprovisioned_instance_minutes: float | None,
        underprovisioned_seconds: float | None,
    ) -> SurgePrediction:
        if actual_start_at is not None:
            actual_start_at = ensure_utc(actual_start_at, field_name="actual_start_at")
        if actual_peak_at is not None:
            actual_peak_at = ensure_utc(actual_peak_at, field_name="actual_peak_at")
        if actual_peak_rps is not None and actual_peak_rps < 0:
            raise ValueError("actual_peak_rps must be nonnegative")
        async with self._session_factory() as session, session.begin():
            prediction = await session.scalar(
                select(SurgePrediction)
                .where(SurgePrediction.id == prediction_id)
                .with_for_update()
            )
            if prediction is None:
                raise LookupError(f"prediction {prediction_id} was not found")
            prediction.actual_start_at = actual_start_at
            prediction.actual_peak_at = actual_peak_at
            prediction.actual_peak_rps = actual_peak_rps
            prediction.detection_lead_seconds = (
                None if detection_lead_seconds is None else round(detection_lead_seconds)
            )
            prediction.prediction_error_pct = prediction_error_pct
            prediction.overprovisioned_instance_minutes = (
                overprovisioned_instance_minutes
            )
            prediction.underprovisioned_seconds = (
                None if underprovisioned_seconds is None else round(underprovisioned_seconds)
            )
            prediction.status = "completed"
            prediction.evaluated_at = datetime.now(UTC)
            await session.flush()
            return prediction
