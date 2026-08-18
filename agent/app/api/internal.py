from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from agent.app.metrics.providers.simulated import (
    DuplicateSignalError,
    ExpiredSignalError,
    SignalBufferFullError,
    SimulatedSignalBuffer,
    SimulatedSignalFrame,
)
from common.contracts import ContractModel
from common.enums import ProviderStatus
from common.time import Clock, SystemClock

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
    app.include_router(router)
    return app


__all__ = [
    "SimulatedSignalRequest",
    "SimulatedSignalResponse",
    "create_simulated_signal_app",
    "ingest_simulated_signal",
    "router",
]
