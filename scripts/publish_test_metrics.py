from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from load_tests.lib.models import build_scenario_plan
from load_tests.lib.pulse_api import PulseApiClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish deterministic leading-signal frames")
    parser.add_argument("--scenario", choices=("scheduled-diwali", "sudden-spike"), required=True)
    parser.add_argument("--baseline", choices=("pulse", "reactive_only"), default="pulse")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agent-url", default="http://localhost:8100")
    parser.add_argument("--demo-app-url", default="http://localhost:8000")
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()
    plan = build_scenario_plan(args.scenario, seed=args.seed, smoke=args.smoke)
    started = time.monotonic()
    with PulseApiClient(
        agent_url=args.agent_url,
        demo_app_url=args.demo_app_url,
        control_token=args.control_token,
    ) as api:
        for index, frame in enumerate(plan.signals_for(args.baseline)):
            if not args.no_wait:
                time.sleep(max(0.0, frame.at_seconds - (time.monotonic() - started)))
            api.publish_signal(
                frame,
                observed_at=datetime.now(UTC),
                run_id=args.run_id,
                source=f"load-test:{args.scenario}:{args.baseline}:{index}",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
