from __future__ import annotations

import json
import os
import subprocess
from typing import Any

OVERRIDES = {
    "PULSE_QUERY_MAX_ROWS": "321",
    "PULSE_OPTIONAL_SIGNAL_FRESHNESS_SECONDS": "17",
    "PULSE_BASELINE_STRATEGY": "exponential",
    "PULSE_EXPONENTIAL_SMOOTHING_ALPHA": "0.42",
    "PULSE_REACTIVE_CPU_THRESHOLD_PCT": "71",
    "PULSE_FORECAST_HORIZON_SECONDS": "31",
    "PULSE_RPS_PER_INSTANCE": "26",
    "PULSE_SCHEDULED_LOOKAHEAD_SECONDS": "7200",
    "PULSE_SCHEDULED_RAMP_STEPS": "5",
    "PULSE_FEEDBACK_HORIZON_SECONDS": "41",
    "PULSE_RECONCILIATION_MIN_AGE_SECONDS": "11",
    "PULSE_STATUS_CAPACITY_TIMEOUT_SECONDS": "4",
    "PULSE_AWS_SDK_READ_TIMEOUT_SECONDS": "6",
    "PULSE_SNAPSHOT_RETENTION_SECONDS": "86400",
}


def verify_environment(configuration: dict[str, Any]) -> None:
    services = configuration.get("services", {})
    agent = services.get("agent", {})
    environment = agent.get("environment", {})
    missing = {
        key: expected
        for key, expected in OVERRIDES.items()
        if str(environment.get(key)) != expected
    }
    if missing:
        raise RuntimeError(f"resolved agent environment lost overrides: {missing}")
    volume = configuration.get("volumes", {}).get("pulse-postgres-data", {})
    resolved_name = str(volume.get("name", ""))
    if resolved_name != "pulse-config-contract_pulse-postgres-data":
        raise RuntimeError(
            "PostgreSQL volume must remain project-scoped; "
            f"resolved name was {resolved_name!r}"
        )


def main() -> int:
    environment = {
        **os.environ,
        **OVERRIDES,
        "COMPOSE_PROJECT_NAME": "pulse-config-contract",
    }
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    verify_environment(json.loads(result.stdout))
    print("Resolved Compose overrides and project-scoped volume identity passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["OVERRIDES", "verify_environment"]
