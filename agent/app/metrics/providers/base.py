from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from common.contracts import SignalReading


@runtime_checkable
class SignalProvider(Protocol):
    """A bounded provider that returns one typed reading for an evaluation instant."""

    name: str
    required: bool
    timeout_seconds: float
    freshness_limit_seconds: float

    async def read(self, now: datetime) -> SignalReading: ...


__all__ = ["SignalProvider"]
