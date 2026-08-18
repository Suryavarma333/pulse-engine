from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, field_validator

from agent.app.metrics.providers.simulated import (
    DuplicateSignalError,
    ExpiredSignalError,
    SignalBufferFullError,
    SimulatedSignalBuffer,
    SimulatedSignalFrame,
)
from common.contracts import ContractModel, DemoRunSpec, validate_evidence
from common.enums import DemoRunStatus, ExecutionMode, PredictionMode, ProviderStatus
from common.time import Clock, SystemClock, ensure_utc
from db.repositories.demo_runs import IdempotencyConflict

router = APIRouter(prefix="/internal", tags=["internal-signals"])


class SimulatedSignalRequest(ContractModel):
    source: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    demo_run_id: UUID | None = None
    ttl_seconds: int = Field(ge=1, le=3_600)
    edge_request_rate_rps: float | None = Field(default=None, ge=0, le=10_000_000)
    queue_depth: int | None = Field(default=None, ge=0, le=1_000_000_000)
    concurrent_sessions: int | None = Field(default=None, ge=0, le=10_000_000)
    login_rate_rps: float | None = Field(default=None, ge=0, le=10_000_000)
    cpu_utilization_pct: float | None = Field(default=None, ge=0, le=100)


class SimulatedSignalResponse(ContractModel):
    source: str
    observed_at: datetime
    demo_run_id: UUID | None
    expires_at: datetime
    accepted_values: dict[str, float]
    provider_status: ProviderStatus


class DemoRunStartRequest(ContractModel):
    scenario_name: str = Field(min_length=1, max_length=120)
    mode: PredictionMode
    environment: str = Field(min_length=1, max_length=32)
    baseline_type: str = Field(min_length=1, max_length=40)
    execution_mode: ExecutionMode
    configuration: dict[str, Any] = Field(default_factory=dict)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    pair_group_id: str | None = Field(default=None, min_length=1, max_length=160)
    started_at: datetime | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("started_at")
    @classmethod
    def started_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("configuration", "thresholds")
    @classmethod
    def evidence_is_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)


class DemoRunCompletionRequest(ContractModel):
    status: DemoRunStatus
    ended_at: datetime
    locust_summary: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=2_000)

    @field_validator("ended_at")
    @classmethod
    def ended_at_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("status")
    @classmethod
    def status_is_terminal(cls, value: DemoRunStatus) -> DemoRunStatus:
        if value not in {
            DemoRunStatus.COMPLETED,
            DemoRunStatus.FAILED,
            DemoRunStatus.CANCELLED,
        }:
            raise ValueError("status must be completed, failed, or cancelled")
        return value

    @field_validator("locust_summary")
    @classmethod
    def summary_is_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_evidence(value)


def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


def _expected_token(request: Request) -> str:
    raw: Any = request.app.state.control_token
    return raw.get_secret_value() if isinstance(raw, SecretStr) else str(raw)


def _tokens_match(provided: str, expected: str) -> bool:
    return secrets.compare_digest(provided.encode(), expected.encode())


@router.post(
    "/signals/simulated",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SimulatedSignalResponse,
)
async def ingest_simulated_signal(
    payload: SimulatedSignalRequest,
    request: Request,
    control_token: str | None = Header(default=None, alias="X-Pulse-Control-Token"),
) -> SimulatedSignalResponse | JSONResponse:
    if control_token is None or not _tokens_match(control_token, _expected_token(request)):
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_CONTROL_TOKEN",
            "Invalid control token",
        )
    if not getattr(request.app.state, "worker_available", False):
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "REALTIME_WORKER_UNAVAILABLE",
            "The real-time worker is unavailable",
        )
    clock: Clock = request.app.state.clock
    buffer: SimulatedSignalBuffer = request.app.state.simulated_signal_buffer
    try:
        frame = SimulatedSignalFrame.model_validate(payload.model_dump())
        await buffer.add(frame, now=clock.now())
    except DuplicateSignalError:
        return _error(status.HTTP_409_CONFLICT, "DUPLICATE_SIGNAL", "Signal already accepted")
    except SignalBufferFullError:
        return _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "SIGNAL_BUFFER_FULL",
            "The bounded signal buffer is full",
        )
    except ExpiredSignalError:
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "EXPIRED_SIGNAL",
            "Signal expired before ingestion",
        )
    except ValueError as exc:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "INVALID_SIGNAL", str(exc))
    return SimulatedSignalResponse(
        source=frame.source,
        observed_at=frame.observed_at,
        demo_run_id=frame.demo_run_id,
        expires_at=frame.expires_at,
        accepted_values=frame.values(),
        provider_status=ProviderStatus.SIMULATED,
    )


