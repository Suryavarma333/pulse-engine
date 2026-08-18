from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import validate_evidence
from common.time import ensure_utc
from db.models import ScheduledEvent
from db.repositories.base import RepositoryLimits, bounded_limit


class ScheduledEventRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def add(self, event: ScheduledEvent) -> ScheduledEvent:
        event.starts_at = ensure_utc(event.starts_at, field_name="starts_at")
        event.ends_at = ensure_utc(event.ends_at, field_name="ends_at")
        if event.ends_at <= event.starts_at:
            raise ValueError("event ends_at must be after starts_at")
        if event.peak_lead_seconds > event.prewarm_lead_seconds:
            raise ValueError("peak_lead_seconds must not exceed prewarm_lead_seconds")
        if event.minimum_desired_capacity > event.peak_desired_capacity:
            raise ValueError("minimum capacity must not exceed peak capacity")
        if (
            event.max_instances_override is not None
            and event.peak_desired_capacity > event.max_instances_override
        ):
            raise ValueError("peak capacity must not exceed the event ceiling")
        event.ramp_profile = validate_evidence(event.ramp_profile)
        async with self._session_factory() as session, session.begin():
            session.add(event)
            await session.flush()
        return event

    async def list_intersecting(
        self,
        *,
        start: datetime,
        end: datetime,
        statuses: tuple[str, ...] = ("active",),
        limit: int = 200,
        for_update: bool = False,
    ) -> list[ScheduledEvent]:
        start = ensure_utc(start, field_name="start")
        end = ensure_utc(end, field_name="end")
        if end <= start:
            raise ValueError("end must be after start")
        if end - start > self._limits.max_window:
            raise ValueError(f"query window must not exceed {self._limits.max_window}")
        if not statuses:
            raise ValueError("at least one event status is required")
        statement = (
            select(ScheduledEvent)
            .where(
                ScheduledEvent.status.in_(statuses),
                ScheduledEvent.starts_at <= end,
                ScheduledEvent.ends_at >= start,
            )
            .order_by(ScheduledEvent.starts_at.asc(), ScheduledEvent.id.asc())
            .limit(bounded_limit(limit, self._limits.max_rows))
        )
        if for_update:
            statement = statement.with_for_update(skip_locked=True)
        async with self._session_factory() as session, session.begin():
            return list((await session.scalars(statement)).all())

    async def list_actionable(
        self,
        *,
        now: datetime,
        lookahead_seconds: int,
        recovery_lookbehind_seconds: int = 86_400,
        statuses: tuple[str, ...] = ("active", "cancelled"),
        limit: int = 200,
    ) -> list[ScheduledEvent]:
        now = ensure_utc(now, field_name="now")
        if not 60 <= lookahead_seconds <= 604_800:
            raise ValueError("lookahead_seconds must be between 60 and 604800")
        if not 0 <= recovery_lookbehind_seconds <= 604_800:
            raise ValueError("recovery lookbehind is out of bounds")
        if not statuses:
            raise ValueError("at least one event status is required")
        horizon_end = now + timedelta(seconds=lookahead_seconds)
        history_start = now - timedelta(seconds=recovery_lookbehind_seconds)
        statement = (
            select(ScheduledEvent)
            .where(
                ScheduledEvent.status.in_(statuses),
                ScheduledEvent.starts_at <= horizon_end,
                ScheduledEvent.ends_at >= history_start,
            )
            .order_by(ScheduledEvent.starts_at.asc(), ScheduledEvent.id.asc())
            .limit(bounded_limit(limit, self._limits.max_rows))
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return [
            event
            for event in rows
            if event.starts_at - timedelta(seconds=event.prewarm_lead_seconds) <= horizon_end
        ]
