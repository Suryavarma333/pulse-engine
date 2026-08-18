from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from db.models import ScheduledEvent
from db.session import Database

EVENT_ID = uuid5(NAMESPACE_URL, "pulse-load-test:scheduled-diwali:v1")


async def seed_diwali_event(
    *,
    database_url: str,
    starts_at: datetime,
    duration_seconds: int,
    peak_capacity: int,
    ceiling: int,
    smoke: bool,
) -> ScheduledEvent:
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        raise ValueError("starts_at must be timezone-aware")
    if peak_capacity < 1 or ceiling < peak_capacity:
        raise ValueError("peak capacity must be positive and at or below the ceiling")
    if duration_seconds < 10:
        raise ValueError("duration_seconds must be at least 10")
    starts_at = starts_at.astimezone(UTC)
    prewarm = 8 if smoke else 90
    peak_lead = 2 if smoke else 15
    points = [
        {"offset_seconds": -prewarm, "desired_capacity": 1},
        {"offset_seconds": -(prewarm // 2), "desired_capacity": min(2, peak_capacity)},
        {"offset_seconds": -peak_lead, "desired_capacity": peak_capacity},
    ]
    database = Database(database_url)
    try:
        async with database.session_factory() as session, session.begin():
            event = await session.get(ScheduledEvent, EVENT_ID)
            if event is None:
                event = ScheduledEvent(id=EVENT_ID)
                session.add(event)
            event.name = "Pulse Demo: Diwali scheduled traffic"
            event.event_type = "commerce_event"
            event.starts_at = starts_at
            event.ends_at = starts_at + timedelta(seconds=duration_seconds)
            event.timezone = "Asia/Kolkata"
            event.recurrence_rule = None
            event.expected_multiplier = 8.0
            event.baseline_rps = 5.0
            event.expected_peak_rps = 120.0 if not smoke else 30.0
            event.prewarm_lead_seconds = prewarm
            event.peak_lead_seconds = peak_lead
            event.scale_down_duration_seconds = 60 if smoke else 300
            event.minimum_desired_capacity = 1
            event.peak_desired_capacity = peak_capacity
            event.max_instances_override = ceiling
            event.ramp_profile = {"points": points, "seed": 20260818, "generated": True}
            event.confidence = 0.95
            event.source = "load_test"
            event.status = "active"
            await session.flush()
        return event
    finally:
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotently seed the scheduled Diwali demo")
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "PULSE_DATABASE_URL",
            "postgresql+psycopg://pulse:pulse-local-only@localhost:5432/pulse",
        ),
    )
    parser.add_argument("--starts-in-seconds", type=int, default=120)
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--peak-capacity", type=int, default=3)
    parser.add_argument("--ceiling", type=int, default=3)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    starts_at = datetime.now(UTC) + timedelta(seconds=args.starts_in_seconds)
    event = asyncio.run(
        seed_diwali_event(
            database_url=args.database_url,
            starts_at=starts_at,
            duration_seconds=args.duration_seconds,
            peak_capacity=args.peak_capacity,
            ceiling=args.ceiling,
            smoke=args.smoke,
        )
    )
    print(f"seeded event={event.id} starts_at={event.starts_at.isoformat()} source=load_test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
