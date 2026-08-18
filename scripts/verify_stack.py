from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-Pulse-Control-Token"] = token
    request = Request(url, method=method, data=data, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - caller controls local URLs
            body = json.loads(response.read())
            return body, {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} returned {exc.code}: {detail[:300]}") from exc


def verify(*, demo_url: str, agent_url: str, token: str) -> None:
    demo_health, _ = request_json(f"{demo_url.rstrip('/')}/health")
    agent_health, _ = request_json(f"{agent_url.rstrip('/')}/health")
    if not demo_health.get("critical_path_protected"):
        raise RuntimeError("demo health does not report critical-path protection")
    if not agent_health.get("ready"):
        raise RuntimeError("agent health does not report ready")

    for level in range(4):
        request_json(
            f"{demo_url.rstrip('/')}/internal/load-shedding",
            method="POST",
            token=token,
            payload={
                "level": level,
                "reason_code": "stack_smoke",
                "reasoning": f"Verify checkout at protection level {level}",
                "changed_by": "system",
                "signal_evidence": {"scope": "bounded_stack_smoke"},
            },
        )
        checkout, headers = request_json(
            f"{demo_url.rstrip('/')}/checkout",
            method="POST",
            payload={"cart_id": f"stack-smoke-{level}", "item_count": 1},
        )
        if not checkout.get("accepted"):
            raise RuntimeError(f"checkout was not accepted at level {level}")
        if headers.get("x-pulse-endpoint-mode") != "normal":
            raise RuntimeError(f"checkout was not normal at level {level}")
        if headers.get("x-pulse-protection") != "critical-never-shed":
            raise RuntimeError(f"checkout protection header missing at level {level}")

    request_json(
        f"{demo_url.rstrip('/')}/internal/load-shedding",
        method="POST",
        token=token,
        payload={
            "level": 0,
            "reason_code": "stack_smoke_reset",
            "reasoning": "Return the bounded stack smoke to normal",
            "changed_by": "system",
            "signal_evidence": {"scope": "tier_only"},
        },
    )
    status, _ = request_json(f"{agent_url.rstrip('/')}/api/v1/status")
    if status.get("execution_mode") != "dry_run":
        raise RuntimeError("stack smoke is not running in dry-run mode")
    if int(status.get("global_ceiling", 0)) > 3:
        raise RuntimeError("stack smoke ceiling exceeds three instances")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a running Pulse stack safely")
    parser.add_argument("--demo-url", default="http://localhost:8000")
    parser.add_argument("--agent-url", default="http://localhost:8100")
    parser.add_argument("--control-token", required=True)
    args = parser.parse_args()
    verify(demo_url=args.demo_url, agent_url=args.agent_url, token=args.control_token)
    print("Pulse stack health, dry-run bounds, and checkout protection passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
