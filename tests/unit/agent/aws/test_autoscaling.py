from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ReadTimeoutError

from agent.app.aws.autoscaling import AutoScalingCapacityAdapter
from common.enums import ActionStatus, ExecutionMode, ProviderStatus, ResponseIntent

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class FakeAutoScalingClient:
    def __init__(self, *, desired: int = 1, failure: Exception | None = None) -> None:
        self.desired = desired
        self.failure = failure
        self.describe_calls = 0
        self.set_calls: list[dict] = []

    def describe_auto_scaling_groups(self, **kwargs):
        self.describe_calls += 1
        return {
            "AutoScalingGroups": [
                {
                    "DesiredCapacity": self.desired,
                    "Instances": [
                        {"LifecycleState": "InService"},
                        {"LifecycleState": "Pending"},
                    ],
                }
            ],
            "ResponseMetadata": {"RequestId": "describe-request"},
        }

    def set_desired_capacity(self, **kwargs):
        self.set_calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        self.desired = kwargs["DesiredCapacity"]
        return {"ResponseMetadata": {"RequestId": "set-request"}}


class ProviderError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Authorization=secret-value")
        self.response = {
            "Error": {
                "Code": "Throttling",
                "Message": "Authorization=secret-value request throttled",
            }
        }


def adapter(*, mode=ExecutionMode.DRY_RUN, client=None, ceiling=3, simulated=1):
    return AutoScalingCapacityAdapter(
        execution_mode=mode,
        target_resource="pulse-asg",
        region="ap-south-1" if mode is ExecutionMode.LIVE else None,
        global_ceiling=ceiling,
        simulated_capacity=simulated,
        client=client,
    )


def test_dry_run_is_credential_free_and_never_calls_client() -> None:
    client = FakeAutoScalingClient()
    capacity = adapter(client=client)

    observation = asyncio.run(capacity.read_capacity(observed_at=NOW))
    decision = asyncio.run(
        capacity.execute(requested_capacity=2, effective_ceiling=3, observed_at=NOW)
    )

    assert observation.state.provider_status is ProviderStatus.SIMULATED
    assert decision.status is ActionStatus.DRY_RUN
    assert decision.applied == 2
    assert client.describe_calls == 0
    assert client.set_calls == []


def test_dry_run_reports_noop_and_capped_without_mutation() -> None:
    capacity = adapter(simulated=2)
    noop = asyncio.run(
        capacity.execute(requested_capacity=2, effective_ceiling=3, observed_at=NOW)
    )
    capped = asyncio.run(
        capacity.execute(requested_capacity=9, effective_ceiling=2, observed_at=NOW)
    )
    assert noop.status is ActionStatus.NOOP
    assert capped.status is ActionStatus.CAPPED
    assert capped.applied == capped.ceiling == 2


def test_live_mode_reads_then_sets_capacity_and_captures_request_id() -> None:
    client = FakeAutoScalingClient(desired=1)
    capacity = adapter(mode=ExecutionMode.LIVE, client=client)
    decision = asyncio.run(
        capacity.execute(requested_capacity=3, effective_ceiling=3, observed_at=NOW)
    )
    assert decision.status is ActionStatus.SUCCEEDED
    assert decision.provider_request_id == "set-request"
    assert client.describe_calls == 1
    assert client.set_calls == [
        {
            "AutoScalingGroupName": "pulse-asg",
            "DesiredCapacity": 3,
            "HonorCooldown": False,
        }
    ]


def test_live_recovery_decrease_honors_provider_cooldown() -> None:
    client = FakeAutoScalingClient(desired=3)
    decision = asyncio.run(
        adapter(mode=ExecutionMode.LIVE, client=client).execute(
            requested_capacity=2,
            effective_ceiling=3,
            observed_at=NOW,
            intent=ResponseIntent.RECOVER,
        )
    )

    assert decision.status is ActionStatus.SUCCEEDED
    assert client.set_calls == [
        {
            "AutoScalingGroupName": "pulse-asg",
            "DesiredCapacity": 2,
            "HonorCooldown": True,
        }
    ]


