from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, TypeVar

from common.contracts import QueryWindow

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RepositoryLimits:
    max_rows: int = 1_000
    max_window: timedelta = timedelta(days=1)

    def __post_init__(self) -> None:
        if not 1 <= self.max_rows <= 1_000:
            raise ValueError("max_rows must be between 1 and 1000")
        if self.max_window <= timedelta(0):
            raise ValueError("max_window must be positive")

    def validate_window(self, window: QueryWindow) -> None:
        if window.limit > self.max_rows:
            raise ValueError(f"query limit must not exceed {self.max_rows}")
        if window.end - window.start > self.max_window:
            raise ValueError(f"query window must not exceed {self.max_window}")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: Sequence[T]
    has_more: bool


def bounded_limit(limit: int, maximum: int) -> int:
    if limit < 1:
        raise ValueError("limit must be positive")
    if not 1 <= maximum <= 1_000:
        raise ValueError("maximum must be between 1 and 1000")
    return min(limit, maximum)
