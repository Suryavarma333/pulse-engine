from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.contracts import validate_evidence


class DemoAppModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class CheckoutRequest(DemoAppModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, allow_inf_nan=False)

    cart_id: str = Field(default="demo-cart", min_length=1, max_length=100)
    item_count: int = Field(default=1, ge=1, le=100)


class SheddingTransitionRequest(DemoAppModel):
    level: int = Field(ge=0, le=3)
    reason_code: str = Field(min_length=1, max_length=60)
    reasoning: str = Field(min_length=1, max_length=1000)
    changed_by: str = Field(default="agent", pattern="^(agent|operator|system)$")
    signal_evidence: dict[str, Any] = Field(default_factory=dict)
    correlation_id: UUID | None = None
    prediction_id: UUID | None = None
    trigger_snapshot_id: int | None = Field(default=None, ge=1)
    demo_run_id: UUID | None = None

    @field_validator("signal_evidence")
    @classmethod
    def evidence_is_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)


class SheddingStateResponse(DemoAppModel):
    changed: bool | None = None
    event_id: UUID | None
    level: int
    level_name: str
    started_at: datetime
    reason_code: str
    reasoning: str
    endpoint_policies: dict[str, str]


class EndpointMetricResponse(DemoAppModel):
    request_count: int = Field(ge=0)
    p99_latency_ms: float = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)


class PersistenceStatusResponse(DemoAppModel):
    backend: str
    durable: bool
    available: bool
    state_loaded: bool


class TrafficSnapshotResponse(DemoAppModel):
    observed_at: datetime
    window_seconds: int = Field(ge=1)
    origin_request_rate_rps: float = Field(ge=0)
    concurrent_requests: int = Field(ge=0)
    request_count: int = Field(ge=0)
    sample_capacity: int = Field(ge=1)
    samples_dropped: int = Field(ge=0)
    samples_truncated: bool
    error_rate: float = Field(ge=0, le=1)
    p50_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    p99_latency_ms: float = Field(ge=0)
    requests_by_path: dict[str, int]
    endpoints: dict[str, EndpointMetricResponse]
    environment: str
    load_shedding_level: int = Field(ge=0, le=3)
    endpoint_policies: dict[str, str]
    persistence: PersistenceStatusResponse


class ApplicationHealthResponse(DemoAppModel):
    status: str
    service: str
    environment: str
    ready: bool
    persistence: PersistenceStatusResponse
    load_shedding: SheddingStateResponse
    metrics: TrafficSnapshotResponse
    critical_path_protected: bool
