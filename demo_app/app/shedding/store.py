from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import LoadSheddingEvent


@dataclass(frozen=True, slots=True)
class StoredSheddingState:
    event_id: UUID
    level: int
    started_at: datetime
    reason_code: str
    reasoning: str
    policy: dict[str, str]


@dataclass(frozen=True, slots=True)
class SheddingTransition:
    event_id: UUID
    correlation_id: UUID
    environment: str
    started_at: datetime
    from_level: int
    to_level: int
    changed_by: str
    reason_code: str
    reasoning: str
    signal_evidence: dict
    policy: dict[str, str]
    prediction_id: UUID | None = None
    trigger_snapshot_id: int | None = None


class LoadSheddingStore(Protocol):
    async def load_active(self, environment: str) -> StoredSheddingState | None: ...

    async def record_transition(self, transition: SheddingTransition) -> None: ...


class InMemoryLoadSheddingStore:
    def __init__(self) -> None:
        self.active: dict[str, StoredSheddingState] = {}
        self.transitions: list[SheddingTransition] = []

    async def load_active(self, environment: str) -> StoredSheddingState | None:
        return self.active.get(environment)

    async def record_transition(self, transition: SheddingTransition) -> None:
        self.transitions.append(transition)
        self.active[transition.environment] = StoredSheddingState(
            event_id=transition.event_id,
            level=transition.to_level,
            started_at=transition.started_at,
            reason_code=transition.reason_code,
            reasoning=transition.reasoning,
            policy=transition.policy,
        )


class SqlAlchemyLoadSheddingStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_active(self, environment: str) -> StoredSheddingState | None:
        async with self._session_factory() as session:
            event = await session.scalar(
                select(LoadSheddingEvent)
                .where(
                    LoadSheddingEvent.environment == environment,
                    LoadSheddingEvent.ended_at.is_(None),
                )
                .order_by(LoadSheddingEvent.started_at.desc())
                .limit(1)
            )
        if event is None:
            return None
        return StoredSheddingState(
            event_id=event.id,
            level=event.to_level,
            started_at=event.started_at,
            reason_code=event.reason_code,
            reasoning=event.reasoning,
            policy=event.policy_snapshot,
        )

    async def record_transition(self, transition: SheddingTransition) -> None:
        async with self._session_factory() as session, session.begin():
            active_event = await session.scalar(
                select(LoadSheddingEvent)
                .where(
                    LoadSheddingEvent.environment == transition.environment,
                    LoadSheddingEvent.ended_at.is_(None),
                )
                .with_for_update()
            )
            if active_event is not None:
                active_event.ended_at = transition.started_at

            session.add(
                LoadSheddingEvent(
                    id=transition.event_id,
                    correlation_id=transition.correlation_id,
                    prediction_id=transition.prediction_id,
                    trigger_snapshot_id=transition.trigger_snapshot_id,
                    environment=transition.environment,
                    started_at=transition.started_at,
                    from_level=transition.from_level,
                    to_level=transition.to_level,
                    changed_by=transition.changed_by,
                    reason_code=transition.reason_code,
                    reasoning=transition.reasoning,
                    signal_evidence=transition.signal_evidence,
                    policy_snapshot=transition.policy,
                    application_status="applied",
                )
            )
