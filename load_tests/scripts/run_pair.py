from __future__ import annotations

import argparse
import subprocess
import sys
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reactive-only then Pulse with equal inputs")
    parser.add_argument("--scenario", choices=("scheduled-diwali", "sudden-spike"), required=True)
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--agent-url", default="http://localhost:8100")
    parser.add_argument("--demo-app-url", default="http://localhost:8000")
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    common = [
        sys.executable,
        "load_tests/scripts/run_scenario.py",
        "--scenario",
        args.scenario,
        "--control-token",
        args.control_token,
        "--host",
        args.host,
        "--agent-url",
        args.agent_url,
        "--demo-app-url",
        args.demo_app_url,
        "--seed",
        str(args.seed),
    ]
    if args.smoke:
        common.append("--smoke")
    common.extend(
        ["--pair-group-id", f"pair:{args.scenario}:{args.seed}:{uuid4()}"]
    )
    for baseline in ("reactive_only", "pulse"):
        subprocess.run(
            [
                sys.executable,
                "scripts/reset_demo.py",
                "--agent-url",
                args.agent_url,
                "--demo-app-url",
                args.demo_app_url,
                "--control-token",
                args.control_token,
            ],
            check=True,
        )
        subprocess.run([*common, "--baseline", baseline], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
