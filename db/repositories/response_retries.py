from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import ResponseCommand
from common.enums import (
    ResponseRetryKind,
    ResponseRetryStatus,
    SheddingLevel,
)
from common.time import ensure_utc
from db.models import ResponseRetry

_MAX_COMMAND_BYTES = 32_768


class ResponseRetryRepository:
    """Persists bounded response retries independently of scaling-action status."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def schedule_dispatch(
        self,
        command: ResponseCommand,
        *,
        now: datetime,
        error_message: str,
    ) -> ResponseRetry:
        return await self.schedule(
            command,
            kind=ResponseRetryKind.DISPATCH,
            desired_shedding_level=None,
            now=now,
            error_message=error_message,
        )

    async def schedule_shedding(
        self,
        command: ResponseCommand,
        *,
        level: SheddingLevel,
        now: datetime,
        error_message: str,
    ) -> ResponseRetry:
        return await self.schedule(
            command,
            kind=ResponseRetryKind.SHEDDING,
            desired_shedding_level=level,
            now=now,
            error_message=error_message,
        )

    async def schedule(
        self,
        command: ResponseCommand,
        *,
        kind: ResponseRetryKind,
        desired_shedding_level: SheddingLevel | None,
        now: datetime,
        error_message: str,
    ) -> ResponseRetry:
        now = ensure_utc(now, field_name="now")
        payload = command.model_dump(mode="json")
        if len(json.dumps(payload, separators=(",", ":")).encode()) > _MAX_COMMAND_BYTES:
            raise ValueError("response retry command payload is too large")
        retry_key = f"{kind.value}:{command.idempotency_key}"
        values = {
            "id": uuid4(),
            "retry_key": retry_key,
            "retry_kind": kind.value,
            "correlation_id": command.correlation_id,
            "demo_run_id": command.demo_run_id,
            "prediction_id": command.prediction_id,
            "command_payload": payload,
            "desired_shedding_level": (
                None if desired_shedding_level is None else int(desired_shedding_level)
            ),
            "status": ResponseRetryStatus.PENDING.value,
            "attempts": 0,
            "next_attempt_at": now,
            "last_error": error_message[:2_000],
        }
        statement = (
            insert(ResponseRetry)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[ResponseRetry.retry_key],
                set_={
                    "command_payload": payload,
                    "desired_shedding_level": values["desired_shedding_level"],
                    "status": ResponseRetryStatus.PENDING.value,
                    "next_attempt_at": now,
                    "last_error": values["last_error"],
                },
                where=ResponseRetry.status != ResponseRetryStatus.SUCCEEDED.value,
            )
            .returning(ResponseRetry.id)
        )
        async with self._session_factory() as session, session.begin():
            retry_id = (await session.execute(statement)).scalar_one_or_none()
            retry = (
                await session.get(ResponseRetry, retry_id)
                if retry_id is not None
                else await session.scalar(
                    select(ResponseRetry).where(ResponseRetry.retry_key == retry_key)
                )
            )
            if retry is None:
                raise RuntimeError("response retry could not be reloaded")
            return retry

    async def list_due(self, *, now: datetime, limit: int) -> list[ResponseRetry]:
        now = ensure_utc(now, field_name="now")
        if not 1 <= limit <= 1_000:
            raise ValueError("retry limit must be between 1 and 1000")
        statement = (
            select(ResponseRetry)
            .where(
                ResponseRetry.status == ResponseRetryStatus.PENDING.value,
                ResponseRetry.next_attempt_at <= now,
            )
            .order_by(ResponseRetry.next_attempt_at, ResponseRetry.id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(statement)).all())

    async def mark_succeeded(self, retry_id: object, *, now: datetime) -> ResponseRetry:
        return await self._finish(
            retry_id,
            status=ResponseRetryStatus.SUCCEEDED,
            now=now,
            error_message=None,
        )

    async def mark_failed(
        self, retry_id: object, *, now: datetime, error_message: str
    ) -> ResponseRetry:
        return await self._finish(
            retry_id,
            status=ResponseRetryStatus.FAILED,
            now=now,
            error_message=error_message,
        )

    async def reschedule(
        self,
        retry_id: object,
        *,
        next_attempt_at: datetime,
        error_message: str,
    ) -> ResponseRetry:
        next_attempt_at = ensure_utc(next_attempt_at, field_name="next_attempt_at")
        async with self._session_factory() as session, session.begin():
            retry = await session.get(ResponseRetry, retry_id, with_for_update=True)
            if retry is None:
                raise LookupError("response retry was not found")
            retry.attempts += 1
            retry.next_attempt_at = next_attempt_at
            retry.last_error = error_message[:2_000]
            await session.flush()
            return retry

    async def has_pending(self) -> bool:
        async with self._session_factory() as session:
            retry_id = await session.scalar(
                select(ResponseRetry.id)
                .where(ResponseRetry.status == ResponseRetryStatus.PENDING.value)
                .limit(1)
            )
        return retry_id is not None

    async def _finish(
        self,
        retry_id: object,
        *,
        status: ResponseRetryStatus,
        now: datetime,
        error_message: str | None,
    ) -> ResponseRetry:
        now = ensure_utc(now, field_name="now")
        async with self._session_factory() as session, session.begin():
            retry = await session.get(ResponseRetry, retry_id, with_for_update=True)
            if retry is None:
                raise LookupError("response retry was not found")
            retry.status = status.value
            retry.next_attempt_at = now
            retry.last_error = None if error_message is None else error_message[:2_000]
            await session.flush()
            return retry


__all__ = ["ResponseRetryRepository"]
