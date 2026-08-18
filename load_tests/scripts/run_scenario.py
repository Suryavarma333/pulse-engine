from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from load_tests.lib.evidence import export_evidence, read_locust_summary
from load_tests.lib.models import BaselineType, ScenarioName, ScenarioPlan, build_scenario_plan
from load_tests.lib.pulse_api import PulseApiClient


def _publish_signals(
    *,
    api: PulseApiClient,
    plan: ScenarioPlan,
    baseline: BaselineType,
    run_id: str,
    stop: threading.Event,
    errors: list[Exception],
) -> None:
    try:
        started = time.monotonic()
        for index, frame in enumerate(plan.signals_for(baseline)):
            while not stop.wait(0.1):
                if time.monotonic() - started >= frame.at_seconds:
                    break
            if stop.is_set():
                return
            api.publish_signal(
                frame,
                observed_at=datetime.now(UTC),
                run_id=run_id,
                source=f"load-test:{plan.name}:{baseline}:{index}",
            )
    except Exception as exc:
        errors.append(exc)
        stop.set()


def execute(
    *,
    scenario: ScenarioName,
    baseline: BaselineType,
    host: str,
    agent_url: str,
    demo_app_url: str,
    control_token: str,
    environment: str,
    execution_mode: str,
    seed: int,
    smoke: bool,
    output_directory: Path,
    python: str,
    evaluation_timeout_seconds: float = 45,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    plan = build_scenario_plan(scenario, seed=seed, smoke=smoke)
    started_at = datetime.now(UTC)
    key = idempotency_key or (
        f"load:{scenario}:{baseline}:{seed}:{started_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    stem = f"{scenario}-{baseline}-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_prefix = output_directory / stem
    environment_values = {
        **os.environ,
        "PULSE_SCENARIO": scenario,
        "PULSE_SCENARIO_SEED": str(seed),
        "PULSE_SMOKE": str(smoke).lower(),
    }
    with PulseApiClient(
        agent_url=agent_url,
        demo_app_url=demo_app_url,
        control_token=control_token,
    ) as api:
        started = api.start_run(
            plan,
            baseline_type=baseline,
            environment=environment,
            execution_mode=execution_mode,
            started_at=started_at,
            idempotency_key=key,
            host=host,
        )
        run_id = str(started["id"])
        stop = threading.Event()
        signal_errors: list[Exception] = []
        publisher = threading.Thread(
            target=_publish_signals,
            kwargs={
                "api": api,
                "plan": plan,
                "baseline": baseline,
                "run_id": run_id,
                "stop": stop,
                "errors": signal_errors,
            },
            daemon=True,
            name="pulse-leading-signals",
        )
        publisher.start()
        command = [
            python,
            "-m",
            "locust",
            "-f",
            "load_tests/locustfile.py",
            "--headless",
            "--host",
            host,
            "--csv",
            str(csv_prefix),
            "--only-summary",
        ]
        result = subprocess.run(command, env=environment_values, check=False)
        stop.set()
        publisher.join(timeout=2)
        ended_at = datetime.now(UTC)
        if result.returncode == 0 and not signal_errors:
            summary = read_locust_summary(csv_prefix).as_api_payload()
            terminal_status = "completed"
        else:
            summary = {"request_count": 0, "failure_count": 0, "exit_code": result.returncode}
            terminal_status = "failed"
        terminal = api.complete_run(
            run_id,
            status=terminal_status,
            ended_at=ended_at,
            locust_summary=summary,
            notes=(
                f"deterministic {scenario} {baseline} run; smoke={smoke}; seed={seed}"
            ),
        )
        deadline = time.monotonic() + evaluation_timeout_seconds
        detail = api.run_detail(run_id)
        while detail.get("status") == "pending_evaluation" and time.monotonic() < deadline:
            time.sleep(1)
            detail = api.run_detail(run_id)
        export_evidence(detail, output_directory=output_directory, stem=stem)
        if signal_errors:
            raise RuntimeError(
                f"Leading-signal publication failed; run_id={run_id}; "
                f"error={type(signal_errors[0]).__name__}"
            )
        if result.returncode != 0:
            raise RuntimeError(f"Locust exited with code {result.returncode}; run_id={run_id}")
        return {"run_id": run_id, "terminal": terminal, "evidence": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one traceable Pulse load scenario")
    parser.add_argument("--scenario", choices=("scheduled-diwali", "sudden-spike"), required=True)
    parser.add_argument("--baseline", choices=("pulse", "reactive_only"), default="pulse")
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--agent-url", default="http://localhost:8100")
    parser.add_argument("--demo-app-url", default="http://localhost:8000")
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--environment", default="local")
    parser.add_argument("--execution-mode", choices=("dry_run", "live"), default="dry_run")
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--output-directory", type=Path, default=Path("load_tests/results"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--evaluation-timeout-seconds", type=float, default=45)
    parser.add_argument(
        "--idempotency-key",
        help="Stable retry key for resuming the same logical run start",
    )
    args = parser.parse_args()
    outcome = execute(
        scenario=args.scenario,
        baseline=args.baseline,
        host=args.host,
        agent_url=args.agent_url,
        demo_app_url=args.demo_app_url,
        control_token=args.control_token,
        environment=args.environment,
        execution_mode=args.execution_mode,
        seed=args.seed,
        smoke=args.smoke,
        output_directory=args.output_directory,
        python=args.python,
        evaluation_timeout_seconds=args.evaluation_timeout_seconds,
        idempotency_key=args.idempotency_key,
    )
    print(f"run_id={outcome['run_id']} status={outcome['terminal']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
