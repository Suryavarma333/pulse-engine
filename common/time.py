from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""

    return datetime.now(UTC)


def ensure_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Reject naive datetimes and normalize aware values to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return utc_now()