@router.post("/demo-runs", response_model=None)
async def start_demo_run(
    payload: DemoRunStartRequest,
    request: Request,
    response: Response,
    control_token: str | None = Header(default=None, alias="X-Pulse-Control-Token"),
) -> dict[str, Any] | JSONResponse:
    if control_token is None or not _tokens_match(control_token, _expected_token(request)):
        return _error(
            status.HTTP_401_UNAUTHORIZED, "INVALID_CONTROL_TOKEN", "Invalid control token"
        )
    started_at = payload.started_at or request.app.state.clock.now()
    settings = request.app.state.settings
    workload_configuration = {
        key: payload.configuration[key]
        for key in ("seed", "duration_seconds", "host", "endpoint_weights")
        if key in payload.configuration
    }
    effective_configuration = {
        **workload_configuration,
        "pair_group_id": payload.pair_group_id or f"run:{payload.idempotency_key}",
        "settings_snapshot_version": "v1",
        "rps_per_instance": settings.rps_per_instance,
        "minimum_desired_capacity": settings.minimum_desired_capacity,
        "global_instance_ceiling": settings.global_instance_ceiling,
        "target_resource": settings.asg_name or "local-simulated-asg",
        "effective_control": {
            "baseline_strategy": settings.baseline_strategy,
            "baseline_window_seconds": settings.baseline_window_seconds,
            "minimum_samples": settings.minimum_samples,
            "maximum_samples": settings.realtime_window_max_samples,
            "metric_poll_seconds": settings.metric_poll_seconds,
            "recovery_low_threshold_rps": settings.recovery_low_threshold_rps,
            "recovery_confirmation_count": settings.recovery_confirmation_count,
            "recovery_decrement_step": settings.recovery_decrement_step,
            "cooldown_seconds": settings.cooldown_seconds,
            "forecast_horizon_seconds": settings.forecast_horizon_seconds,
            "optional_signal_freshness_seconds": (
                settings.optional_signal_freshness_seconds
            ),
            "origin_signal_freshness_seconds": settings.origin_signal_freshness_seconds,
            "scheduled_lookahead_seconds": settings.scheduled_lookahead_seconds,
            "scheduled_ramp_steps": settings.scheduled_ramp_steps,
            "near_event_protection_seconds": settings.near_event_protection_seconds,
            "providers": {
                "cloudwatch_cpu": settings.cloudwatch_cpu_enabled,
                "cloudfront": bool(settings.cloudfront_distribution_id),
                "cloudfront_region": (
                    "us-east-1" if settings.cloudfront_distribution_id else None
                ),
                "workload_region": settings.aws_region,
                "sqs": bool(settings.sqs_queue_url),
                "sessions": bool(settings.session_signal_url),
                "simulated": True,
            },
        },
    }
    effective_thresholds = {
        "onset_ratio": 1.2,
        "comparator": "cpu_or_configured_reactive_load",
        "acceleration_threshold_rps2": settings.acceleration_threshold_rps2,
        "entry_ratio_threshold": settings.entry_ratio_threshold,
        "exit_ratio_threshold": settings.exit_ratio_threshold,
        "confidence_threshold": settings.confidence_threshold,
        "confirmation_count": settings.confirmation_count,
        "reactive_cpu_threshold_pct": settings.reactive_cpu_threshold_pct,
        "reactive_load_threshold_rps": settings.reactive_load_threshold_rps,
    }
    spec = DemoRunSpec(
        idempotency_key=payload.idempotency_key,
        scenario_name=payload.scenario_name,
        mode=payload.mode,
        environment=payload.environment,
        baseline_type=payload.baseline_type,
        execution_mode=settings.execution_mode,
        configuration=effective_configuration,
        thresholds=effective_thresholds,
        started_at=started_at,
    )
    try:
        run, created = await request.app.state.demo_runs.start(spec)
    except IdempotencyConflict as exc:
        return _error(status.HTTP_409_CONFLICT, "IDEMPOTENCY_CONFLICT", str(exc))
    except Exception as exc:
        code = "ACTIVE_RUN_CONFLICT" if _is_integrity_error(exc) else "RUN_PERSISTENCE_UNAVAILABLE"
        status_code = (
            status.HTTP_409_CONFLICT
            if code == "ACTIVE_RUN_CONFLICT"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return _error(status_code, code, "Unable to start demo run")
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _run_response(run)


@router.patch("/demo-runs/{run_id}", response_model=None)
async def complete_demo_run(
    run_id: UUID,
    payload: DemoRunCompletionRequest,
    request: Request,
    control_token: str | None = Header(default=None, alias="X-Pulse-Control-Token"),
) -> dict[str, Any] | JSONResponse:
    if control_token is None or not _tokens_match(control_token, _expected_token(request)):
        return _error(
            status.HTTP_401_UNAUTHORIZED, "INVALID_CONTROL_TOKEN", "Invalid control token"
        )
    run = await request.app.state.demo_runs.get(run_id)
    if run is None:
        return _error(status.HTTP_404_NOT_FOUND, "RUN_NOT_FOUND", "Demo run was not found")
    if (
        run.status == payload.status.value
        and run.ended_at == payload.ended_at
        and run.locust_summary == payload.locust_summary
        and run.notes == payload.notes
    ):
        return _run_response(run)
    target = (
        DemoRunStatus.PENDING_EVALUATION
        if payload.status is DemoRunStatus.COMPLETED
        else payload.status
    )
    try:
        run = await request.app.state.demo_runs.complete(
            run_id=run_id,
            status=target,
            ended_at=payload.ended_at,
            locust_summary=payload.locust_summary,
            notes=payload.notes,
        )
        if (
            payload.status is DemoRunStatus.COMPLETED
            and (
                request.app.state.clock.now() - payload.ended_at
            ).total_seconds() >= request.app.state.settings.feedback_horizon_seconds
        ):
            run = await request.app.state.feedback_service.evaluate_run(run_id)
    except IdempotencyConflict as exc:
        return _error(status.HTTP_409_CONFLICT, "RUN_COMPLETION_CONFLICT", str(exc))
    except LookupError:
        return _error(status.HTTP_404_NOT_FOUND, "RUN_NOT_FOUND", "Demo run was not found")
    except ValueError as exc:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "INVALID_RUN_COMPLETION", str(exc))
    except Exception:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "RUN_EVALUATION_UNAVAILABLE",
            "Unable to persist or evaluate demo run",
        )
    return _run_response(run)


