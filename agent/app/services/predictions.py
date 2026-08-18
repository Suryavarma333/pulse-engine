from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from common.contracts import RecoveryPlan, ResponseCommand, SurgePredictionCandidate
from common.enums import PredictionMode, SheddingLevel
from db.models import SurgePrediction, SurgePredictionPoint


class PredictionWriter(Protocol):
    async def create_with_points(
        self,
        prediction: SurgePrediction,
        points: list[SurgePredictionPoint],
    ) -> SurgePrediction: ...


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


def _optional_comparator(evidence: dict) -> datetime | None:
    raw = evidence.get("reactive_comparator_crossed_at")
    if raw is None:
        return None
    return datetime.fromisoformat(str(raw))


__all__ = ["PersistedPrediction", "PredictionService", "PredictionWriter"]
