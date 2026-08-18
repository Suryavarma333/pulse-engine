from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from agent.app.api.internal import router as internal_router
from agent.app.api.public import router as public_router
from agent.app.aws.autoscaling import AutoScalingCapacityAdapter
from agent.app.config import AgentSettings
from agent.app.detection.realtime import RealtimeDetector
from agent.app.metrics.collector import CompositeSignalCollector
from agent.app.metrics.providers.demo_app import DemoAppSignalProvider
from agent.app.metrics.providers.simulated import (
    SimulatedSignalBuffer,
    SimulatedSignalProvider,
)
from agent.app.orchestration.recovery import (
    RecoveryCoordinator,
    RecoveryObservation,
    RecoveryWorker,
)
from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlEvent, ControlStateMachine
from agent.app.services.feedback import FeedbackEvaluator, FeedbackService, FeedbackWorker
from agent.app.services.predictions import PredictionService
from agent.app.services.ramp_planner import RampPlanner
from agent.app.services.shedding_client import DemoAppSheddingClient
from agent.app.workers.realtime import RealtimeWorker
from agent.app.workers.scheduled import ScheduledControlObservation, ScheduledWorker
from common.contracts import RecoveryPlan, ResponseCommand
from common.enums import ControlState, PredictionMode, ResponseIntent, SheddingLevel
from common.time import Clock, SystemClock
from db.repositories.base import RepositoryLimits
from db.repositories.demo_runs import DemoRunRepository
from db.repositories.evaluation import EvaluationDataRepository
from db.repositories.operator import OperatorQueryRepository
from db.repositories.predictions import PredictionRepository
from db.repositories.scaling_actions import ScalingActionRepository
from db.repositories.scheduled_events import ScheduledEventRepository
from db.repositories.snapshots import SnapshotRepository
from db.session import Database


@dataclass(slots=True)
class AgentComponents:
    settings: AgentSettings
    clock: Clock
    simulated_signal_buffer: SimulatedSignalBuffer
    operator_store: Any
    demo_runs: Any
    feedback_service: Any
    capacity_adapter: Any
    state_machine: ControlStateMachine
    recovery_coordinator: Any
    workers: list[Any]
    database: Database | None = None
    shedding_client: DemoAppSheddingClient | None = None
    db_ready: bool = True


def create_app(
    *,
    settings: AgentSettings | None = None,
    components: AgentComponents | None = None,
    start_workers: bool = True,
) -> FastAPI:
    configured = settings or (
        components.settings if components else AgentSettings.from_environment()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        assembled = components or await _assemble(configured)
        _install_state(app, assembled)
        stop_event = asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        if start_workers and assembled.db_ready:
            for worker in assembled.workers:
                if isinstance(worker, FeedbackWorker):
                    coroutine = worker.run(
                        stop_event, poll_seconds=configured.feedback_poll_seconds
                    )
                else:
                    coroutine = worker.run(stop_event)
                tasks.append(asyncio.create_task(coroutine, name=f"pulse-{worker.name}"))
        app.state.worker_tasks = tasks
        app.state.worker_available = assembled.db_ready and (
            bool(tasks) or not start_workers
        )
        try:
            yield
        finally:
            stop_event.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if assembled.shedding_client is not None:
                await assembled.shedding_client.close()
            if assembled.database is not None:
                await assembled.database.dispose()

    app = FastAPI(
        title="Pulse predictive surge control plane",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.dashboard_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
        max_age=600,
    )

    @app.exception_handler(HTTPException)
    async def structured_http_error(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and {"detail", "code"} <= exc.detail.keys():
            content = exc.detail
        else:
            content = {"detail": str(exc.detail), "code": "HTTP_ERROR"}
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    async def structured_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "type": item.get("type"),
                "location": list(item.get("loc", ())),
                "message": item.get("msg"),
            }
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Request validation failed",
                "code": "VALIDATION_ERROR",
                "errors": errors,
            },
        )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        ready = bool(request.app.state.db_ready and request.app.state.worker_available)
        workers = [
            {
                "name": getattr(worker, "name", type(worker).__name__),
                "status": _worker_status(worker),
                "detail": _worker_detail(worker),
            }
            for worker in request.app.state.workers
        ]
        degraded = any(item["status"] in {"degraded", "unavailable"} for item in workers)
        payload = {
            "service": "pulse-agent",
            "environment": request.app.state.settings.environment,
            "status": "ok" if ready and not degraded else "degraded",
            "ready": ready,
            "database": {"ready": bool(request.app.state.db_ready)},
            "demo_app": {"ready": _demo_app_ready(request.app.state)},
            "workers": workers,
        }
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    app.include_router(public_router)
    app.include_router(internal_router)
    return app


