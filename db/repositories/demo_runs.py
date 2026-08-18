from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.contracts import DemoRunSpec, QueryWindow, ResultMetrics, validate_evidence
from common.enums import DemoRunStatus
from common.time import ensure_utc
from db.models import DemoRun
from db.repositories.base import Page, RepositoryLimits


class IdempotencyConflict(ValueError):
    """The same idempotency key was reused with a different payload."""


class DemoRunRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limits: RepositoryLimits | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._limits = limits or RepositoryLimits()

    async def start(self, spec: DemoRunSpec) -> tuple[DemoRun, bool]:
        run_id = uuid4()
        values = {
            "id": run_id,
            "idempotency_key": spec.idempotency_key,
            "scenario_name": spec.scenario_name,
            "mode": spec.mode.value,
            "environment": spec.environment,
            "baseline_type": spec.baseline_type,
            "execution_mode": spec.execution_mode.value,
            "configuration": validate_evidence(spec.configuration),
            "thresholds": validate_evidence(spec.thresholds),
            "started_at": spec.started_at,
            "status": DemoRunStatus.RUNNING.value,
            "formula_version": "v1",
            "locust_summary": {},
            "result_summary": {},
            "warnings": {},
        }
        statement = (
            insert(DemoRun)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[DemoRun.idempotency_key])
            .returning(DemoRun.id)
        )
        async with self._session_factory() as session, session.begin():
            inserted_id = (await session.execute(statement)).scalar_one_or_none()
            if inserted_id is not None:
                run = await session.get(DemoRun, inserted_id)
                if run is None:
                    raise RuntimeError("created demo run could not be reloaded")
                return run, True
            existing = await session.scalar(
                select(DemoRun).where(DemoRun.idempotency_key == spec.idempotency_key)
            )
            if existing is None:
                raise RuntimeError("conflicting demo run could not be loaded")
            if not self._matches_spec(existing, spec):
                raise IdempotencyConflict("idempotency key already has a different run payload")
            return existing, False

    async def complete(
        self,
        *,
        run_id: UUID,
        status: DemoRunStatus,
        ended_at: datetime,
        locust_summary: dict,
        notes: str | None = None,
        results: ResultMetrics | None = None,
    ) -> DemoRun:
        if status not in {
            DemoRunStatus.PENDING_EVALUATION,
            DemoRunStatus.COMPLETED,
            DemoRunStatus.FAILED,
            DemoRunStatus.CANCELLED,
        }:
            raise ValueError("a run can only complete into a terminal or evaluation state")
        ended_at = ensure_utc(ended_at, field_name="ended_at")
        locust_summary = validate_evidence(locust_summary)
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(DemoRun).where(DemoRun.id == run_id).with_for_update()
            )
            if run is None:
                raise LookupError(f"demo run {run_id} was not found")
            if ended_at < run.started_at:
                raise ValueError("ended_at must not precede started_at")
            if run.status not in {
                DemoRunStatus.RUNNING.value,
                DemoRunStatus.PENDING_EVALUATION.value,
            }:
                if run.status == status.value and run.ended_at == ended_at:
                    return run
                raise IdempotencyConflict("demo run already has a different terminal state")
            run.status = status.value
            run.ended_at = ended_at
            run.locust_summary = locust_summary
            run.notes = notes
            if results is not None:
                result_values = results.model_dump(mode="json")
                run.checkout_p99_latency_ms = results.checkout_p99_latency_ms
                run.checkout_success_rate = results.checkout_success_rate
                run.detection_lead_seconds = results.detection_lead_seconds
                run.provisioning_efficiency_pct = results.provisioning_efficiency_pct
                run.prediction_error_pct = results.prediction_error_pct
                run.overprovisioned_instance_minutes = results.overprovisioned_instance_minutes
                run.underprovisioned_seconds = results.underprovisioned_seconds
                run.recovery_duration_seconds = results.recovery_duration_seconds
                run.cost_duration_seconds = results.cost_duration_seconds
                run.formula_version = results.formula_version
                run.result_summary = result_values
                run.warnings = {"items": list(results.warnings)}
            await session.flush()
            return run

    async def list_bounded(
        self,
        *,
        window: QueryWindow,
        environment: str | None = None,
        scenario_name: str | None = None,
        status: DemoRunStatus | None = None,
    ) -> Page[DemoRun]:
        self._limits.validate_window(window)
        statement = (
            select(DemoRun)
            .where(DemoRun.started_at >= window.start, DemoRun.started_at <= window.end)
            .order_by(DemoRun.started_at.desc(), DemoRun.id.desc())
            .limit(window.limit + 1)
        )
        if environment is not None:
            statement = statement.where(DemoRun.environment == environment)
        if scenario_name is not None:
            statement = statement.where(DemoRun.scenario_name == scenario_name)
        if status is not None:
            statement = statement.where(DemoRun.status == status.value)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        return Page(items=rows[: window.limit], has_more=len(rows) > window.limit)

    @staticmethod
    def _matches_spec(run: DemoRun, spec: DemoRunSpec) -> bool:
        return (
            run.scenario_name == spec.scenario_name
            and run.mode == spec.mode.value
            and run.environment == spec.environment
            and run.baseline_type == spec.baseline_type
            and run.execution_mode == spec.execution_mode.value
            and run.configuration == spec.configuration
            and run.thresholds == spec.thresholds
            and run.started_at == spec.started_at
        )
