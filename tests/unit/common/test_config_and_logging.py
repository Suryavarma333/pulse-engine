from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from agent.app.config import AgentSettings
from common.enums import ExecutionMode
from common.logging import JsonFormatter


def test_agent_settings_default_to_bounded_dry_run() -> None:
    settings = AgentSettings()

    assert settings.execution_mode is ExecutionMode.DRY_RUN
    assert settings.global_instance_ceiling == 3
    assert settings.minimum_desired_capacity == 1
    assert settings.shedding_control_timeout_seconds == 2
    assert settings.reconciliation_min_age_seconds == 30
    assert settings.reconciliation_batch_size == 50
    assert settings.snapshot_retention_seconds == 604_800
    assert settings.snapshot_retention_batch_size == 500
    assert settings.aws_sdk_read_timeout_seconds == 3
    assert settings.aws_sdk_max_attempts == 3
    assert "local-demo-token" not in repr(settings)


def test_live_mode_requires_explicit_target_and_region() -> None:
    with pytest.raises(ValidationError, match="live execution requires"):
        AgentSettings(execution_mode=ExecutionMode.LIVE)

    live = AgentSettings(
        execution_mode=ExecutionMode.LIVE,
        aws_region="ap-south-1",
        asg_name="pulse-demo",
    )
    assert live.execution_mode is ExecutionMode.LIVE


def test_aws_signal_sources_require_live_mode_and_valid_urls() -> None:
    with pytest.raises(ValidationError, match="AWS signal providers require"):
        AgentSettings(cloudwatch_cpu_enabled=True)
    with pytest.raises(ValidationError, match="HTTPS"):
        AgentSettings(
            execution_mode=ExecutionMode.LIVE,
            aws_region="ap-south-1",
            asg_name="pulse-demo",
            sqs_queue_url="http://unsafe.example/queue",
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"global_instance_ceiling": 2, "minimum_desired_capacity": 3}, "minimum"),
        ({"global_instance_ceiling": 2, "simulated_desired_capacity": 3}, "simulated"),
        ({"entry_ratio_threshold": 1.2, "exit_ratio_threshold": 1.2}, "exit_ratio"),
        ({"query_default_window_seconds": 3600, "query_max_window_seconds": 600}, "query"),
    ],
)
def test_agent_settings_reject_invalid_safety_bounds(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        AgentSettings(**values)


def test_agent_settings_reject_non_finite_thresholds() -> None:
    with pytest.raises(ValidationError, match="finite number"):
        AgentSettings(acceleration_threshold_rps2=float("inf"))


def test_agent_settings_parse_environment_without_exposing_token() -> None:
    settings = AgentSettings.from_environment(
        {
            "PULSE_EXECUTION_MODE": "live",
            "AWS_REGION": "ap-south-1",
            "PULSE_ASG_NAME": "pulse-demo",
            "PULSE_MAX_INSTANCE_CEILING": "4",
            "PULSE_CONTROL_TOKEN": "runtime-only-token",
            "PULSE_SHEDDING_CONTROL_TIMEOUT_SECONDS": "3",
            "PULSE_RECONCILIATION_MIN_AGE_SECONDS": "45",
            "PULSE_RECONCILIATION_BATCH_SIZE": "25",
            "PULSE_DASHBOARD_ORIGINS": "https://pulse.example,http://localhost:3000",
        }
    )

    assert settings.execution_mode is ExecutionMode.LIVE
    assert settings.global_instance_ceiling == 4
    assert settings.control_token.get_secret_value() == "runtime-only-token"
    assert settings.shedding_control_timeout_seconds == 3
    assert settings.reconciliation_min_age_seconds == 45
    assert settings.reconciliation_batch_size == 25
    assert settings.dashboard_origins == (
        "https://pulse.example",
        "http://localhost:3000",
    )
    assert "runtime-only-token" not in repr(settings)


def test_blank_compose_optional_provider_values_remain_unconfigured() -> None:
    settings = AgentSettings.from_environment(
        {
            "PULSE_EXECUTION_MODE": "dry_run",
            "AWS_REGION": "",
            "PULSE_ASG_NAME": "",
            "PULSE_CLOUDFRONT_DISTRIBUTION_ID": "",
            "PULSE_SQS_QUEUE_URL": "",
            "PULSE_SESSION_SIGNAL_URL": "",
        }
    )

    assert settings.aws_region is None
    assert settings.asg_name is None
    assert settings.cloudfront_distribution_id is None


def test_agent_settings_reject_wildcard_dashboard_origin() -> None:
    with pytest.raises(ValidationError, match="explicit HTTP"):
        AgentSettings(dashboard_origins=("*",))


def test_json_formatter_emits_machine_readable_context() -> None:
    record = logging.LogRecord(
        name="pulse.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="capacity_capped",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "correlation-1"
    record.requested_capacity = 8
    record.applied_capacity = 3
    record.provider = {"request_id": "request-1", "credential_hint": "do-not-log"}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "capacity_capped"
    assert payload["level"] == "warning"
    assert payload["correlation_id"] == "correlation-1"
    assert payload["applied_capacity"] == 3
    assert payload["provider"]["request_id"] == "request-1"
    assert payload["provider"]["credential_hint"] == "[REDACTED]"