async def _assemble(settings: AgentSettings) -> AgentComponents:
    clock = SystemClock()
    database = Database(settings.database_url.get_secret_value())
    db_ready = await _database_ready(database)
    limits = RepositoryLimits(
        max_rows=settings.query_max_rows,
        max_window=timedelta(seconds=settings.query_max_window_seconds),
    )
    snapshots = SnapshotRepository(database.session_factory, limits=limits)
    predictions = PredictionRepository(database.session_factory, limits=limits)
    actions = ScalingActionRepository(database.session_factory, limits=limits)
    scheduled_events = ScheduledEventRepository(database.session_factory, limits=limits)
    demo_runs = DemoRunRepository(database.session_factory, limits=limits)
    operator = OperatorQueryRepository(database.session_factory, limits=limits)
    evaluation_data = EvaluationDataRepository(database.session_factory)
    feedback_service = FeedbackService(
        runs=demo_runs,
        data_source=evaluation_data,
        predictions=predictions,
        evaluator=FeedbackEvaluator(),
        max_rows=settings.query_max_rows,
    )
    buffer = SimulatedSignalBuffer(
        capacity=settings.signal_buffer_capacity,
        max_ttl_seconds=settings.simulated_signal_max_ttl_seconds,
        future_tolerance_seconds=settings.simulated_signal_future_tolerance_seconds,
    )
    target_resource = settings.asg_name or "local-simulated-asg"
    capacity = AutoScalingCapacityAdapter(
        execution_mode=settings.execution_mode,
        target_resource=target_resource,
        region=settings.aws_region,
        global_ceiling=settings.global_instance_ceiling,
        simulated_capacity=settings.simulated_desired_capacity,
    )
    state_machine = ControlStateMachine()
    recovery_plan = RecoveryPlan(
        low_threshold_rps=settings.recovery_low_threshold_rps,
        confirmation_count=settings.recovery_confirmation_count,
        cooldown_seconds=settings.cooldown_seconds,
        decrement_step=settings.recovery_decrement_step,
        capacity_floor=settings.minimum_desired_capacity,
    )
    shedding = DemoAppSheddingClient(
        base_url=settings.demo_app_base_url,
        control_token=settings.control_token,
        timeout_seconds=settings.shedding_control_timeout_seconds,
    )
    response = ResponsePipeline(
        repository=actions,
        capacity_adapter=capacity,
        shedding_client=shedding,
        state_machine=state_machine,
        global_ceiling=settings.global_instance_ceiling,
    )
    collector = CompositeSignalCollector(
        [
            DemoAppSignalProvider(
                settings.demo_app_base_url,
                timeout_seconds=settings.provider_timeout_seconds,
                freshness_limit_seconds=settings.origin_signal_freshness_seconds,
            ),
            SimulatedSignalProvider(
                buffer,
                timeout_seconds=min(0.5, settings.provider_timeout_seconds),
                freshness_limit_seconds=settings.optional_signal_freshness_seconds,
            ),
        ],
        snapshots,
    )
    prediction_service = PredictionService(
        predictions,
        target_resource=target_resource,
        maximum_ceiling=settings.global_instance_ceiling,
        recovery_plan=recovery_plan,
    )

    async def observation() -> ScheduledControlObservation:
        capacity_state = await capacity.read_capacity(observed_at=clock.now())
        snapshot = await snapshots.latest(settings.environment)
        return ScheduledControlObservation(
            current_capacity=capacity_state.state.desired,
            current_shedding_level=SheddingLevel(
                0 if snapshot is None else snapshot.load_shedding_level
            ),
            demo_run_id=await demo_runs.current_demo_run_id(
                environment=settings.environment
            ),
            request_rate_rps=(
                None if snapshot is None else snapshot.origin_request_rate_rps
            ),
        )

    async def command_handler(command: ResponseCommand) -> None:
        observed = await observation()
        await response.execute(
            command,
            current_capacity=observed.current_capacity,
            current_shedding_level=observed.current_shedding_level,
            requested_at=clock.now(),
        )

    realtime = RealtimeWorker(
        collector=collector,
        detector=RealtimeDetector.from_settings(settings),
        prediction_service=prediction_service,
        clock=clock,
        poll_seconds=settings.metric_poll_seconds,
        command_handler=command_handler,
        active_run_provider=demo_runs,
    )
    recovery_coordinator = RecoveryCoordinator(state_machine)
    recovery_worker = RecoveryWorker(
        coordinator=recovery_coordinator,
        response_pipeline=response,
    )

    async def scheduled_recovery(
        event: Any,
        observed: ScheduledControlObservation,
        now: Any,
    ) -> None:
        low = (observed.request_rate_rps or 0) <= recovery_plan.low_threshold_rps
        if state_machine.state is ControlState.PREWARM and low:
            state_machine.transition(ControlEvent.EVENT_CANCELLED_LOW)
        if state_machine.state not in {
            ControlState.PROTECT,
            ControlState.RECOVERY,
            ControlState.COOLDOWN,
        }:
            return
        template = ResponseCommand(
            idempotency_key=f"scheduled:{event.id}:recovery-template",
            correlation_id=uuid5(NAMESPACE_URL, f"pulse-scheduled:{event.id}"),
            scheduled_event_id=event.id,
            mode=PredictionMode.SCHEDULED,
            intent=ResponseIntent.RECOVER,
            target_resource=target_resource,
            requested_desired_capacity=observed.current_capacity,
            maximum_ceiling=min(
                settings.global_instance_ceiling,
                event.max_instances_override or settings.global_instance_ceiling,
            ),
            reason_code="scheduled_recovery",
            reasoning="Scheduled event recovery uses sustained-low shared response policy",
            signal_evidence={"scheduled_event_id": str(event.id)},
            recovery_plan=recovery_plan,
        )
        await recovery_worker.run_once(
            RecoveryObservation(
                observed_at=now,
                request_rate_rps=observed.request_rate_rps or 0,
                high_load=not low,
                current_capacity=observed.current_capacity,
                current_shedding_level=observed.current_shedding_level,
            ),
            command_template=template,
        )

    scheduled = ScheduledWorker(
        reader=scheduled_events,
        planner=RampPlanner(
            global_ceiling=settings.global_instance_ceiling,
            derived_steps=settings.scheduled_ramp_steps,
        ),
        response_pipeline=response,
        observation_provider=observation,
        recovery_plan=recovery_plan,
        target_resource=target_resource,
        clock=clock,
        poll_seconds=settings.scheduled_poll_seconds,
        lookahead_seconds=settings.scheduled_lookahead_seconds,
        near_event_protection_seconds=settings.near_event_protection_seconds,
        recovery_handler=scheduled_recovery,
        prediction_service=prediction_service,
        environment=settings.environment,
    )
    feedback = FeedbackWorker(
        runs=demo_runs,
        service=feedback_service,
        clock=clock,
        horizon_seconds=settings.feedback_horizon_seconds,
    )
    return AgentComponents(
        settings=settings,
        clock=clock,
        simulated_signal_buffer=buffer,
        operator_store=operator,
        demo_runs=demo_runs,
        feedback_service=feedback_service,
        capacity_adapter=capacity,
        state_machine=state_machine,
        recovery_coordinator=recovery_coordinator,
        workers=[realtime, scheduled, feedback],
        database=database,
        shedding_client=shedding,
        db_ready=db_ready,
    )


