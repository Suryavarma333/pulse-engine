from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agent.app.services.ramp_planner import RampPlan
from common.contracts import RecoveryPlan, ResponseCommand, SurgePredictionCandidate
from common.enums import PredictionMode, SheddingLevel
from db.models import SurgePrediction, SurgePredictionPoint


class PredictionWriter(Protocol):
    async def create_with_points(
        self,
        prediction: SurgePrediction,
        points: list[SurgePredictionPoint],
    ) -> SurgePrediction: ...

    async def latest_for_scheduled_event(
        self, scheduled_event_id: UUID
    ) -> SurgePrediction | None: ...

    async def get_with_points(
        self, prediction_id: UUID, *, point_limit: int = 1_000
    ) -> tuple[SurgePrediction | None, list[SurgePredictionPoint]]: ...


@dataclass(frozen=True, slots=True)
class PersistedPrediction:
    prediction: SurgePrediction
    points: tuple[SurgePredictionPoint, ...]
    command: ResponseCommand


class PredictionService:
    """Persists detector output before constructing the shared immutable response command."""

    def __init__(
        self,
        writer: PredictionWriter,
        *,
        target_resource: str,
        maximum_ceiling: int,
        recovery_plan: RecoveryPlan,
    ) -> None:
        self._writer = writer
        self._target_resource = target_resource
        self._maximum_ceiling = maximum_ceiling
        self._recovery_plan = recovery_plan

    async def persist_realtime(
        self,
        candidate: SurgePredictionCandidate,
        *,
        trigger_snapshot_id: int,
    ) -> PersistedPrediction:
        if candidate.mode is not PredictionMode.REALTIME:
            raise ValueError("persist_realtime requires a real-time prediction")
        if trigger_snapshot_id < 1:
            raise ValueError("trigger_snapshot_id must be positive")
        candidate = candidate.model_copy(update={"trigger_snapshot_id": trigger_snapshot_id})
        prediction_id = uuid4()
        multiplier = (
            candidate.predicted_peak_rps / candidate.baseline_rps
            if candidate.baseline_rps > 0
            else 1.0
        )
        prediction = SurgePrediction(
            id=prediction_id,
            mode=candidate.mode.value,
            demo_run_id=candidate.demo_run_id,
            environment=candidate.environment,
            correlation_id=candidate.correlation_id,
            scheduled_event_id=candidate.scheduled_event_id,
            trigger_snapshot_id=trigger_snapshot_id,
            model_name="rolling-acceleration",
            model_version="v1",
            basis_window_start=candidate.basis_window_start,
            basis_window_end=candidate.basis_window_end,
            predicted_start_at=candidate.predicted_start_at,
            predicted_peak_at=candidate.predicted_peak_at,
            predicted_end_at=candidate.predicted_end_at,
            baseline_rps=candidate.baseline_rps,
            predicted_peak_rps=candidate.predicted_peak_rps,
            predicted_multiplier=multiplier,
            recommended_capacity=candidate.recommended_capacity,
            confidence=candidate.confidence,
            trigger_type=candidate.reason_code,
            reasoning=candidate.reasoning,
            signal_evidence=candidate.signal_evidence,
            status="active",
            reactive_comparator_crossed_at=_optional_comparator(candidate.signal_evidence),
            formula_version="v1",
        )
        current_rps = float(
            candidate.signal_evidence.get("current_rps", candidate.baseline_rps)
        )
        points = [
            SurgePredictionPoint(
                prediction_id=prediction_id,
                point_at=candidate.predicted_start_at,
                predicted_rps=current_rps,
                predicted_capacity=max(
                    1, min(candidate.recommended_capacity, self._maximum_ceiling)
                ),
            ),
            SurgePredictionPoint(
                prediction_id=prediction_id,
                point_at=candidate.predicted_peak_at,
                predicted_rps=candidate.predicted_peak_rps,
                predicted_capacity=min(candidate.recommended_capacity, self._maximum_ceiling),
            ),
        ]
        persisted = await self._writer.create_with_points(prediction, points)
        command = ResponseCommand(
            command_id=uuid4(),
            idempotency_key=f"realtime:{candidate.environment}:{trigger_snapshot_id}",
            correlation_id=candidate.correlation_id,
            demo_run_id=candidate.demo_run_id,
            prediction_id=prediction_id,
            trigger_snapshot_id=trigger_snapshot_id,
            mode=PredictionMode.REALTIME,
            target_resource=self._target_resource,
            requested_desired_capacity=candidate.recommended_capacity,
            maximum_ceiling=self._maximum_ceiling,
            requested_shedding_level=SheddingLevel.DISABLE_RECOMMENDATIONS,
            reason_code=candidate.reason_code,
            reasoning=candidate.reasoning,
            signal_evidence=candidate.signal_evidence,
            recovery_plan=self._recovery_plan,
        )
        return PersistedPrediction(
            prediction=persisted,
            points=tuple(points),
            command=command,
        )

    async def ensure_scheduled(
        self,
        event: Any,
        *,
        plan: RampPlan,
        demo_run_id: UUID | None,
        environment: str,
        created_at: datetime,
    ) -> PersistedPrediction:
        existing = await self._writer.latest_for_scheduled_event(event.id)
        if existing is not None:
            _, points = await self._writer.get_with_points(existing.id)
            return PersistedPrediction(
                prediction=existing,
                points=tuple(points),
                command=self._scheduled_template(
                    existing,
                    event=event,
                    plan=plan,
                    demo_run_id=demo_run_id,
                ),
            )
        correlation_id = uuid5(NAMESPACE_URL, f"pulse-scheduled:{event.id}")
        prediction_id = uuid4()
        baseline = float(event.baseline_rps or 0)
        peak = float(event.expected_peak_rps or baseline * float(event.expected_multiplier))
        prediction = SurgePrediction(
            id=prediction_id,
            mode=PredictionMode.SCHEDULED.value,
            demo_run_id=demo_run_id,
            environment=environment,
            correlation_id=correlation_id,
            scheduled_event_id=event.id,
            model_name="scheduled-ramp",
            model_version="v1",
            created_at=created_at,
            basis_window_start=min(created_at, plan.points[0].due_at) - timedelta(microseconds=1),
            basis_window_end=created_at,
            predicted_start_at=plan.points[0].due_at,
            predicted_peak_at=plan.points[-1].due_at,
            predicted_end_at=event.ends_at,
            baseline_rps=baseline,
            predicted_peak_rps=max(peak, baseline),
            predicted_multiplier=float(event.expected_multiplier),
            recommended_capacity=plan.points[-1].desired_capacity,
            confidence=float(event.confidence),
            trigger_type="scheduled_event",
            reasoning=f"Scheduled prewarm forecast for {event.name}",
            signal_evidence={
                "scheduled_event_id": str(event.id),
                "event_timezone": event.timezone,
                "event_starts_at": event.starts_at.isoformat(),
                "event_effective_ceiling": plan.effective_ceiling,
            },
            status="active",
            formula_version="v1",
        )
        points = [
            SurgePredictionPoint(
                prediction_id=prediction_id,
                point_at=point.due_at,
                predicted_rps=(
                    peak * point.desired_capacity / max(plan.points[-1].desired_capacity, 1)
                ),
                predicted_capacity=point.desired_capacity,
            )
            for point in plan.points
        ]
        persisted = await self._writer.create_with_points(prediction, points)
        return PersistedPrediction(
            prediction=persisted,
            points=tuple(points),
            command=self._scheduled_template(
                persisted,
                event=event,
                plan=plan,
                demo_run_id=demo_run_id,
            ),
        )

    def _scheduled_template(
        self,
        prediction: SurgePrediction,
        *,
        event: Any,
        plan: RampPlan,
        demo_run_id: UUID | None,
    ) -> ResponseCommand:
        return ResponseCommand(
            command_id=uuid5(NAMESPACE_URL, f"pulse-scheduled-template:{event.id}"),
            idempotency_key=f"scheduled:{event.id}:prediction",
            correlation_id=prediction.correlation_id,
            demo_run_id=demo_run_id,
            prediction_id=prediction.id,
            scheduled_event_id=event.id,
            mode=PredictionMode.SCHEDULED,
            target_resource=self._target_resource,
            requested_desired_capacity=plan.points[-1].desired_capacity,
            maximum_ceiling=plan.effective_ceiling,
            reason_code="scheduled_event_prediction",
            reasoning=f"Scheduled forecast for {event.name}",
            signal_evidence=prediction.signal_evidence,
            recovery_plan=self._recovery_plan,
        )


def _optional_comparator(evidence: dict) -> datetime | None:
    raw = evidence.get("reactive_comparator_crossed_at")
    if raw is None:
        return None
    return datetime.fromisoformat(str(raw))


__all__ = ["PersistedPrediction", "PredictionService", "PredictionWriter"]
