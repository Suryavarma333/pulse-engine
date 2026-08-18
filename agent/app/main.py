from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from botocore.config import Config
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
from agent.app.metrics.providers.cloudwatch import (
    CloudFrontSignalProvider,
    CloudWatchCpuSignalProvider,
)
from agent.app.metrics.providers.demo_app import DemoAppSignalProvider
from agent.app.metrics.providers.sessions import (
    SessionLoginSignalProvider,
    http_session_fetcher,
)
from agent.app.metrics.providers.simulated import (
    SimulatedSignalBuffer,
    SimulatedSignalProvider,
)
from agent.app.metrics.providers.sqs import SqsSignalProvider
from agent.app.orchestration.realtime_lifecycle import (
    RealtimeControlLifecycle,
    RealtimeControlObservation,
)
from agent.app.orchestration.reconciliation import ActionReconciler
from agent.app.orchestration.recovery import (
    RecoveryCoordinator,
    RecoveryObservation,
    RecoveryWorker,
)
from agent.app.orchestration.response_pipeline import ResponsePipeline
from agent.app.orchestration.state_machine import ControlEvent, ControlStateMachine
from agent.app.runtime import AgentRuntime
from agent.app.services.feedback import FeedbackEvaluator, FeedbackService, FeedbackWorker
from agent.app.services.predictions import PredictionService
from agent.app.services.ramp_planner import RampPlanner
from agent.app.services.shedding_client import DemoAppSheddingClient
from agent.app.workers.dependencies import DependencySupervisor
from agent.app.workers.maintenance import (
    ControlMaintenanceWorker,
    DependencyReadiness,
    MaintenanceObservation,
)
from agent.app.workers.realtime import RealtimeWorker
from agent.app.workers.scheduled import ScheduledControlObservation, ScheduledWorker
from common.contracts import RecoveryPlan, ResponseCommand
from common.enums import (
    ControlState,
    ExecutionMode,
    PredictionMode,
    ResponseIntent,
    SheddingLevel,
)
from common.time import Clock
from db.repositories.base import RepositoryLimits
from db.repositories.demo_runs import DemoRunRepository
from db.repositories.evaluation import EvaluationDataRepository
from db.repositories.operator import OperatorQueryRepository
from db.repositories.predictions import PredictionRepository
from db.repositories.response_retries import ResponseRetryRepository
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
    runtime: AgentRuntime | None = None
    provider_configuration: dict[str, dict[str, Any]] | None = None


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
        task_by_worker: dict[str, asyncio.Task[Any]] = {}
        if start_workers:
            for worker in assembled.workers:
                if isinstance(worker, FeedbackWorker):
                    coroutine = worker.run(
                        stop_event, poll_seconds=configured.feedback_poll_seconds
                    )
                else:
                    coroutine = worker.run(stop_event)
                task = asyncio.create_task(coroutine, name=f"pulse-{worker.name}")
                tasks.append(task)
                task_by_worker[worker.name] = task
        app.state.worker_tasks = tasks
        app.state.worker_tasks_by_name = task_by_worker
        app.state.worker_execution_enabled = start_workers
        app.state.worker_available = bool(tasks) or not start_workers
        supervisor_task: asyncio.Task[Any] | None = None
        if start_workers and assembled.database is not None:
            supervisor = DependencySupervisor(
                probe=lambda: _database_ready(assembled.database),
                publish=lambda ready: _publish_database_readiness(app, ready),
                clock=assembled.clock,
                poll_seconds=min(configured.maintenance_poll_seconds, 5.0),
            )
            app.state.dependency_supervisor = supervisor
            supervisor_task = asyncio.create_task(
                supervisor.run(stop_event), name="pulse-dependency-supervisor"
            )
            task_by_worker[supervisor.name] = supervisor_task
        else:
            app.state.dependency_supervisor = None
        app.state.dependency_supervisor_task = supervisor_task
        _publish_database_readiness(app, assembled.db_ready)
        try:
            yield
        finally:
            stop_event.set()
            runtime_tasks = [*tasks]
            if supervisor_task is not None:
                runtime_tasks.append(supervisor_task)
            if runtime_tasks:
                await asyncio.gather(*runtime_tasks, return_exceptions=True)
            if assembled.shedding_client is not None:
                await assembled.shedding_client.close()
            if assembled.runtime is not None:
                await assembled.runtime.close()
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
        runtime_workers = list(request.app.state.workers)
        supervisor = getattr(request.app.state, "dependency_supervisor", None)
        if supervisor is not None:
            runtime_workers.append(supervisor)
        workers = [
            _worker_runtime_view(request.app.state, worker)
            for worker in runtime_workers
        ]
        degraded = any(item["status"] in {"degraded", "unavailable"} for item in workers)
        worker_available = _worker_runtime_available(request.app.state)
        request.app.state.worker_available = bool(
            request.app.state.db_ready and worker_available
        )
        ready = bool(request.app.state.db_ready and worker_available and not degraded)
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