def _run_response(run: Any) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "scenario_name": run.scenario_name,
        "mode": run.mode,
        "environment": run.environment,
        "baseline_type": run.baseline_type,
        "execution_mode": run.execution_mode,
        "status": run.status,
        "started_at": ensure_utc(run.started_at).isoformat(),
        "ended_at": None if run.ended_at is None else ensure_utc(run.ended_at).isoformat(),
        "formula_version": run.formula_version,
        "warnings": run.warnings,
        "results": run.result_summary,
    }


def _is_integrity_error(exc: Exception) -> bool:
    return type(exc).__name__ == "IntegrityError"


def create_simulated_signal_app(
    *,
    buffer: SimulatedSignalBuffer,
    control_token: str | SecretStr,
    clock: Clock | None = None,
    worker_available: bool = True,
) -> FastAPI:
    """Create a focused Phase-3 API app; the complete agent lifespan is wired in Phase 5."""

    app = FastAPI(title="Pulse internal signal API")
    app.state.simulated_signal_buffer = buffer
    app.state.control_token = control_token
    app.state.clock = clock or SystemClock()
    app.state.worker_available = worker_available
    app.state.demo_runs = None
    app.include_router(router)
    return app


__all__ = [
    "SimulatedSignalRequest",
    "SimulatedSignalResponse",
    "DemoRunCompletionRequest",
    "DemoRunStartRequest",
    "complete_demo_run",
    "create_simulated_signal_app",
    "ingest_simulated_signal",
    "router",
    "start_demo_run",
]
