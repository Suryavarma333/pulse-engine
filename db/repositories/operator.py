from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import QueryWindow
from common.time import ensure_utc
from db.models import (
    DemoRun,
    LoadSheddingEvent,
    ScalingAction,
    ScheduledEvent,
    SurgePrediction,
    SurgePredictionPoint,
    TrafficSnapshot,
)
from db.repositories.base import RepositoryLimits


@dataclass(frozen=True, slots=True)
class CursorPage:
    items: tuple[Any, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PredictionOverlay:
    prediction: SurgePrediction
    points: tuple[SurgePredictionPoint, ...]
    actions: tuple[ScalingAction, ...]
    shedding_events: tuple[LoadSheddingEvent, ...]
    actual_snapshots: tuple[TrafficSnapshot, ...]


class InvalidCursor(ValueError):
    pass


class OperatorQueryRepository:
    """Indexed, range-capped and opaque-cursor reads for the operator API."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def snapshots(
        self,
        *,
        environment: str,
        window: QueryWindow,
        cursor: str | None = None,
        demo_run_id: UUID | None = None,
    ) -> CursorPage:
        statement = select(TrafficSnapshot).where(
            TrafficSnapshot.environment == environment,
            TrafficSnapshot.observed_at >= window.start,
            TrafficSnapshot.observed_at <= window.end,
        )
        if demo_run_id is not None:
            statement = statement.where(TrafficSnapshot.demo_run_id == demo_run_id)
        return await self._paged(
            statement,
            kind="snapshot",
            time_column=TrafficSnapshot.observed_at,
            id_column=TrafficSnapshot.id,
            window=window,
            cursor=cursor,
            id_parser=int,
        )

    async def predictions(
        self,
        *,
        window: QueryWindow,
        cursor: str | None = None,
        environment: str | None = None,
        demo_run_id: UUID | None = None,
        mode: str | None = None,
        status: str | None = None,
    ) -> CursorPage:
        statement = select(SurgePrediction).where(
            SurgePrediction.created_at >= window.start,
            SurgePrediction.created_at <= window.end,
        )
        if environment is not None:
            statement = statement.where(SurgePrediction.environment == environment)
        if demo_run_id is not None:
            statement = statement.where(SurgePrediction.demo_run_id == demo_run_id)
        if mode is not None:
            statement = statement.where(SurgePrediction.mode == mode)
        if status is not None:
            statement = statement.where(SurgePrediction.status == status)
        return await self._paged(
            statement,
            kind="prediction",
            time_column=SurgePrediction.created_at,
            id_column=SurgePrediction.id,
            window=window,
            cursor=cursor,
            id_parser=UUID,
        )

    async def scaling_actions(
        self,
        *,
        window: QueryWindow,
        cursor: str | None = None,
        status: str | None = None,
        execution_mode: str | None = None,
        correlation_id: UUID | None = None,
        prediction_id: UUID | None = None,
        demo_run_id: UUID | None = None,
    ) -> CursorPage:
        statement = select(ScalingAction).where(
            ScalingAction.requested_at >= window.start,
            ScalingAction.requested_at <= window.end,
        )
        for column, value in (
            (ScalingAction.status, status),
            (ScalingAction.execution_mode, execution_mode),
            (ScalingAction.correlation_id, correlation_id),
            (ScalingAction.prediction_id, prediction_id),
            (ScalingAction.demo_run_id, demo_run_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        return await self._paged(
            statement,
            kind="action",
            time_column=ScalingAction.requested_at,
            id_column=ScalingAction.id,
            window=window,
            cursor=cursor,
            id_parser=UUID,
        )

    async def shedding_events(
        self,
        *,
        window: QueryWindow,
        cursor: str | None = None,
        environment: str | None = None,
        correlation_id: UUID | None = None,
        prediction_id: UUID | None = None,
        demo_run_id: UUID | None = None,
        active_only: bool = False,
    ) -> CursorPage:
        statement = select(LoadSheddingEvent).where(
            LoadSheddingEvent.started_at >= window.start,
            LoadSheddingEvent.started_at <= window.end,
        )
        for column, value in (
            (LoadSheddingEvent.environment, environment),
            (LoadSheddingEvent.correlation_id, correlation_id),
            (LoadSheddingEvent.prediction_id, prediction_id),
            (LoadSheddingEvent.demo_run_id, demo_run_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if active_only:
            statement = statement.where(LoadSheddingEvent.ended_at.is_(None))
        return await self._paged(
            statement,
            kind="shedding",
            time_column=LoadSheddingEvent.started_at,
            id_column=LoadSheddingEvent.id,
            window=window,
            cursor=cursor,
            id_parser=UUID,
        )

    async def scheduled_events(
        self,
        *,
        window: QueryWindow,
        cursor: str | None = None,
        status: str | None = None,
    ) -> CursorPage:
        statement = select(ScheduledEvent).where(
            ScheduledEvent.starts_at <= window.end,
            ScheduledEvent.ends_at >= window.start,
        )
        if status is not None:
            statement = statement.where(ScheduledEvent.status == status)
        return await self._paged(
            statement,
            kind="scheduled",
            time_column=ScheduledEvent.starts_at,
            id_column=ScheduledEvent.id,
            window=window,
            cursor=cursor,
            id_parser=UUID,
        )

    async def result_runs(
        self,
        *,
        window: QueryWindow,
        cursor: str | None = None,
        environment: str | None = None,
        scenario: str | None = None,
        execution_mode: str | None = None,
    ) -> CursorPage:
        statement = select(DemoRun).where(
            DemoRun.started_at >= window.start,
            DemoRun.started_at <= window.end,
            DemoRun.status == "completed",
        )
        for column, value in (
            (DemoRun.environment, environment),
            (DemoRun.scenario_name, scenario),
            (DemoRun.execution_mode, execution_mode),
        ):
            if value is not None:
                statement = statement.where(column == value)
        return await self._paged(
            statement,
            kind="result",
            time_column=DemoRun.started_at,
            id_column=DemoRun.id,
            window=window,
            cursor=cursor,
            id_parser=UUID,
        )

    async def run_detail(self, run_id: UUID) -> DemoRun | None:
        async with self._session_factory() as session:
            return await session.get(DemoRun, run_id)

    async def prediction_overlay(
        self,
        prediction_id: UUID,
        *,
        actual_window_seconds: int,
    ) -> PredictionOverlay | None:
        if not 0 <= actual_window_seconds <= 86_400:
            raise ValueError("actual overlay window is out of bounds")
        async with self._session_factory() as session:
            prediction = await session.get(SurgePrediction, prediction_id)
            if prediction is None:
                return None
            points = tuple(
                (
                    await session.scalars(
                        select(SurgePredictionPoint)
                        .where(SurgePredictionPoint.prediction_id == prediction_id)
                        .order_by(SurgePredictionPoint.point_at)
                        .limit(self._limits.max_rows)
                    )
                ).all()
            )
            actions = tuple(
                (
                    await session.scalars(
                        select(ScalingAction)
                        .where(ScalingAction.prediction_id == prediction_id)
                        .order_by(ScalingAction.requested_at)
                        .limit(self._limits.max_rows)
                    )
                ).all()
            )
            shedding = tuple(
                (
                    await session.scalars(
                        select(LoadSheddingEvent)
                        .where(LoadSheddingEvent.prediction_id == prediction_id)
                        .order_by(LoadSheddingEvent.started_at)
                        .limit(self._limits.max_rows)
                    )
                ).all()
            )
            actual_end = prediction.predicted_peak_at + timedelta(
                seconds=actual_window_seconds
            )
            actual = tuple(
                (
                    await session.scalars(
                        select(TrafficSnapshot)
                        .where(
                            TrafficSnapshot.environment == prediction.environment,
                            TrafficSnapshot.observed_at >= prediction.predicted_start_at,
                            TrafficSnapshot.observed_at <= actual_end,
                        )
                        .order_by(TrafficSnapshot.observed_at)
                        .limit(self._limits.max_rows)
                    )
                ).all()
            )
        return PredictionOverlay(prediction, points, actions, shedding, actual)

    async def status_rows(self, *, environment: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            snapshot = await session.scalar(
                select(TrafficSnapshot)
                .where(TrafficSnapshot.environment == environment)
                .order_by(TrafficSnapshot.observed_at.desc(), TrafficSnapshot.id.desc())
                .limit(1)
            )
            prediction = await session.scalar(
                select(SurgePrediction)
                .where(SurgePrediction.environment == environment)
                .order_by(SurgePrediction.created_at.desc(), SurgePrediction.id.desc())
                .limit(1)
            )
            action = await session.scalar(
                select(ScalingAction)
                .order_by(ScalingAction.requested_at.desc(), ScalingAction.id.desc())
                .limit(1)
            )
            shedding = await session.scalar(
                select(LoadSheddingEvent)
                .where(
                    LoadSheddingEvent.environment == environment,
                    LoadSheddingEvent.ended_at.is_(None),
                )
                .order_by(LoadSheddingEvent.started_at.desc())
                .limit(1)
            )
            event = await session.scalar(
                select(ScheduledEvent)
                .where(ScheduledEvent.status == "active")
                .order_by(ScheduledEvent.starts_at)
                .limit(1)
            )
        return {
            "snapshot": snapshot,
            "prediction": prediction,
            "action": action,
            "shedding": shedding,
            "scheduled_event": event,
        }

    async def _paged(
        self,
        statement: Any,
        *,
        kind: str,
        time_column: Any,
        id_column: Any,
        window: QueryWindow,
        cursor: str | None,
        id_parser: Any,
    ) -> CursorPage:
        self._limits.validate_window(window)
        if cursor:
            cursor_time, raw_id = decode_cursor(cursor, expected_kind=kind)
            try:
                cursor_id = id_parser(raw_id)
            except (TypeError, ValueError) as exc:
                raise InvalidCursor("cursor identifier is invalid") from exc
            statement = statement.where(
                or_(
                    time_column < cursor_time,
                    and_(time_column == cursor_time, id_column < cursor_id),
                )
            )
        statement = statement.order_by(time_column.desc(), id_column.desc()).limit(
            window.limit + 1
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        items = rows[: window.limit]
        next_cursor = None
        if len(rows) > window.limit and items:
            last = items[-1]
            next_cursor = encode_cursor(
                kind,
                getattr(last, time_column.key),
                getattr(last, id_column.key),
            )
        return CursorPage(tuple(items), next_cursor)


def encode_cursor(kind: str, timestamp: datetime, identifier: Any) -> str:
    payload = json.dumps(
        {
            "k": kind,
            "t": ensure_utc(timestamp, field_name="cursor timestamp").isoformat(),
            "i": str(identifier),
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(value: str, *, expected_kind: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if payload.get("k") != expected_kind:
            raise InvalidCursor("cursor belongs to a different endpoint")
        timestamp = ensure_utc(datetime.fromisoformat(payload["t"]), field_name="cursor time")
        identifier = str(payload["i"])
    except InvalidCursor:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursor("cursor is malformed") from exc
    return timestamp, identifier


__all__ = [
    "CursorPage",
    "InvalidCursor",
    "OperatorQueryRepository",
    "PredictionOverlay",
    "decode_cursor",
    "encode_cursor",
]
