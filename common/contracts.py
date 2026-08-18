from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.enums import (
    ActionStatus,
    ControlState,
    DemoRunStatus,
    ExecutionMode,
    PredictionMode,
    ProviderStatus,
    SheddingLevel,
)
from common.time import ensure_utc

MAX_EVIDENCE_BYTES = 16_384
MAX_EVIDENCE_KEYS = 64
_SENSITIVE_KEY_PARTS = ("password", "secret", "token", "credential", "cart_id", "user_id")


def validate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if len(value) > MAX_EVIDENCE_KEYS:
        raise ValueError(f"evidence must contain at most {MAX_EVIDENCE_KEYS} top-level keys")
    _reject_sensitive_keys(value)
    try:
        encoded = json.dumps(value, default=_reject_non_json, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"evidence must be at most {MAX_EVIDENCE_BYTES} encoded bytes")
    return value


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if any(part in key for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(f"evidence key {key!r} may contain sensitive or shopper data")
            _reject_sensitive_keys(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _reject_sensitive_keys(child)


def _reject_non_json(value: object) -> object:
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False
    )


class SignalReading(ContractModel):
    source: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    status: ProviderStatus
    values: dict[str, float] = Field(default_factory=dict, max_length=32)
    freshness_seconds: float = Field(ge=0)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value, field_name="observed_at")

    @field_validator("details")
    @classmethod
    def details_are_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)


class RecoveryPlan(ContractModel):
    low_threshold_rps: float = Field(ge=0)
    confirmation_count: int = Field(ge=1, le=100)
    cooldown_seconds: int = Field(ge=0, le=86_400)
    decrement_step: int = Field(ge=1, le=100)
    capacity_floor: int = Field(ge=0, le=100)


class SurgePredictionCandidate(ContractModel):
    correlation_id: UUID = Field(default_factory=uuid4)
    demo_run_id: UUID | None = None
    mode: PredictionMode
    environment: str = Field(min_length=1, max_length=32)
    scheduled_event_id: UUID | None = None
    trigger_snapshot_id: int | None = Field(default=None, ge=1)
    basis_window_start: datetime
    basis_window_end: datetime
    predicted_start_at: datetime
    predicted_peak_at: datetime
    predicted_end_at: datetime | None = None
    baseline_rps: float = Field(ge=0)
    predicted_peak_rps: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    recommended_capacity: int = Field(ge=0)
    reason_code: str = Field(min_length=1, max_length=60)
    reasoning: str = Field(min_length=1, max_length=2_000)
    signal_evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "basis_window_start",
        "basis_window_end",
        "predicted_start_at",
        "predicted_peak_at",
        "predicted_end_at",
    )
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("signal_evidence")
    @classmethod
    def evidence_is_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.basis_window_end <= self.basis_window_start:
            raise ValueError("basis_window_end must be after basis_window_start")
        if self.predicted_peak_at < self.predicted_start_at:
            raise ValueError("predicted_peak_at must not precede predicted_start_at")
        if self.predicted_end_at is not None and self.predicted_end_at < self.predicted_peak_at:
            raise ValueError("predicted_end_at must not precede predicted_peak_at")
        return self


class ResponseCommand(ContractModel):
    command_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9:._-]+$")
    correlation_id: UUID
    demo_run_id: UUID | None = None
    prediction_id: UUID | None = None
    trigger_snapshot_id: int | None = Field(default=None, ge=1)
    scheduled_event_id: UUID | None = None
    mode: PredictionMode
    target_resource: str = Field(min_length=1, max_length=255)
    requested_desired_capacity: int = Field(ge=0)
    maximum_ceiling: int = Field(ge=1, le=100)
    requested_shedding_level: SheddingLevel | None = None
    reason_code: str = Field(min_length=1, max_length=60)
    reasoning: str = Field(min_length=1, max_length=2_000)
    signal_evidence: dict[str, Any] = Field(default_factory=dict)
    recovery_plan: RecoveryPlan

    @field_validator("signal_evidence")
    @classmethod
    def evidence_is_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)

    @model_validator(mode="after")
    def recovery_floor_respects_ceiling(self) -> Self:
        if self.recovery_plan.capacity_floor > self.maximum_ceiling:
            raise ValueError("recovery capacity_floor must not exceed maximum_ceiling")
        return self


class CapacityState(ContractModel):
    desired: int = Field(ge=0)
    in_service: int = Field(ge=0)
    pending: int = Field(ge=0)
    observed_at: datetime
    provider_status: ProviderStatus

    @field_validator("observed_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class CapacityDecision(ContractModel):
    requested: int = Field(ge=0)
    applied: int | None = Field(default=None, ge=0)
    ceiling: int = Field(ge=1, le=100)
    execution_mode: ExecutionMode
    status: ActionStatus
    provider_request_id: str | None = Field(default=None, max_length=160)
    sanitized_error: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def applied_capacity_is_safe(self) -> Self:
        if self.applied is not None and self.applied > self.ceiling:
            raise ValueError("applied capacity must not exceed ceiling")
        return self


class WorkerHealth(ContractModel):
    name: str = Field(min_length=1, max_length=80)
    status: ProviderStatus
    checked_at: datetime
    detail: str | None = Field(default=None, max_length=500)

    @field_validator("checked_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class OperationalStatus(ContractModel):
    environment: str = Field(min_length=1, max_length=32)
    execution_mode: ExecutionMode
    state: ControlState
    global_ceiling: int = Field(ge=1, le=100)
    current_capacity: CapacityState | None = None
    shedding_level: SheddingLevel
    workers: tuple[WorkerHealth, ...] = ()
    cooldown_until: datetime | None = None

    @field_validator("cooldown_until")
    @classmethod
    def timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class DemoRunSpec(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    scenario_name: str = Field(min_length=1, max_length=120)
    mode: PredictionMode
    environment: str = Field(min_length=1, max_length=32)
    baseline_type: str = Field(min_length=1, max_length=40)
    execution_mode: ExecutionMode
    configuration: dict[str, Any] = Field(default_factory=dict)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("configuration", "thresholds")
    @classmethod
    def mappings_are_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)


class ResultMetrics(ContractModel):
    checkout_p99_latency_ms: float | None = Field(default=None, ge=0)
    checkout_success_rate: float | None = Field(default=None, ge=0, le=1)
    detection_lead_seconds: float | None = None
    provisioning_efficiency_pct: float | None = Field(default=None, ge=0, le=100)
    prediction_error_pct: float | None = Field(default=None, ge=0)
    overprovisioned_instance_minutes: float | None = Field(default=None, ge=0)
    underprovisioned_seconds: float | None = Field(default=None, ge=0)
    recovery_duration_seconds: float | None = Field(default=None, ge=0)
    cost_duration_seconds: float | None = Field(default=None, ge=0)
    formula_version: str = Field(default="v1", min_length=1, max_length=20)
    warnings: tuple[str, ...] = ()


class DemoRunState(ContractModel):
    id: UUID
    spec: DemoRunSpec
    status: DemoRunStatus
    ended_at: datetime | None = None
    results: ResultMetrics | None = None

    @field_validator("ended_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @model_validator(mode="after")
    def terminal_time_is_ordered(self) -> Self:
        if self.ended_at is not None and self.ended_at < self.spec.started_at:
            raise ValueError("ended_at must not precede started_at")
        return self


class QueryWindow(ContractModel):
    start: datetime
    end: datetime
    limit: int = Field(default=200, ge=1, le=1_000)

    @field_validator("start", "end")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("query end must be after start")
        return self
