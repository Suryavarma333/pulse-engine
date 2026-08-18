from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Request, status

from common.enums import SheddingLevel
from demo_app.app.schemas import SheddingStateResponse, SheddingTransitionRequest
from demo_app.app.shedding.state import SheddingState, TransitionResult

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


@router.get("/health")
async def health(request: Request) -> dict:
    state = request.app.state.shedding_controller.current()
    metrics = await request.app.state.traffic_metrics.snapshot()
    return {
        "status": "ok",
        "service": "pulse-protected-demo-app",
        "environment": request.app.state.settings.environment,
        "load_shedding": _serialize_state(state).model_dump(mode="json"),
        "metrics": metrics,
        "critical_path_protected": True,
    }


@router.get("/metrics/snapshot")
async def metrics_snapshot(request: Request) -> dict:
    state = request.app.state.shedding_controller.current()
    snapshot = await request.app.state.traffic_metrics.snapshot()
    return {
        **snapshot,
        "environment": request.app.state.settings.environment,
        "load_shedding_level": int(state.level),
        "endpoint_policies": state.policy,
    }


@router.post("/internal/load-shedding", response_model=SheddingStateResponse)
async def set_load_shedding_level(
    payload: SheddingTransitionRequest,
    request: Request,
    control_token: str | None = Header(default=None, alias="X-Pulse-Control-Token"),
) -> SheddingStateResponse:
    expected_token = request.app.state.settings.control_token
    if control_token is None or not secrets.compare_digest(control_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid control token"
        )

    result: TransitionResult = await request.app.state.shedding_controller.transition(
        level=SheddingLevel(payload.level),
        reason_code=payload.reason_code,
        reasoning=payload.reasoning,
        signal_evidence=payload.signal_evidence,
        changed_by=payload.changed_by,
        correlation_id=payload.correlation_id,
        prediction_id=payload.prediction_id,
        trigger_snapshot_id=payload.trigger_snapshot_id,
    )
    return _serialize_state(result.state, changed=result.changed)
