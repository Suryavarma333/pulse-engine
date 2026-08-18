from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse

from common.enums import SheddingLevel
from demo_app.app.schemas import (
    ApplicationHealthResponse,
    PersistenceStatusResponse,
    SheddingStateResponse,
    SheddingTransitionRequest,
    TrafficSnapshotResponse,
)
from demo_app.app.shedding.state import SheddingState, TransitionResult
from demo_app.app.shedding.store import (
    AuditPersistenceUnavailableError,
    TransitionConflictError,
)

router = APIRouter(tags=["operations"])


def _serialize_state(state: SheddingState, *, changed: bool | None = None) -> SheddingStateResponse:
    return SheddingStateResponse(
        changed=changed,
        event_id=state.event_id,
        level=int(state.level),
        level_name=state.level.name.lower(),
        started_at=state.started_at,
        reason_code=state.reason_code,
        reasoning=state.reasoning,
        endpoint_policies=state.policy,
    )


def _serialize_persistence(request: Request) -> PersistenceStatusResponse:
    persistence = request.app.state.shedding_controller.persistence_status()
    return PersistenceStatusResponse(
        backend=persistence.backend,
        durable=persistence.durable,
        available=persistence.available,
        state_loaded=persistence.state_loaded,
    )


async def _metrics_snapshot(request: Request) -> TrafficSnapshotResponse:
    state = request.app.state.shedding_controller.current()
    snapshot = await request.app.state.traffic_metrics.snapshot()
    return TrafficSnapshotResponse(
        **snapshot,
        environment=request.app.state.settings.environment,
        load_shedding_level=int(state.level),
        endpoint_policies=state.policy,
        persistence=_serialize_persistence(request),
    )


@router.get(
    "/health",
    response_model=ApplicationHealthResponse,
    responses={503: {"model": ApplicationHealthResponse}},
)
async def health(request: Request, response: Response) -> ApplicationHealthResponse:
    state = request.app.state.shedding_controller.current()
    persistence = _serialize_persistence(request)
    ready = persistence.available and persistence.state_loaded
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ApplicationHealthResponse(
        status="ok" if ready else "degraded",
        service="pulse-protected-demo-app",
        environment=request.app.state.settings.environment,
        ready=ready,
        persistence=persistence,
        load_shedding=_serialize_state(state),
        metrics=await _metrics_snapshot(request),
        critical_path_protected=True,
    )


@router.get("/metrics/snapshot", response_model=TrafficSnapshotResponse)
async def metrics_snapshot(request: Request) -> TrafficSnapshotResponse:
    return await _metrics_snapshot(request)


def _control_error(
    *, status_code: int, code: str, detail: str, correlation_id: str | None = None
) -> JSONResponse:
    body = {"detail": detail, "code": code}
    if correlation_id is not None:
        body["correlation_id"] = correlation_id
    return JSONResponse(status_code=status_code, content=body)


def _tokens_match(provided: str, expected: str) -> bool:
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


@router.post("/internal/load-shedding", response_model=SheddingStateResponse)
async def set_load_shedding_level(
    payload: SheddingTransitionRequest,
    request: Request,
    control_token: str | None = Header(default=None, alias="X-Pulse-Control-Token"),
) -> SheddingStateResponse | JSONResponse:
    expected_token = request.app.state.settings.control_token
    if control_token is None or not _tokens_match(control_token, expected_token):
        return _control_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_CONTROL_TOKEN",
            detail="Invalid control token",
        )

    try:
        result: TransitionResult = await request.app.state.shedding_controller.transition(
            level=SheddingLevel(payload.level),
            reason_code=payload.reason_code,
            reasoning=payload.reasoning,
            signal_evidence=payload.signal_evidence,
            changed_by=payload.changed_by,
            correlation_id=payload.correlation_id,
            prediction_id=payload.prediction_id,
            trigger_snapshot_id=payload.trigger_snapshot_id,
            demo_run_id=payload.demo_run_id,
        )
    except TransitionConflictError:
        return _control_error(
            status_code=status.HTTP_409_CONFLICT,
            code="TRANSITION_CONFLICT",
            detail="The authoritative load-shedding tier changed; retry from current state",
            correlation_id=str(payload.correlation_id) if payload.correlation_id else None,
        )
    except AuditPersistenceUnavailableError:
        return _control_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="AUDIT_PERSISTENCE_UNAVAILABLE",
            detail="The load-shedding transition could not be audited",
            correlation_id=str(payload.correlation_id) if payload.correlation_id else None,
        )
    return _serialize_state(result.state, changed=result.changed)