async def _database_ready(database: Database) -> bool:
    try:
        async with database.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _install_state(app: FastAPI, components: AgentComponents) -> None:
    app.state.settings = components.settings
    app.state.clock = components.clock
    app.state.simulated_signal_buffer = components.simulated_signal_buffer
    app.state.control_token = components.settings.control_token
    app.state.operator_store = components.operator_store
    app.state.demo_runs = components.demo_runs
    app.state.feedback_service = components.feedback_service
    app.state.capacity_adapter = components.capacity_adapter
    app.state.state_machine = components.state_machine
    app.state.recovery_coordinator = components.recovery_coordinator
    app.state.workers = components.workers
    app.state.scheduled_worker = next(
        (worker for worker in components.workers if isinstance(worker, ScheduledWorker)),
        None,
    )
    app.state.db_ready = components.db_ready


def _worker_status(worker: Any) -> str:
    health = getattr(worker, "health", None)
    if health is not None:
        return health.status.value
    status = getattr(worker, "status", None)
    return getattr(status, "value", "degraded")


def _worker_detail(worker: Any) -> str | None:
    health = getattr(worker, "health", None)
    return health.detail if health is not None else getattr(worker, "detail", None)


def _demo_app_ready(state: Any) -> bool:
    for worker in state.workers:
        if isinstance(worker, RealtimeWorker):
            return worker.health.status.value == "healthy"
    return False


app = create_app()


__all__ = ["AgentComponents", "app", "create_app"]
