from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import (
    LoadSheddingEvent,
    ScalingAction,
    SurgePrediction,
    TrafficSnapshot,
)


@dataclass(frozen=True, slots=True)
class EvaluationRows:
    snapshots: tuple[object, ...]
    predictions: tuple[object, ...]
    actions: tuple[object, ...]
    shedding_events: tuple[object, ...]


class EvaluationDataRepository:
    """Loads one bounded, run-correlated evidence bundle for formula evaluation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_for_run(self, run: Any, *, limit: int) -> EvaluationRows:
        limit = min(max(limit, 1), 1_000)
        run_id = run.id
        async with self._session_factory() as session:
            snapshots = list(
                (
                    await session.scalars(
                        select(TrafficSnapshot)
                        .where(TrafficSnapshot.demo_run_id == run_id)
                        .order_by(TrafficSnapshot.observed_at, TrafficSnapshot.id)
                        .limit(limit)
                    )
                ).all()
            )
            predictions = list(
                (
                    await session.scalars(
                        select(SurgePrediction)
                        .where(SurgePrediction.demo_run_id == run_id)
                        .order_by(SurgePrediction.created_at, SurgePrediction.id)
                        .limit(limit)
                    )
                ).all()
            )
            actions = list(
                (
                    await session.scalars(
                        select(ScalingAction)
                        .where(ScalingAction.demo_run_id == run_id)
                        .order_by(ScalingAction.requested_at, ScalingAction.id)
                        .limit(limit)
                    )
                ).all()
            )
            shedding = list(
                (
                    await session.scalars(
                        select(LoadSheddingEvent)
                        .where(LoadSheddingEvent.demo_run_id == run_id)
                        .order_by(LoadSheddingEvent.started_at, LoadSheddingEvent.id)
                        .limit(limit)
                    )
                ).all()
            )
        return EvaluationRows(
            snapshots=tuple(snapshots),
            predictions=tuple(predictions),
            actions=tuple(actions),
            shedding_events=tuple(shedding),
        )


__all__ = ["EvaluationDataRepository", "EvaluationRows"]
