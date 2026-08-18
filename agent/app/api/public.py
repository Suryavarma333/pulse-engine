from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from agent.app.services.feedback import FeedbackEvaluator
from common.contracts import QueryWindow
from common.time import ensure_utc
from db.repositories.operator import InvalidCursor

router = APIRouter(prefix="/api/v1", tags=["operator"])

FromDate = Annotated[datetime | None, Query(alias="from")]
PageLimit = Annotated[int | None, Query(ge=1, le=1_000)]
PageCursor = Annotated[str | None, Query(max_length=1_024)]
PredictionStatusFilter = Annotated[
    str | None,
    Query(alias="status", pattern="^(active|evaluating|completed|cancelled)$"),
]
ActionStatusFilter = Annotated[
    str | None,
    Query(
        alias="status",
        pattern="^(planned|dry_run|noop|capped|skipped|succeeded|failed|unknown|reconciled)$",
    ),
]
ScheduledStatusFilter = Annotated[
    str | None,
    Query(alias="status", pattern="^(draft|active|completed|cancelled)$"),
]
PredictionModeFilter = Annotated[
    str | None, Query(pattern="^(scheduled|realtime)$")
]
ExecutionModeFilter = Annotated[str | None, Query(pattern="^(dry_run|live)$")]


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"detail": detail, "code": code})


def _window(
    request: Request,
    start: datetime | None,
    end: datetime | None,
    limit: int | None,
) -> QueryWindow:
    settings = request.app.state.settings
    if (start is None) != (end is None):
        raise _error(422, "INVALID_QUERY_WINDOW", "from and to must be supplied together")
    if start is None:
        end = request.app.state.clock.now()
        start = end - timedelta(seconds=settings.query_default_window_seconds)
    resolved_limit = min(200, settings.query_max_rows) if limit is None else limit
    if resolved_limit > settings.query_max_rows:
        raise _error(
            422,
            "QUERY_LIMIT_EXCEEDED",
            f"limit must not exceed {settings.query_max_rows}",
        )
    try:
        window = QueryWindow(start=start, end=end, limit=resolved_limit)
    except ValueError as exc:
        raise _error(422, "INVALID_QUERY_WINDOW", str(exc)) from exc
    if window.end - window.start > timedelta(seconds=settings.query_max_window_seconds):
        raise _error(422, "QUERY_RANGE_EXCEEDED", "requested time range is too large")
    return window


