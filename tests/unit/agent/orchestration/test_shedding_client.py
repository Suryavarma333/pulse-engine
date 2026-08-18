from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from agent.app.services.shedding_client import DemoAppSheddingClient, SheddingControlError
from common.contracts import RecoveryPlan, ResponseCommand
from common.enums import PredictionMode, SheddingLevel


def command() -> ResponseCommand:
    return ResponseCommand(
        idempotency_key="shedding:test:1",
        correlation_id=uuid4(),
        mode=PredictionMode.REALTIME,
        target_resource="pulse-asg",
        requested_desired_capacity=2,
        maximum_ceiling=3,
        requested_shedding_level=SheddingLevel.DISABLE_RECOMMENDATIONS,
        reason_code="protect_checkout",
        reasoning="Reduce non-critical work while checkout remains normal",
        signal_evidence={"request_rate_rps": 40.0},
        recovery_plan=RecoveryPlan(
            low_threshold_rps=2,
            confirmation_count=3,
            cooldown_seconds=60,
            decrement_step=1,
            capacity_floor=1,
        ),
    )


def test_client_sends_authenticated_correlated_command_without_token_in_body() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers["X-Pulse-Control-Token"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "changed": True,
                "event_id": str(uuid4()),
                "level": 1,
                "endpoint_policies": {"checkout": "normal", "recommendations": "disabled"},
            },
        )

    http_client = httpx.AsyncClient(
        base_url="http://demo-app:8000", transport=httpx.MockTransport(handler)
    )
    client = DemoAppSheddingClient(
        base_url="http://demo-app:8000",
        control_token=SecretStr("internal-only-token"),
        timeout_seconds=1,
        client=http_client,
    )
    result = asyncio.run(
        client.apply(command=command(), level=SheddingLevel.DISABLE_RECOMMENDATIONS)
    )
    asyncio.run(http_client.aclose())

    assert seen["header"] == "internal-only-token"
    assert "token" not in json.dumps(seen["body"]).lower()
    assert result.endpoint_policies["checkout"] == "normal"


def test_client_classifies_auth_failure_without_leaking_response_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "code": "INVALID_CONTROL_TOKEN",
                "detail": "received internal-only-token",
            },
        )

    http_client = httpx.AsyncClient(
        base_url="http://demo-app:8000", transport=httpx.MockTransport(handler)
    )
    client = DemoAppSheddingClient(
        base_url="http://demo-app:8000",
        control_token="internal-only-token",
        timeout_seconds=1,
        client=http_client,
    )
    with pytest.raises(SheddingControlError) as raised:
        asyncio.run(client.apply(command=command(), level=SheddingLevel.NORMAL))
    asyncio.run(http_client.aclose())

    assert raised.value.retryable is False
    assert raised.value.status_code == 401
    assert "internal-only-token" not in str(raised.value)


def test_client_classifies_service_failure_as_retryable() -> None:
    http_client = httpx.AsyncClient(
        base_url="http://demo-app:8000",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"code": "AUDIT_UNAVAILABLE"})
        ),
    )
    client = DemoAppSheddingClient(
        base_url="http://demo-app:8000",
        control_token="test-token",
        timeout_seconds=1,
        client=http_client,
    )
    with pytest.raises(SheddingControlError) as raised:
        asyncio.run(client.apply(command=command(), level=SheddingLevel.NORMAL))
    asyncio.run(http_client.aclose())
    assert raised.value.retryable is True
