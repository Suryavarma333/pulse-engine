from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent.app.services.ramp_planner import RampPlanner, resolve_local_event_time
from db.models import ScheduledEvent

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def event(**overrides) -> ScheduledEvent:
    values = {
        "id": uuid4(),
        "name": "Diwali sale",
        "event_type": "sale",
        "starts_at": NOW + timedelta(hours=1),
        "ends_at": NOW + timedelta(hours=2),
        "timezone": "Asia/Kolkata",
        "expected_multiplier": 4,
        "prewarm_lead_seconds": 1_800,
        "peak_lead_seconds": 300,
        "scale_down_duration_seconds": 1_800,
        "minimum_desired_capacity": 1,
        "peak_desired_capacity": 6,
        "max_instances_override": 4,
        "ramp_profile": {},
        "confidence": 0.9,
        "source": "test",
        "status": "active",
    }
    values.update(overrides)
    return ScheduledEvent(**values)


def test_derived_ramp_is_monotonic_stable_and_clamped_by_both_ceilings() -> None:
    scheduled = event()
    plan = RampPlanner(global_ceiling=3, derived_steps=4).plan(scheduled)

    assert [point.offset_seconds for point in plan.points] == [-1800, -1300, -800, -300]
    assert [point.requested_capacity for point in plan.points] == [1, 3, 4, 6]
    assert [point.desired_capacity for point in plan.points] == [1, 3, 3, 3]
    assert plan.effective_ceiling == 3
    assert plan.points[-1].due_at < scheduled.starts_at
    assert plan.points[-1].idempotency_key.endswith(":-300:3")
    assert RampPlanner(global_ceiling=3, derived_steps=4).plan(scheduled) == plan


def test_stored_profile_is_used_and_requires_strict_monotonicity() -> None:
    stored = event(
        peak_desired_capacity=3,
        max_instances_override=3,
        ramp_profile={
            "points": [
                {"offset_seconds": -1800, "desired_capacity": 1},
                {"offset_seconds": -900, "desired_capacity": 2},
                {"offset_seconds": -300, "desired_capacity": 3},
            ]
        },
    )
    plan = RampPlanner(global_ceiling=3).plan(stored)
    assert [point.desired_capacity for point in plan.points] == [1, 2, 3]

    stored.ramp_profile = {
        "points": [
            {"offset_seconds": -900, "desired_capacity": 2},
            {"offset_seconds": -900, "desired_capacity": 3},
        ]
    }
    with pytest.raises(ValueError, match="strictly increasing"):
        RampPlanner(global_ceiling=3).plan(stored)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"starts_at": NOW.replace(tzinfo=None)}, "timezone-aware"),
        ({"timezone": "Mars/Olympus"}, "unknown IANA timezone"),
        ({"prewarm_lead_seconds": 60, "peak_lead_seconds": 90}, "peak lead"),
        ({"minimum_desired_capacity": 4, "peak_desired_capacity": 3}, "peak capacity"),
        ({"ramp_profile": {"points": "invalid"}}, "non-empty list"),
    ],
)
def test_invalid_event_and_profile_inputs_are_rejected(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        RampPlanner(global_ceiling=3).plan(event(**overrides))


def test_dst_ambiguous_and_nonexistent_local_times_require_explicit_resolution() -> None:
    ambiguous = datetime(2026, 11, 1, 1, 30)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_local_event_time(ambiguous, "America/New_York")
    first = resolve_local_event_time(ambiguous, "America/New_York", fold=0)
    second = resolve_local_event_time(ambiguous, "America/New_York", fold=1)
    assert second - first == timedelta(hours=1)

    nonexistent = datetime(2026, 3, 8, 2, 30)
    with pytest.raises(ValueError, match="does not exist"):
        resolve_local_event_time(nonexistent, "America/New_York")


def test_due_and_next_point_are_timezone_aware_and_latest_due_is_identifiable() -> None:
    plan = RampPlanner(global_ceiling=3).plan(event(peak_desired_capacity=3))
    instant = NOW + timedelta(minutes=45)
    due = plan.due(instant)
    assert due
    assert due[-1].due_at <= instant
    assert plan.next_after(instant).due_at > instant
    with pytest.raises(ValueError, match="timezone-aware"):
        plan.due(instant.replace(tzinfo=None))