async def _query(call: Any) -> Any:
    try:
        return await call
    except InvalidCursor as exc:
        raise _error(422, "INVALID_CURSOR", str(exc)) from exc
    except ValueError as exc:
        raise _error(422, "INVALID_QUERY", str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(500, "QUERY_FAILED", "Operator query failed") from exc


def _page(page: Any, window: QueryWindow) -> dict[str, Any]:
    return {
        "items": [_row(item) for item in page.items],
        "next_cursor": page.next_cursor,
        "from": window.start.isoformat(),
        "to": window.end.isoformat(),
    }


def _row(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in value.__table__.columns:
        result[column.name] = _json_value(getattr(value, column.name))
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


@router.get("/status")
async def operational_status(request: Request, environment: str | None = None) -> dict[str, Any]:
    settings = request.app.state.settings
    selected_environment = environment or settings.environment
    if not selected_environment or len(selected_environment) > 32:
        raise _error(422, "INVALID_ENVIRONMENT", "environment is invalid")
    rows = await _query(
        request.app.state.operator_store.status_rows(environment=selected_environment)
    )
    capacity = None
    capacity_error = None
    try:
        observed = await asyncio.wait_for(
            request.app.state.capacity_adapter.read_capacity(
                observed_at=request.app.state.clock.now()
            ),
            timeout=settings.status_capacity_timeout_seconds,
        )
        capacity = observed.state.model_dump(mode="json")
    except Exception as exc:
        capacity_error = f"capacity_unavailable:{type(exc).__name__}"
    snapshot = rows["snapshot"]
    active_shedding = rows["shedding"]
    workers = [_worker(item) for item in request.app.state.workers]
    scheduled = getattr(request.app.state, "scheduled_worker", None)
    next_due = None if scheduled is None else scheduled.next_due_at
    provider_health = (
        {}
        if snapshot is None
        else snapshot.signal_details.get("provider_health", {})
    )
    return {
        "environment": selected_environment,
        "execution_mode": settings.execution_mode.value,
        "global_ceiling": settings.global_instance_ceiling,
        "state": request.app.state.state_machine.state.value,
        "database_ready": bool(request.app.state.db_ready),
        "demo_app_ready": _demo_ready(snapshot),
        "capacity": capacity,
        "capacity_error": capacity_error,
        "shedding_level": (
            int(snapshot.load_shedding_level)
            if snapshot is not None
            else int(active_shedding.to_level)
            if active_shedding is not None
            else 0
        ),
        "endpoint_policy": (
            {} if active_shedding is None else active_shedding.policy_snapshot
        ),
        "cooldown_until": (
            None
            if rows["action"] is None or rows["action"].cooldown_until is None
            else ensure_utc(rows["action"].cooldown_until).isoformat()
        ),
        "recovery": {
            "low_confirmations": getattr(
                request.app.state.recovery_coordinator, "low_confirmations", 0
            ),
        },
        "latest_snapshot": None if snapshot is None else _row(snapshot),
        "latest_prediction": (
            None if rows["prediction"] is None else _row(rows["prediction"])
        ),
        "latest_action": None if rows["action"] is None else _row(rows["action"]),
        "scheduled": {
            "next_due_at": None if next_due is None else next_due.isoformat(),
            "event": (
                None if rows["scheduled_event"] is None else _row(rows["scheduled_event"])
            ),
        },
        "providers": provider_health,
        "workers": workers,
    }


@router.get("/snapshots")
async def snapshots(
    request: Request,
    environment: str | None = None,
    from_: FromDate = None,
    to: datetime | None = None,
    limit: PageLimit = None,
    cursor: PageCursor = None,
    demo_run_id: UUID | None = None,
) -> dict[str, Any]:
    window = _window(request, from_, to, limit)
    page = await _query(
        request.app.state.operator_store.snapshots(
            environment=environment or request.app.state.settings.environment,
            window=window,
            cursor=cursor,
            demo_run_id=demo_run_id,
        )
    )
    return _page(page, window)


@router.get("/predictions")
async def predictions(
    request: Request,
    from_: FromDate = None,
    to: datetime | None = None,
    limit: PageLimit = None,
    cursor: PageCursor = None,
    environment: str | None = None,
    demo_run_id: UUID | None = None,
    mode: PredictionModeFilter = None,
    status_: PredictionStatusFilter = None,
) -> dict[str, Any]:
    window = _window(request, from_, to, limit)
    page = await _query(
        request.app.state.operator_store.predictions(
            window=window,
            cursor=cursor,
            environment=environment,
            demo_run_id=demo_run_id,
            mode=mode,
            status=status_,
        )
    )
    return _page(page, window)


@router.get("/predictions/{prediction_id}")
async def prediction_detail(
    prediction_id: UUID,
    request: Request,
    include_actual_window_seconds: Annotated[int, Query(ge=0, le=86_400)] = 3_600,
) -> dict[str, Any]:
    maximum = request.app.state.settings.query_max_window_seconds
    if include_actual_window_seconds > maximum:
        raise _error(422, "QUERY_RANGE_EXCEEDED", "actual overlay window is too large")
    overlay = await _query(
        request.app.state.operator_store.prediction_overlay(
            prediction_id, actual_window_seconds=include_actual_window_seconds
        )
    )
    if overlay is None:
        raise _error(404, "PREDICTION_NOT_FOUND", "Prediction was not found")
    return {
        "prediction": _row(overlay.prediction),
        "predicted_points": [_row(item) for item in overlay.points],
        "actions": [_row(item) for item in overlay.actions],
        "load_shedding_events": [_row(item) for item in overlay.shedding_events],
        "actual_snapshots": [_row(item) for item in overlay.actual_snapshots],
    }


@router.get("/scaling-actions")
async def scaling_actions(
    request: Request,
    from_: FromDate = None,
    to: datetime | None = None,
    limit: PageLimit = None,
    cursor: PageCursor = None,
    status_: ActionStatusFilter = None,
    execution_mode: ExecutionModeFilter = None,
    correlation_id: UUID | None = None,
    prediction_id: UUID | None = None,
    demo_run_id: UUID | None = None,
) -> dict[str, Any]:
    window = _window(request, from_, to, limit)
    page = await _query(
        request.app.state.operator_store.scaling_actions(
            window=window,
            cursor=cursor,
            status=status_,
            execution_mode=execution_mode,
            correlation_id=correlation_id,
            prediction_id=prediction_id,
            demo_run_id=demo_run_id,
        )
    )
    return _page(page, window)


@router.get("/load-shedding-events")
async def load_shedding_events(
    request: Request,
    from_: FromDate = None,
    to: datetime | None = None,
    limit: PageLimit = None,
    cursor: PageCursor = None,
    environment: str | None = None,
    correlation_id: UUID | None = None,
    prediction_id: UUID | None = None,
    demo_run_id: UUID | None = None,
    active_only: bool = False,
) -> dict[str, Any]:
    window = _window(request, from_, to, limit)
    page = await _query(
        request.app.state.operator_store.shedding_events(
            window=window,
            cursor=cursor,
            environment=environment,
            correlation_id=correlation_id,
            prediction_id=prediction_id,
            demo_run_id=demo_run_id,
            active_only=active_only,
        )
    )
    return _page(page, window)


@router.get("/scheduled-events")
async def scheduled_events(
    request: Request,
    from_: FromDate = None,
    to: datetime | None = None,
    limit: PageLimit = None,
    cursor: PageCursor = None,
    status_: ScheduledStatusFilter = None,
) -> dict[str, Any]:
    window = _window(request, from_, to, limit)
    page = await _query(
        request.app.state.operator_store.scheduled_events(
            window=window, cursor=cursor, status=status_
        )
    )
    return _page(page, window)


@router.get("/results/summary")
async def results_summary(
    request: Request,
    from_: FromDate = None,
    to: datetime | None = None,
    limit: PageLimit = None,
    cursor: PageCursor = None,
    environment: str | None = None,
    scenario: Annotated[str | None, Query(max_length=120)] = None,
    execution_mode: ExecutionModeFilter = None,
) -> dict[str, Any]:
    window = _window(request, from_, to, limit)
    page = await _query(
        request.app.state.operator_store.result_runs(
            window=window,
            cursor=cursor,
            environment=environment,
            scenario=scenario,
            execution_mode=execution_mode,
        )
    )
    return _page(page, window)


@router.get("/results/runs/{run_id}")
async def result_run_detail(
    run_id: UUID,
    request: Request,
    require_complete: bool = False,
) -> dict[str, Any]:
    run = await _query(request.app.state.operator_store.run_detail(run_id))
    if run is None:
        raise _error(404, "RUN_NOT_FOUND", "Demo run was not found")
    if require_complete and run.status != "completed":
        raise _error(409, "RUN_NOT_COMPLETE", "Demo run evaluation is not complete")
    payload = _row(run)
    payload["formula_definitions"] = FeedbackEvaluator.formulas()
    payload["references"] = run.result_summary.get("references", {})
    payload["chart_time_bounds"] = {
        "from": ensure_utc(run.started_at).isoformat(),
        "to": None if run.ended_at is None else ensure_utc(run.ended_at).isoformat(),
    }
    return payload


def _worker(worker: Any) -> dict[str, Any]:
    health = getattr(worker, "health", None)
    if health is not None:
        return health.model_dump(mode="json")
    return {
        "name": getattr(worker, "name", type(worker).__name__),
        "status": getattr(getattr(worker, "status", None), "value", "degraded"),
        "checked_at": None,
        "detail": getattr(worker, "detail", None),
    }


def _demo_ready(snapshot: Any | None) -> bool:
    if snapshot is None:
        return False
    health = snapshot.signal_details.get("provider_health", {}).get("demo_app", {})
    return health.get("status") == "healthy"


__all__ = ["operational_status", "router"]
