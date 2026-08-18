from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from load_tests.lib.models import BaselineType, ScenarioPlan, SignalFrame


class PulseApiError(RuntimeError):
    """A sanitized Pulse or demo-app API failure."""


class PulseApiClient:
    def __init__(
        self,
        *,
        agent_url: str,
        demo_app_url: str,
        control_token: str,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.demo_app_url = demo_app_url.rstrip("/")
        self._headers = {"X-Pulse-Control-Token": control_token}
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PulseApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start_run(
        self,
        plan: ScenarioPlan,
        *,
        baseline_type: BaselineType,
        environment: str,
        execution_mode: str,
        started_at: datetime,
        idempotency_key: str,
        host: str,
    ) -> dict[str, Any]:
        payload = {
            "scenario_name": plan.name,
            "mode": "scheduled" if plan.name == "scheduled-diwali" else "realtime",
            "environment": environment,
            "baseline_type": baseline_type,
            "execution_mode": execution_mode,
            "configuration": {
                "seed": plan.seed,
                "duration_seconds": plan.duration_seconds,
                "host": host,
                "endpoint_weights": {
                    "/checkout": 5,
                    "/catalog": 3,
                    "/recommendations": 2,
                },
            },
            "thresholds": {
                "onset_ratio": 1.2,
                "comparator": "cpu_or_configured_reactive_load",
            },
            "started_at": _utc(started_at),
            "idempotency_key": idempotency_key,
        }
        return self._json(
            self._client.post(
                f"{self.agent_url}/internal/demo-runs",
                headers=self._headers,
                json=payload,
            )
        )

    def complete_run(
        self,
        run_id: UUID | str,
        *,
        status: str,
        ended_at: datetime,
        locust_summary: dict[str, Any],
        notes: str,
    ) -> dict[str, Any]:
        return self._json(
            self._client.patch(
                f"{self.agent_url}/internal/demo-runs/{run_id}",
                headers=self._headers,
                json={
                    "status": status,
                    "ended_at": _utc(ended_at),
                    "locust_summary": locust_summary,
                    "notes": notes,
                },
            )
        )

    def publish_signal(
        self,
        frame: SignalFrame,
        *,
        observed_at: datetime,
        run_id: UUID | str,
        source: str,
        ttl_seconds: int = 30,
    ) -> dict[str, Any]:
        payload = {
            "source": source,
            "observed_at": _utc(observed_at),
            "demo_run_id": str(run_id),
            "ttl_seconds": ttl_seconds,
            "edge_request_rate_rps": frame.edge_request_rate_rps,
            "queue_depth": frame.queue_depth,
            "concurrent_sessions": frame.concurrent_sessions,
            "login_rate_rps": frame.login_rate_rps,
            "cpu_utilization_pct": frame.cpu_utilization_pct,
        }
        return self._json(
            self._client.post(
                f"{self.agent_url}/internal/signals/simulated",
                headers=self._headers,
                json={key: value for key, value in payload.items() if value is not None},
            )
        )

    def reset_tier(
        self,
        *,
        environment: str,
        reason: str = "Scoped load-test reset",
    ) -> dict[str, Any]:
        return self._json(
            self._client.post(
                f"{self.demo_app_url}/internal/load-shedding",
                headers=self._headers,
                json={
                    "level": 0,
                    "reason_code": "load_test_reset",
                    "reasoning": reason,
                    "changed_by": "operator",
                    "signal_evidence": {"environment": environment, "scope": "tier_only"},
                },
            )
        )

    def run_detail(self, run_id: UUID | str) -> dict[str, Any]:
        return self._json(
            self._client.get(f"{self.agent_url}/api/v1/results/runs/{run_id}")
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            try:
                body = response.json()
                code = body.get("code", "PULSE_API_ERROR")
                detail = body.get("detail", "request failed")
            except ValueError:
                code = "PULSE_API_ERROR"
                detail = "request failed without a JSON error body"
            raise PulseApiError(f"{response.status_code} {code}: {detail}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise PulseApiError("Pulse API returned a non-object response")
        return payload


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


__all__ = ["PulseApiClient", "PulseApiError"]
