from __future__ import annotations

import argparse
from pathlib import Path

from load_tests.lib.evidence import export_evidence
from load_tests.lib.pulse_api import PulseApiClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Export persisted Pulse run evidence")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agent-url", default="http://localhost:8100")
    parser.add_argument("--output-directory", type=Path, default=Path("load_tests/results"))
    args = parser.parse_args()
    with PulseApiClient(
        agent_url=args.agent_url,
        demo_app_url="http://localhost:8000",
        control_token="unused-for-read-only-export",
    ) as api:
        detail = api.run_detail(args.run_id)
    paths = export_evidence(
        detail,
        output_directory=args.output_directory,
        stem=f"run-{args.run_id}",
    )
    print(" ".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
