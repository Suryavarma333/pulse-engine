from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import QueryWindow
from db.models import LoadSheddingEvent
from db.repositories.base import Page, RepositoryLimits


class LoadSheddingEventRepository:
    """Read-only audit access; transition writes remain owned by the demo controller store."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def list_bounded(
        self,
        *,
        window: QueryWindow,
        environment: str | None = None,
        demo_run_id: UUID | None = None,
        correlation_id: UUID | None = None,
        active_only: bool = False,
    ) -> Page[LoadSheddingEvent]:
        self._limits.validate_window(window)
        statement = (
            select(LoadSheddingEvent)
            .where(
                LoadSheddingEvent.started_at >= window.start,
                LoadSheddingEvent.started_at <= window.end,
            )
            .order_by(LoadSheddingEvent.started_at.desc(), LoadSheddingEvent.id.desc())
            .limit(window.limit + 1)
        )
        if environment is not None:
            statement = statement.where(LoadSheddingEvent.environment == environment)
        if demo_run_id is not None:
            statement = statement.where(LoadSheddingEvent.demo_run_id == demo_run_id)
        if correlation_id is not None:
            statement = statement.where(LoadSheddingEvent.correlation_id == correlation_id)
        if active_only:
            statement = statement.where(LoadSheddingEvent.ended_at.is_(None))
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return Page(items=rows[: window.limit], has_more=len(rows) > window.limit)
