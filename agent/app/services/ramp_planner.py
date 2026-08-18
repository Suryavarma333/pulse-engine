from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.time import ensure_utc


class ScheduledEventLike(Protocol):
    id: UUID
    starts_at: datetime
    ends_at: datetime
    timezone: str
    prewarm_lead_seconds: int
    peak_lead_seconds: int
    minimum_desired_capacity: int
    peak_desired_capacity: int
    max_instances_override: int | None
    ramp_profile: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RampPoint:
    due_at: datetime
    offset_seconds: int
    desired_capacity: int
    requested_capacity: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RampPlan:
    event_id: UUID
    timezone: str
    effective_ceiling: int
    points: tuple[RampPoint, ...]

    def due(self, now: datetime) -> tuple[RampPoint, ...]:
        instant = ensure_utc(now, field_name="now")
        return tuple(point for point in self.points if point.due_at <= instant)

    def next_after(self, now: datetime) -> RampPoint | None:
        instant = ensure_utc(now, field_name="now")
        return next((point for point in self.points if point.due_at > instant), None)


class RampPlanner:
    """Validates stored profiles or derives a monotonic, ceiling-bounded prewarm ramp."""

    def __init__(self, *, global_ceiling: int, derived_steps: int = 4) -> None:
        if not 1 <= global_ceiling <= 100:
            raise ValueError("global_ceiling must be between 1 and 100")
        if not 2 <= derived_steps <= 100:
            raise ValueError("derived_steps must be between 2 and 100")
        self._global_ceiling = global_ceiling
        self._derived_steps = derived_steps

    def plan(self, event: ScheduledEventLike) -> RampPlan:
        starts_at = ensure_utc(event.starts_at, field_name="starts_at")
        ends_at = ensure_utc(event.ends_at, field_name="ends_at")
        if ends_at <= starts_at:
            raise ValueError("scheduled event must end after it starts")
        _load_zone(event.timezone)
        if event.prewarm_lead_seconds <= 0:
            raise ValueError("prewarm_lead_seconds must be positive")
        if not 0 <= event.peak_lead_seconds <= event.prewarm_lead_seconds:
            raise ValueError("peak lead must be within the prewarm horizon")
        if event.minimum_desired_capacity < 0:
            raise ValueError("minimum capacity must be nonnegative")
        if event.peak_desired_capacity < event.minimum_desired_capacity:
            raise ValueError("peak capacity must not be below minimum capacity")
        event_ceiling = event.max_instances_override or self._global_ceiling
        if event_ceiling < 1:
            raise ValueError("event ceiling must be positive")
        effective_ceiling = min(self._global_ceiling, event_ceiling)

        raw_points = self._stored_points(event.ramp_profile)
        if raw_points is None:
            raw_points = self._derived_points(event)
        self._validate_monotonic(raw_points, event)
        points = tuple(
            RampPoint(
                due_at=starts_at + timedelta(seconds=offset),
                offset_seconds=offset,
                desired_capacity=min(requested, effective_ceiling),
                requested_capacity=requested,
                idempotency_key=(
                    f"scheduled:{event.id}:{offset}:{min(requested, effective_ceiling)}"
                ),
            )
            for offset, requested in raw_points
        )
        return RampPlan(event.id, event.timezone, effective_ceiling, points)

    @staticmethod
    def _stored_points(profile: dict[str, Any]) -> list[tuple[int, int]] | None:
        if not profile:
            return None
        raw = profile.get("points")
        if not isinstance(raw, list) or not raw:
            raise ValueError("stored ramp_profile.points must be a non-empty list")
        points: list[tuple[int, int]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each stored ramp point must be an object")
            offset = item.get("offset_seconds")
            capacity = item.get("desired_capacity")
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise ValueError("ramp offset_seconds must be an integer")
            if isinstance(capacity, bool) or not isinstance(capacity, int):
                raise ValueError("ramp desired_capacity must be an integer")
            points.append((offset, capacity))
        return points

    def _derived_points(self, event: ScheduledEventLike) -> list[tuple[int, int]]:
        start_offset = -event.prewarm_lead_seconds
        peak_offset = -event.peak_lead_seconds
        capacity_span = event.peak_desired_capacity - event.minimum_desired_capacity
        offset_span = peak_offset - start_offset
        points: list[tuple[int, int]] = []
        for index in range(self._derived_steps):
            fraction = index / (self._derived_steps - 1)
            offset = round(start_offset + offset_span * fraction)
            capacity = round(event.minimum_desired_capacity + capacity_span * fraction)
            if not points or points[-1] != (offset, capacity):
                points.append((offset, capacity))
        points[0] = (start_offset, event.minimum_desired_capacity)
        points[-1] = (peak_offset, event.peak_desired_capacity)
        return points

    @staticmethod
    def _validate_monotonic(
        points: list[tuple[int, int]], event: ScheduledEventLike
    ) -> None:
        if len(points) > 100:
            raise ValueError("a ramp profile may contain at most 100 points")
        offsets = [point[0] for point in points]
        capacities = [point[1] for point in points]
        if any(offset > 0 for offset in offsets):
            raise ValueError("prewarm ramp points must not occur after event start")
        if offsets != sorted(set(offsets)):
            raise ValueError("ramp offsets must be strictly increasing")
        if capacities != sorted(capacities) or any(value < 0 for value in capacities):
            raise ValueError("ramp capacities must be nonnegative and monotonic")
        if offsets[0] < -event.prewarm_lead_seconds:
            raise ValueError("ramp starts before the configured prewarm horizon")
        if offsets[-1] > -event.peak_lead_seconds:
            raise ValueError("peak ramp point is later than the configured peak lead")
        if capacities[0] < event.minimum_desired_capacity:
            raise ValueError("ramp starts below the configured minimum capacity")
        if capacities[-1] < event.peak_desired_capacity:
            raise ValueError("ramp does not reach the configured peak capacity")


def resolve_local_event_time(
    local_time: datetime,
    timezone_name: str,
    *,
    fold: int | None = None,
) -> datetime:
    """Resolve a naive event time, rejecting DST gaps and ambiguous time without a fold."""

    if local_time.tzinfo is not None:
        raise ValueError("local event time must be naive")
    if fold not in {None, 0, 1}:
        raise ValueError("fold must be 0 or 1")
    zone = _load_zone(timezone_name)
    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        aware = local_time.replace(tzinfo=zone, fold=candidate_fold)
        utc = aware.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == local_time:
            candidates.append(aware)
    unique_offsets = {item.utcoffset() for item in candidates}
    if not candidates:
        raise ValueError("local event time does not exist in the selected timezone")
    if len(unique_offsets) > 1 and fold is None:
        raise ValueError("local event time is ambiguous; an explicit fold is required")
    selected_fold = fold or 0
    selected = next((item for item in candidates if item.fold == selected_fold), None)
    if selected is None:
        raise ValueError("the requested fold is invalid for this local event time")
    return selected.astimezone(UTC)


def _load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name}") from exc


__all__ = [
    "RampPlan",
    "RampPlanner",
    "RampPoint",
    "ScheduledEventLike",
    "resolve_local_event_time",
]
