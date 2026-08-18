from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from common.enums import ExecutionMode


class AgentSettings(BaseModel):
    """Validated control-plane settings with safe local defaults."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False
    )

    environment: str = Field(default="local", min_length=1, max_length=32)
    database_url: SecretStr = Field(
        default=SecretStr(
            "postgresql+psycopg://pulse:pulse-local-only@postgres:5432/pulse"
        ),
        min_length=1,
    )
    demo_app_base_url: str = Field(default="http://demo-app:8000", min_length=1)
    dashboard_origins: tuple[str, ...] = ("http://localhost:3000",)
    control_token: SecretStr = Field(default=SecretStr("local-demo-token"))
    execution_mode: ExecutionMode = ExecutionMode.DRY_RUN
    aws_region: str | None = Field(default=None, min_length=1, max_length=32)
    asg_name: str | None = Field(default=None, min_length=1, max_length=255)
    global_instance_ceiling: int = Field(default=3, ge=1, le=100)
    minimum_desired_capacity: int = Field(default=1, ge=0, le=100)
    simulated_desired_capacity: int = Field(default=1, ge=0, le=100)
    metric_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    baseline_window_seconds: int = Field(default=60, ge=5, le=3_600)
    minimum_samples: int = Field(default=5, ge=2, le=1_000)
    acceleration_threshold_rps2: float = Field(default=2.0, gt=0)
    entry_ratio_threshold: float = Field(default=1.5, gt=1)
    exit_ratio_threshold: float = Field(default=1.15, ge=0)
    confidence_threshold: float = Field(default=0.65, ge=0, le=1)
    confirmation_count: int = Field(default=3, ge=1, le=100)
    recovery_low_threshold_rps: float = Field(default=2.0, ge=0)
    recovery_confirmation_count: int = Field(default=3, ge=1, le=100)
    cooldown_seconds: int = Field(default=120, ge=0, le=86_400)
    recovery_decrement_step: int = Field(default=1, ge=1, le=100)
    query_default_window_seconds: int = Field(default=3_600, ge=60, le=86_400)
    query_max_window_seconds: int = Field(default=86_400, ge=60, le=604_800)
    query_max_rows: int = Field(default=1_000, ge=1, le=1_000)
    provider_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    shedding_control_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    reconciliation_min_age_seconds: int = Field(default=30, ge=0, le=86_400)
    reconciliation_batch_size: int = Field(default=50, ge=1, le=1_000)
    optional_signal_freshness_seconds: int = Field(default=15, ge=1, le=3_600)
    origin_signal_freshness_seconds: int = Field(default=10, ge=1, le=3_600)
    signal_buffer_capacity: int = Field(default=1_000, ge=1, le=100_000)
    simulated_signal_max_ttl_seconds: int = Field(default=300, ge=1, le=3_600)
    simulated_signal_future_tolerance_seconds: int = Field(default=5, ge=0, le=300)
    realtime_window_max_samples: int = Field(default=1_000, ge=2, le=100_000)
    baseline_strategy: str = Field(
        default="moving_average", pattern="^(moving_average|exponential)$"
    )
    exponential_smoothing_alpha: float = Field(default=0.35, gt=0, le=1)
    reactive_cpu_threshold_pct: float = Field(default=70.0, ge=0, le=100)
    reactive_load_threshold_rps: float = Field(default=50.0, gt=0)
    forecast_horizon_seconds: int = Field(default=30, ge=1, le=3_600)
    rps_per_instance: float = Field(default=25.0, gt=0)
    scheduled_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    scheduled_lookahead_seconds: int = Field(default=86_400, ge=60, le=604_800)
    scheduled_ramp_steps: int = Field(default=4, ge=2, le=100)
    near_event_protection_seconds: int = Field(default=300, ge=0, le=86_400)
    feedback_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    feedback_horizon_seconds: int = Field(default=30, ge=0, le=86_400)
    status_capacity_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @model_validator(mode="after")
    def validate_safety_bounds(self) -> Self:
        if self.minimum_desired_capacity > self.global_instance_ceiling:
            raise ValueError("minimum_desired_capacity must not exceed global_instance_ceiling")
        if self.simulated_desired_capacity > self.global_instance_ceiling:
            raise ValueError("simulated_desired_capacity must not exceed global_instance_ceiling")
        if self.recovery_decrement_step > self.global_instance_ceiling:
            raise ValueError("recovery_decrement_step must not exceed global_instance_ceiling")
        if self.exit_ratio_threshold >= self.entry_ratio_threshold:
            raise ValueError("exit_ratio_threshold must be lower than entry_ratio_threshold")
        if self.query_default_window_seconds > self.query_max_window_seconds:
            raise ValueError(
                "query_default_window_seconds must not exceed query_max_window_seconds"
            )
        if self.execution_mode is ExecutionMode.LIVE and (not self.aws_region or not self.asg_name):
            raise ValueError("live execution requires aws_region and asg_name")
        if not self.dashboard_origins or any(
            not origin.startswith(("http://", "https://")) or "*" in origin
            for origin in self.dashboard_origins
        ):
            raise ValueError("dashboard_origins must contain explicit HTTP(S) origins")
        return self

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AgentSettings:
        values = environ or os.environ
        field_map: dict[str, str] = {
            "PULSE_ENVIRONMENT": "environment",
            "PULSE_DATABASE_URL": "database_url",
            "PULSE_DEMO_APP_BASE_URL": "demo_app_base_url",
            "PULSE_CONTROL_TOKEN": "control_token",
            "PULSE_EXECUTION_MODE": "execution_mode",
            "AWS_REGION": "aws_region",
            "PULSE_ASG_NAME": "asg_name",
            "PULSE_MAX_INSTANCE_CEILING": "global_instance_ceiling",
            "PULSE_MINIMUM_DESIRED_CAPACITY": "minimum_desired_capacity",
            "PULSE_SIMULATED_DESIRED_CAPACITY": "simulated_desired_capacity",
            "PULSE_METRIC_POLL_SECONDS": "metric_poll_seconds",
            "PULSE_BASELINE_WINDOW_SECONDS": "baseline_window_seconds",
            "PULSE_MINIMUM_SAMPLES": "minimum_samples",
            "PULSE_ACCELERATION_THRESHOLD_RPS2": "acceleration_threshold_rps2",
            "PULSE_ENTRY_RATIO_THRESHOLD": "entry_ratio_threshold",
            "PULSE_EXIT_RATIO_THRESHOLD": "exit_ratio_threshold",
            "PULSE_CONFIDENCE_THRESHOLD": "confidence_threshold",
            "PULSE_CONFIRMATION_COUNT": "confirmation_count",
            "PULSE_RECOVERY_LOW_THRESHOLD_RPS": "recovery_low_threshold_rps",
            "PULSE_RECOVERY_CONFIRMATION_COUNT": "recovery_confirmation_count",
            "PULSE_COOLDOWN_SECONDS": "cooldown_seconds",
            "PULSE_RECOVERY_DECREMENT_STEP": "recovery_decrement_step",
            "PULSE_QUERY_DEFAULT_WINDOW_SECONDS": "query_default_window_seconds",
            "PULSE_QUERY_MAX_WINDOW_SECONDS": "query_max_window_seconds",
            "PULSE_QUERY_MAX_ROWS": "query_max_rows",
            "PULSE_PROVIDER_TIMEOUT_SECONDS": "provider_timeout_seconds",
            "PULSE_SHEDDING_CONTROL_TIMEOUT_SECONDS": "shedding_control_timeout_seconds",
            "PULSE_RECONCILIATION_MIN_AGE_SECONDS": "reconciliation_min_age_seconds",
            "PULSE_RECONCILIATION_BATCH_SIZE": "reconciliation_batch_size",
            "PULSE_OPTIONAL_SIGNAL_FRESHNESS_SECONDS": "optional_signal_freshness_seconds",
            "PULSE_ORIGIN_SIGNAL_FRESHNESS_SECONDS": "origin_signal_freshness_seconds",
            "PULSE_SIGNAL_BUFFER_CAPACITY": "signal_buffer_capacity",
            "PULSE_SIMULATED_SIGNAL_MAX_TTL_SECONDS": "simulated_signal_max_ttl_seconds",
            "PULSE_SIMULATED_SIGNAL_FUTURE_TOLERANCE_SECONDS": (
                "simulated_signal_future_tolerance_seconds"
            ),
            "PULSE_REALTIME_WINDOW_MAX_SAMPLES": "realtime_window_max_samples",
            "PULSE_BASELINE_STRATEGY": "baseline_strategy",
            "PULSE_EXPONENTIAL_SMOOTHING_ALPHA": "exponential_smoothing_alpha",
            "PULSE_REACTIVE_CPU_THRESHOLD_PCT": "reactive_cpu_threshold_pct",
            "PULSE_REACTIVE_LOAD_THRESHOLD_RPS": "reactive_load_threshold_rps",
            "PULSE_FORECAST_HORIZON_SECONDS": "forecast_horizon_seconds",
            "PULSE_RPS_PER_INSTANCE": "rps_per_instance",
            "PULSE_SCHEDULED_POLL_SECONDS": "scheduled_poll_seconds",
            "PULSE_SCHEDULED_LOOKAHEAD_SECONDS": "scheduled_lookahead_seconds",
            "PULSE_SCHEDULED_RAMP_STEPS": "scheduled_ramp_steps",
            "PULSE_NEAR_EVENT_PROTECTION_SECONDS": "near_event_protection_seconds",
            "PULSE_FEEDBACK_POLL_SECONDS": "feedback_poll_seconds",
            "PULSE_FEEDBACK_HORIZON_SECONDS": "feedback_horizon_seconds",
            "PULSE_STATUS_CAPACITY_TIMEOUT_SECONDS": "status_capacity_timeout_seconds",
        }
        payload = {field: values[key] for key, field in field_map.items() if key in values}
        if "PULSE_DASHBOARD_ORIGINS" in values:
            payload["dashboard_origins"] = tuple(
                origin.strip()
                for origin in values["PULSE_DASHBOARD_ORIGINS"].split(",")
                if origin.strip()
            )
        return cls.model_validate(payload)


__all__ = ["AgentSettings"]