def test_live_emergency_scale_out_interrupts_recovery_cooldown() -> None:
    client = FakeAutoScalingClient(desired=3)
    capacity = adapter(mode=ExecutionMode.LIVE, client=client)

    recovery = asyncio.run(
        capacity.execute(
            requested_capacity=2,
            effective_ceiling=3,
            observed_at=NOW,
            intent=ResponseIntent.RECOVER,
        )
    )
    emergency = asyncio.run(
        capacity.execute(
            requested_capacity=3,
            effective_ceiling=3,
            observed_at=NOW,
            intent=ResponseIntent.PROTECT,
        )
    )

    assert recovery.status is ActionStatus.SUCCEEDED
    assert emergency.status is ActionStatus.SUCCEEDED
    assert [call["HonorCooldown"] for call in client.set_calls] == [True, False]


def test_live_successive_scheduled_ramp_steps_bypass_provider_cooldown() -> None:
    client = FakeAutoScalingClient(desired=1)
    capacity = adapter(mode=ExecutionMode.LIVE, client=client)

    first = asyncio.run(
        capacity.execute(
            requested_capacity=2,
            effective_ceiling=3,
            observed_at=NOW,
            intent=ResponseIntent.PREWARM,
        )
    )
    second = asyncio.run(
        capacity.execute(
            requested_capacity=3,
            effective_ceiling=3,
            observed_at=NOW,
            intent=ResponseIntent.PREWARM,
        )
    )

    assert first.status is second.status is ActionStatus.SUCCEEDED
    assert [call["DesiredCapacity"] for call in client.set_calls] == [2, 3]
    assert all(call["HonorCooldown"] is False for call in client.set_calls)


def test_live_adapter_rejects_non_recovery_scale_in_defensively() -> None:
    client = FakeAutoScalingClient(desired=3)
    decision = asyncio.run(
        adapter(mode=ExecutionMode.LIVE, client=client).execute(
            requested_capacity=2,
            effective_ceiling=3,
            observed_at=NOW,
            intent=ResponseIntent.PROTECT,
        )
    )

    assert decision.status is ActionStatus.FAILED
    assert decision.sanitized_error == "non_recovery_scale_in_blocked"
    assert client.set_calls == []


def test_live_mode_applies_internal_second_clamp_and_noops_equal_current() -> None:
    client = FakeAutoScalingClient(desired=2)
    capacity = adapter(mode=ExecutionMode.LIVE, client=client, ceiling=2)
    decision = asyncio.run(
        capacity.execute(requested_capacity=99, effective_ceiling=8, observed_at=NOW)
    )
    assert decision.status is ActionStatus.CAPPED
    assert decision.applied == decision.ceiling == 2
    assert client.set_calls == []


def test_live_mode_sanitizes_throttling_and_does_not_retry() -> None:
    client = FakeAutoScalingClient(desired=1, failure=ProviderError())
    capacity = adapter(mode=ExecutionMode.LIVE, client=client)
    decision = asyncio.run(
        capacity.execute(requested_capacity=2, effective_ceiling=3, observed_at=NOW)
    )
    assert decision.status is ActionStatus.FAILED
    assert decision.applied is None
    assert "Throttling" in decision.sanitized_error
    assert "secret-value" not in decision.sanitized_error
    assert len(client.set_calls) == 1


def test_live_default_client_has_bounded_timeout_and_retry_configuration(
    monkeypatch,
) -> None:
    captured = {}
    client = FakeAutoScalingClient()

    def create_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return client

    monkeypatch.setattr(boto3, "client", create_client)
    AutoScalingCapacityAdapter(
        execution_mode=ExecutionMode.LIVE,
        target_resource="pulse-asg",
        region="ap-south-1",
        global_ceiling=3,
        simulated_capacity=1,
        connect_timeout_seconds=2,
        read_timeout_seconds=5,
        max_attempts=2,
    )

    config = captured["config"]
    assert captured["service"] == "autoscaling"
    assert config.connect_timeout == 2
    assert config.read_timeout == 5
    assert config.retries["total_max_attempts"] == 2
    assert config.retries["mode"] == "standard"


def test_live_mutation_timeout_is_unknown_until_reconciled() -> None:
    client = FakeAutoScalingClient(
        desired=1,
        failure=ReadTimeoutError(endpoint_url="https://autoscaling.example.test"),
    )
    decision = asyncio.run(
        adapter(mode=ExecutionMode.LIVE, client=client).execute(
            requested_capacity=2,
            effective_ceiling=3,
            observed_at=NOW,
        )
    )

    assert decision.status is ActionStatus.UNKNOWN
    assert decision.applied is None
    assert len(client.set_calls) == 1