def build_signal_providers(
    settings: AgentSettings,
    runtime: AgentRuntime,
    *,
    aws_client_factory: Any | None = None,
) -> tuple[list[Any], tuple[str, ...]]:
    """Assemble opt-in providers without touching AWS in local dry-run mode."""

    providers: list[Any] = [
        DemoAppSignalProvider(
            settings.demo_app_base_url,
            timeout_seconds=settings.provider_timeout_seconds,
            freshness_limit_seconds=settings.origin_signal_freshness_seconds,
        ),
        SimulatedSignalProvider(
            runtime.simulated_signals,
            timeout_seconds=min(0.5, settings.provider_timeout_seconds),
            freshness_limit_seconds=settings.optional_signal_freshness_seconds,
        ),
    ]
    omitted = {"cloudwatch_cpu", "cloudfront", "sqs", "sessions"}
    if settings.execution_mode is ExecutionMode.LIVE:
        factory = aws_client_factory or _default_aws_client
        sdk_config = Config(
            connect_timeout=settings.aws_sdk_connect_timeout_seconds,
            read_timeout=settings.aws_sdk_read_timeout_seconds,
            retries={
                "mode": "standard",
                "total_max_attempts": settings.aws_sdk_max_attempts,
            },
        )
        cloudwatch = None
        if settings.cloudwatch_cpu_enabled:
            cloudwatch = factory(
                "cloudwatch",
                region_name=settings.aws_region,
                config=sdk_config,
            )
        if settings.cloudwatch_cpu_enabled:
            assert cloudwatch is not None and settings.asg_name is not None
            providers.append(
                CloudWatchCpuSignalProvider(
                    cloudwatch,
                    settings.asg_name,
                    timeout_seconds=settings.provider_timeout_seconds,
                    freshness_limit_seconds=settings.optional_signal_freshness_seconds,
                    period_seconds=settings.aws_metric_period_seconds,
                )
            )
            omitted.remove("cloudwatch_cpu")
        if settings.cloudfront_distribution_id:
            cloudfront_cloudwatch = factory(
                "cloudwatch",
                region_name="us-east-1",
                config=sdk_config,
            )
            providers.append(
                CloudFrontSignalProvider(
                    cloudfront_cloudwatch,
                    settings.cloudfront_distribution_id,
                    timeout_seconds=settings.provider_timeout_seconds,
                    freshness_limit_seconds=settings.optional_signal_freshness_seconds,
                    period_seconds=settings.aws_metric_period_seconds,
                )
            )
            omitted.remove("cloudfront")
        if settings.sqs_queue_url:
            sqs = factory(
                "sqs",
                region_name=settings.aws_region,
                config=sdk_config,
            )
            providers.append(
                SqsSignalProvider(
                    sqs,
                    settings.sqs_queue_url,
                    timeout_seconds=settings.provider_timeout_seconds,
                    freshness_limit_seconds=settings.optional_signal_freshness_seconds,
                )
            )
            omitted.remove("sqs")
    if settings.session_signal_url:
        client = runtime.manage(
            httpx.AsyncClient(timeout=settings.provider_timeout_seconds)
        )
        providers.append(
            SessionLoginSignalProvider(
                http_session_fetcher(client, settings.session_signal_url),
                timeout_seconds=settings.provider_timeout_seconds,
                freshness_limit_seconds=settings.optional_signal_freshness_seconds,
            )
        )
        omitted.remove("sessions")
    return providers, tuple(sorted(omitted))


def _default_aws_client(service_name: str, **kwargs: Any) -> Any:
    import boto3

    return boto3.client(service_name, **kwargs)


