from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import QueryWindow, validate_evidence
from common.time import ensure_utc
from db.models import TrafficSnapshot
from db.repositories.base import Page, RepositoryLimits


class SnapshotRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def add(self, snapshot: TrafficSnapshot) -> TrafficSnapshot:
        snapshot.observed_at = ensure_utc(snapshot.observed_at, field_name="observed_at")
        snapshot.signal_details = validate_evidence(snapshot.signal_details)
        async with self._session_factory() as session, session.begin():
            session.add(snapshot)
            await session.flush()
        return snapshot

    async def list_bounded(
        self,
        *,
        environment: str,
        window: QueryWindow,
        demo_run_id: UUID | None = None,
    ) -> Page[TrafficSnapshot]:
        self._limits.validate_window(window)
        statement = (
            select(TrafficSnapshot)
            .where(
                TrafficSnapshot.environment == environment,
                TrafficSnapshot.observed_at >= window.start,
                TrafficSnapshot.observed_at <= window.end,
            )
            .order_by(TrafficSnapshot.observed_at.asc(), TrafficSnapshot.id.asc())
            .limit(window.limit + 1)
        )
        if demo_run_id is not None:
            statement = statement.where(TrafficSnapshot.demo_run_id == demo_run_id)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return Page(items=rows[: window.limit], has_more=len(rows) > window.limit)

    async def latest(self, environment: str) -> TrafficSnapshot | None:
        statement = (
            select(TrafficSnapshot)
            .where(TrafficSnapshot.environment == environment)
            .order_by(TrafficSnapshot.observed_at.desc(), TrafficSnapshot.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)
