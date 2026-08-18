from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime

from sqlalchemy import delete, select

from common.enums import DemoRunStatus
from db.models import DemoRun, ScheduledEvent
from db.session import Database
from load_tests.lib.pulse_api import PulseApiClient

SCENARIOS = ("scheduled-diwali", "sudden-spike")


async def find_active_generated_run(database_url: str, environment: str) -> str | None:
    database = Database(database_url)
    try:
        async with database.session_factory() as session:
            run_id = await session.scalar(
                select(DemoRun.id)
                .where(
                    DemoRun.environment == environment,
                    DemoRun.scenario_name.in_(SCENARIOS),
                    DemoRun.status.in_(("running", "pending_evaluation")),
                )
                .order_by(DemoRun.started_at.desc())
                .limit(1)
            )
            return None if run_id is None else str(run_id)
    finally:
        await database.dispose()


async def purge_generated_records(database_url: str, environment: str) -> tuple[int, int]:
    database = Database(database_url)
    try:
        async with database.session_factory() as session, session.begin():
            runs = await session.execute(
                delete(DemoRun).where(
                    DemoRun.environment == environment,
                    DemoRun.scenario_name.in_(SCENARIOS),
                )
            )
            events = await session.execute(
                delete(ScheduledEvent).where(ScheduledEvent.source == "load_test")
            )
            return runs.rowcount or 0, events.rowcount or 0
    finally:
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotently reset scoped Pulse demo state")
    parser.add_argument("--agent-url", default="http://localhost:8100")
    parser.add_argument("--demo-app-url", default="http://localhost:8000")
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--environment", default="local")
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "PULSE_DATABASE_URL",
            "postgresql+psycopg://pulse:pulse-local-only@localhost:5432/pulse",
        ),
    )
    parser.add_argument(
        "--purge-generated",
        action="store_true",
        help="Delete only load-test scenario runs and source=load_test event seeds",
    )
    args = parser.parse_args()
    with PulseApiClient(
        agent_url=args.agent_url,
        demo_app_url=args.demo_app_url,
        control_token=args.control_token,
    ) as api:
        run_id = asyncio.run(find_active_generated_run(args.database_url, args.environment))
        if run_id is not None:
            api.complete_run(
                run_id,
                status=DemoRunStatus.CANCELLED.value,
                ended_at=datetime.now(UTC),
                locust_summary={},
                notes="Scoped idempotent reset cancelled an unfinished generated run",
            )
        tier = api.reset_tier(environment=args.environment)
    print(f"tier={tier['level']} changed={tier.get('changed', False)}")
    if args.purge_generated:
        run_count, event_count = asyncio.run(
            purge_generated_records(args.database_url, args.environment)
        )
        print(f"purged generated runs={run_count} scheduled_events={event_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