async def _assemble(settings: AgentSettings) -> AgentComponents:
    runtime = AgentRuntime.create(settings)
    clock = runtime.clock
    database = Database(settings.database_url.get_secret_value())
    db_ready = await _database_ready(database)
    limits = RepositoryLimits(
        max_rows=settings.query_max_rows,
        max_window=timedelta(seconds=settings.query_max_window_seconds),
    )
    snapshots = SnapshotRepository(database.session_factory, limits=limits)
    predictions = PredictionRepository(database.session_factory, limits=limits)
    actions = ScalingActionRepository(database.session_factory, limits=limits)
    response_retries = ResponseRetryRepository(database.session_factory)
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
    buffer = runtime.simulated_signals
    target_resource = settings.asg_name or "local-simulated-asg"
    capacity = AutoScalingCapacityAdapter(
        execution_mode=settings.execution_mode,
        target_resource=target_resource,
        region=settings.aws_region,
        global_ceiling=settings.global_instance_ceiling,
        simulated_capacity=settings.simulated_desired_capacity,
        connect_timeout_seconds=settings.aws_sdk_connect_timeout_seconds,
        read_timeout_seconds=settings.aws_sdk_read_timeout_seconds,
        max_attempts=settings.aws_sdk_max_attempts,
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
        retry_repository=response_retries,
    )
    providers, omitted_providers = build_signal_providers(settings, runtime)
    collector = CompositeSignalCollector(
        providers,
        snapshots,
        omitted_providers=omitted_providers,
        capacity_reader=capacity,
        capacity_per_instance_rps=settings.rps_per_instance,
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
        demo_status = await shedding.read_status()
        return ScheduledControlObservation(
            current_capacity=capacity_state.state.desired,
            current_shedding_level=demo_status.level,
            demo_run_id=await demo_runs.current_demo_run_id(
                environment=settings.environment
            ),
            request_rate_rps=(
                None if snapshot is None else snapshot.origin_request_rate_rps
            ),
            observed_at=None if snapshot is None else snapshot.observed_at,
            snapshot_id=None if snapshot is None else snapshot.id,
        )

    async def command_handler(command: ResponseCommand) -> None:
        observed = await observation()
        await response.execute(
            command,
            current_capacity=observed.current_capacity,
            current_shedding_level=observed.current_shedding_level,
            requested_at=clock.now(),
        )

    recovery_coordinator = RecoveryCoordinator(state_machine)
    recovery_worker = RecoveryWorker(
        coordinator=recovery_coordinator,
        response_pipeline=response,
    )

    async def realtime_observation() -> RealtimeControlObservation:
        observed = await observation()
        return RealtimeControlObservation(
            observed_at=clock.now(),
            current_capacity=observed.current_capacity,
            current_shedding_level=observed.current_shedding_level,
        )

    realtime_lifecycle = RealtimeControlLifecycle(
        state_machine=state_machine,
        recovery_worker=recovery_worker,
        observation_provider=realtime_observation,
    )
    realtime = RealtimeWorker(
        collector=collector,
        detector=RealtimeDetector.from_settings(settings),
        prediction_service=prediction_service,
        clock=clock,
        poll_seconds=settings.metric_poll_seconds,
        command_handler=command_handler,
        decision_handler=realtime_lifecycle.handle,
        active_run_provider=demo_runs,
        retry_scheduler=response_retries,
        environment=settings.environment,
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
                snapshot_id=observed.snapshot_id,
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
    reconciler = ActionReconciler(
        repository=actions,
        capacity_adapter=capacity,
        minimum_age_seconds=settings.reconciliation_min_age_seconds,
        batch_size=settings.reconciliation_batch_size,
    )

    async def maintenance_observation() -> MaintenanceObservation:
        observed = await observation()
        return MaintenanceObservation(
            current_capacity=observed.current_capacity,
            current_shedding_level=observed.current_shedding_level,
            request_rate_rps=observed.request_rate_rps or 0.0,
            observed_at=observed.observed_at,
            snapshot_id=observed.snapshot_id,
        )

    async def maintenance_recovery(observed: MaintenanceObservation) -> None:
        template = ResponseCommand(
            idempotency_key=f"runtime:{settings.environment}:recovery-template",
            correlation_id=uuid5(
                NAMESPACE_URL,
                f"pulse-runtime-recovery:{settings.environment}",
            ),
            mode=PredictionMode.REALTIME,
            intent=ResponseIntent.RECOVER,
            target_resource=target_resource,
            requested_desired_capacity=observed.current_capacity,
            maximum_ceiling=settings.global_instance_ceiling,
            reason_code="runtime_recovery",
            reasoning=(
                "Maintenance resumed bounded recovery from authoritative runtime state"
            ),
            signal_evidence={"source": "maintenance"},
            recovery_plan=recovery_plan,
        )
        await recovery_worker.run_once(
            RecoveryObservation(
                observed_at=observed.observed_at or clock.now(),
                request_rate_rps=observed.request_rate_rps,
                high_load=(
                    observed.request_rate_rps > recovery_plan.low_threshold_rps
                ),
                current_capacity=observed.current_capacity,
                current_shedding_level=observed.current_shedding_level,
                snapshot_id=observed.snapshot_id,
            ),
            command_template=template,
        )

    async def dependency_probe() -> DependencyReadiness:
        if not await _database_ready(database):
            return DependencyReadiness(False, ControlState.WATCH, "database_unavailable")
        try:
            capacity_state = await capacity.read_capacity(observed_at=clock.now())
        except Exception as exc:
            return DependencyReadiness(
                False,
                ControlState.WATCH,
                f"capacity_unavailable:{type(exc).__name__}",
            )
        try:
            demo_status = await shedding.read_status()
        except Exception as exc:
            return DependencyReadiness(
                False,
                ControlState.WATCH,
                f"demo_control_unavailable:{type(exc).__name__}",
            )
        if not demo_status.ready:
            return DependencyReadiness(False, ControlState.WATCH, "demo_control_not_ready")
        snapshot = await snapshots.latest(settings.environment)
        request_rate = 0.0 if snapshot is None else snapshot.origin_request_rate_rps
        elevated = (
            capacity_state.state.desired > settings.minimum_desired_capacity
            or demo_status.level > SheddingLevel.NORMAL
        )
        if elevated and request_rate > recovery_plan.low_threshold_rps:
            target = ControlState.PROTECT
        elif elevated:
            target = ControlState.RECOVERY
        else:
            target = ControlState.WATCH
        return DependencyReadiness(True, target, "dependencies_ready")

    maintenance = ControlMaintenanceWorker(
        reconciler=reconciler,
        retries=response_retries,
        actions=actions,
        response_pipeline=response,
        observation_provider=maintenance_observation,
        dependency_probe=dependency_probe,
        retention_store=snapshots,
        target_resource=target_resource,
        clock=clock,
        poll_seconds=settings.maintenance_poll_seconds,
        batch_size=settings.reconciliation_batch_size,
        retry_max_attempts=settings.response_retry_max_attempts,
        retry_backoff_seconds=settings.response_retry_backoff_seconds,
        retention_enabled=settings.snapshot_retention_enabled,
        retention_seconds=settings.snapshot_retention_seconds,
        retention_batch_size=settings.snapshot_retention_batch_size,
        recovery_handler=maintenance_recovery,
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
        workers=[realtime, scheduled, feedback, maintenance],
        database=database,
        shedding_client=shedding,
        db_ready=db_ready,
        runtime=runtime,
        provider_configuration=collector.provider_configuration,
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
    app.state.provider_configuration = components.provider_configuration or {}
    app.state.workers = components.workers
    app.state.scheduled_worker = next(
        (worker for worker in components.workers if isinstance(worker, ScheduledWorker)),
        None,
    )
    app.state.db_ready = components.db_ready


def _publish_database_readiness(app: FastAPI, ready: bool) -> None:
    app.state.db_ready = ready
    app.state.worker_available = bool(ready and _worker_runtime_available(app.state))


def _worker_runtime_available(state: Any) -> bool:
    if not getattr(state, "worker_execution_enabled", False):
        return True
    tasks = getattr(state, "worker_tasks", ())
    return bool(tasks) and all(not task.done() for task in tasks)


def _worker_runtime_view(state: Any, worker: Any) -> dict[str, Any]:
    name = getattr(worker, "name", type(worker).__name__)
    task = getattr(state, "worker_tasks_by_name", {}).get(name)
    if task is not None and task.done():
        detail = "worker_task_stopped"
        if not task.cancelled():
            try:
                exception = task.exception()
            except asyncio.InvalidStateError:
                exception = None
            if exception is not None:
                detail = f"worker_task_failed:{type(exception).__name__}"
        return {"name": name, "status": "unavailable", "detail": detail}
    return {
        "name": name,
        "status": _worker_status(worker),
        "detail": _worker_detail(worker),
    }


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


__all__ = ["AgentComponents", "app", "build_signal_providers", "create_app"]
