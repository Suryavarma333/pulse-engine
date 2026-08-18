from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ReadTimeoutError

from agent.app.aws.autoscaling import AutoScalingCapacityAdapter
from common.enums import ActionStatus, ExecutionMode, ProviderStatus

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
            "HonorCooldown": True,
        }
    ]


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
