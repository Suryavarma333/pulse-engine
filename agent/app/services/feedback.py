from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from common.contracts import ResultMetrics
from common.enums import DemoRunStatus, ProviderStatus
from common.time import Clock, ensure_utc

FORMULA_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class EvaluationBundle:
    snapshots: tuple[Any, ...]
    predictions: tuple[Any, ...]
    actions: tuple[Any, ...]
    shedding_events: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    metrics: ResultMetrics
    summary: dict[str, Any]
    primary_prediction_id: UUID | None
    actual_start_at: datetime | None
    actual_peak_at: datetime | None
    actual_peak_rps: float | None


class FeedbackDataSource(Protocol):
    async def load_for_run(self, run: Any, *, limit: int) -> EvaluationBundle: ...


class DemoRunStore(Protocol):
    async def get(self, run_id: UUID) -> Any | None: ...

    async def complete(self, **values: Any) -> Any: ...

    async def list_pending_evaluation(self, *, limit: int = 50) -> list[Any]: ...


class PredictionEvaluationStore(Protocol):
    async def update_evaluation(self, **values: Any) -> Any: ...


class FeedbackEvaluator:
    """Pure, formula-versioned result calculator; missing evidence is never invented."""

    formula_version = FORMULA_VERSION

    def evaluate(self, run: Any, bundle: EvaluationBundle) -> EvaluationOutcome:
        warnings: list[str] = []
        snapshots = sorted(bundle.snapshots, key=lambda item: item.observed_at)
        predictions = sorted(bundle.predictions, key=lambda item: item.created_at)
        started_at = ensure_utc(run.started_at, field_name="started_at")
        ended_at = (
            ensure_utc(run.ended_at, field_name="ended_at")
            if run.ended_at is not None
            else None
        )
        if ended_at is None or run.status == DemoRunStatus.RUNNING.value:
            warnings.append("incomplete_run")

        baseline = self._baseline(snapshots, predictions)
        onset_ratio = self._positive_number(run.thresholds.get("onset_ratio", 1.2), 1.2)
        actual_start = next(
            (
                item.observed_at
                for item in snapshots
                if baseline is not None and item.origin_request_rate_rps >= baseline * onset_ratio
            ),
            None,
        )
        peak = max(snapshots, key=lambda item: item.origin_request_rate_rps, default=None)
        actual_peak_at = None if peak is None else peak.observed_at
        actual_peak_rps = None if peak is None else float(peak.origin_request_rate_rps)
        if not snapshots:
            warnings.append("missing_snapshot_evidence")

        primary = predictions[0] if predictions else None
        prediction_error = self._prediction_error(primary, actual_peak_rps, warnings)
        lead = self._lead_seconds(run, primary, warnings)
        checkout_p99, checkout_success = self._checkout_metrics(
            run.locust_summary, snapshots, warnings
        )
        error_rate = self._error_rate(run.locust_summary, snapshots, warnings)
        capacity_per_instance = self._positive_number(
            run.configuration.get("rps_per_instance", 25.0), 25.0
        )
        floor = max(0, int(run.configuration.get("minimum_desired_capacity", 1)))
        efficiency, over_minutes, under_seconds = self._provisioning(
            snapshots, capacity_per_instance, warnings
        )
        recovery_duration, cost_duration = self._recovery_durations(
            snapshots,
            actual_peak_at=actual_peak_at,
            floor=floor,
            ended_at=ended_at,
            warnings=warnings,
        )
        if run.baseline_type != "reactive_only":
            warnings.append("paired_reactive_baseline_unavailable")

        unique_warnings = tuple(dict.fromkeys(warnings))
        metrics = ResultMetrics(
            checkout_p99_latency_ms=checkout_p99,
            checkout_success_rate=checkout_success,
            detection_lead_seconds=lead,
            provisioning_efficiency_pct=efficiency,
            prediction_error_pct=prediction_error,
            overprovisioned_instance_minutes=over_minutes,
            underprovisioned_seconds=under_seconds,
            error_rate=error_rate,
            recovery_duration_seconds=recovery_duration,
            cost_duration_seconds=cost_duration,
            formula_version=self.formula_version,
            warnings=unique_warnings,
        )
        references = {
            "run_id": str(run.id),
            "snapshot_ids": [item.id for item in snapshots[:100]],
            "snapshot_count": len(snapshots),
            "prediction_ids": [str(item.id) for item in predictions[:100]],
            "prediction_count": len(predictions),
            "action_ids": [str(item.id) for item in bundle.actions[:100]],
            "action_count": len(bundle.actions),
            "shedding_event_ids": [
                str(item.id) for item in bundle.shedding_events[:100]
            ],
            "shedding_event_count": len(bundle.shedding_events),
            "references_truncated": any(
                len(items) > 100
                for items in (
                    snapshots,
                    predictions,
                    bundle.actions,
                    bundle.shedding_events,
                )
            ),
        }
        summary = {
            "formula_version": self.formula_version,
            "observation": {
                "started_at": started_at.isoformat(),
                "ended_at": None if ended_at is None else ended_at.isoformat(),
                "actual_onset_at": (
                    None if actual_start is None else actual_start.isoformat()
                ),
                "actual_peak_at": (
                    None if actual_peak_at is None else actual_peak_at.isoformat()
                ),
                "actual_peak_rps": actual_peak_rps,
                "baseline_rps": baseline,
                "onset_ratio": onset_ratio,
            },
            "metrics": metrics.model_dump(mode="json"),
            "raw": {
                "configuration": run.configuration,
                "thresholds": run.thresholds,
                "locust_summary": run.locust_summary,
            },
            "references": references,
            "formulas": self.formulas(),
            "warnings": list(unique_warnings),
            "comparability": {
                "baseline_type": run.baseline_type,
                "error_rate_reduction_pct": None,
                "complete": not any(
                    warning in unique_warnings
                    for warning in ("incomplete_run", "missing_snapshot_evidence")
                ),
            },
        }
        return EvaluationOutcome(
            metrics=metrics,
            summary=summary,
            primary_prediction_id=None if primary is None else primary.id,
            actual_start_at=actual_start,
            actual_peak_at=actual_peak_at,
            actual_peak_rps=actual_peak_rps,
        )

    @staticmethod
    def formulas() -> dict[str, str]:
        return {
            "checkout_success_rate": "successful checkout requests / checkout attempts",
            "detection_lead_seconds": "reactive comparator time - prediction creation time",
            "prediction_error_pct": "abs(predicted peak - actual peak) / actual peak * 100",
            "provisioning_efficiency_pct": (
                "required instance-minutes / provisioned instance-minutes * 100"
            ),
            "overprovisioned_instance_minutes": "integral max(provisioned - required, 0)",
            "underprovisioned_seconds": "seconds where required instances exceed provisioned",
            "error_rate": "failed requests / total requests",
            "recovery_duration_seconds": "actual peak until capacity floor and tier zero",
            "cost_duration_seconds": "actual peak until last above-floor capacity interval",
        }

    @staticmethod
    def _baseline(snapshots: list[Any], predictions: list[Any]) -> float | None:
        if predictions:
            return float(predictions[0].baseline_rps)
        if snapshots:
            return float(snapshots[0].baseline_request_rate_rps)
        return None

    @staticmethod
    def _prediction_error(
        prediction: Any | None,
        actual_peak_rps: float | None,
        warnings: list[str],
    ) -> float | None:
        if prediction is None:
            warnings.append("missing_prediction")
            return None
        if actual_peak_rps is None or actual_peak_rps <= 0:
            warnings.append("prediction_error_zero_denominator")
            return None
        return abs(float(prediction.predicted_peak_rps) - actual_peak_rps) / actual_peak_rps * 100

    @staticmethod
    def _lead_seconds(run: Any, prediction: Any | None, warnings: list[str]) -> float | None:
        if prediction is None:
            return None
        comparator = run.reactive_comparator_crossed_at or prediction.reactive_comparator_crossed_at
        if comparator is None:
            warnings.append("reactive_comparator_unavailable")
            return None
        return (
            ensure_utc(comparator, field_name="reactive_comparator_crossed_at")
            - ensure_utc(prediction.created_at, field_name="prediction.created_at")
        ).total_seconds()

    @staticmethod
    def _checkout_metrics(
        raw: dict[str, Any], snapshots: list[Any], warnings: list[str]
    ) -> tuple[float | None, float | None]:
        checkout = raw.get("checkout", raw)
        attempts = checkout.get("attempts", checkout.get("request_count"))
        successes = checkout.get("successes")
        p99 = checkout.get("p99_latency_ms")
        if attempts is not None and successes is not None:
            attempts_value = float(attempts)
            if attempts_value <= 0:
                warnings.append("checkout_success_zero_denominator")
                success = None
            else:
                success = min(1.0, max(0.0, float(successes) / attempts_value))
        else:
            values = [
                float(item.checkout_success_rate)
                for item in snapshots
                if item.checkout_success_rate is not None
            ]
            success = sum(values) / len(values) if values else None
            warnings.append("checkout_raw_counts_unavailable")
        if p99 is None:
            values = [
                float(item.checkout_p99_latency_ms)
                for item in snapshots
                if item.checkout_p99_latency_ms is not None
            ]
            p99_value = max(values) if values else None
            warnings.append("checkout_raw_latency_distribution_unavailable")
        else:
            p99_value = max(0.0, float(p99))
        return p99_value, success

    @staticmethod
    def _error_rate(
        raw: dict[str, Any], snapshots: list[Any], warnings: list[str]
    ) -> float | None:
        requests = raw.get("request_count")
        failures = raw.get("failure_count")
        if requests is not None and failures is not None:
            denominator = float(requests)
            if denominator <= 0:
                warnings.append("error_rate_zero_denominator")
                return None
            return min(1.0, max(0.0, float(failures) / denominator))
        values = [float(item.error_rate) for item in snapshots if item.error_rate is not None]
        if not values:
            warnings.append("error_rate_unavailable")
            return None
        warnings.append("error_rate_uses_snapshot_average")
        return sum(values) / len(values)

    @staticmethod
    def _provisioning(
        snapshots: list[Any],
        capacity_per_instance: float,
        warnings: list[str],
    ) -> tuple[float | None, float | None, float | None]:
        required_minutes = 0.0
        provisioned_minutes = 0.0
        over_minutes = 0.0
        under_seconds = 0.0
        intervals = 0
        for current, following in zip(snapshots, snapshots[1:], strict=False):
            seconds = (following.observed_at - current.observed_at).total_seconds()
            if seconds <= 0 or current.asg_desired_capacity is None:
                continue
            required = math.ceil(float(current.origin_request_rate_rps) / capacity_per_instance)
            provisioned = int(current.asg_desired_capacity)
            minutes = seconds / 60
            required_minutes += required * minutes
            provisioned_minutes += provisioned * minutes
            over_minutes += max(provisioned - required, 0) * minutes
            if required > provisioned:
                under_seconds += seconds
            intervals += 1
        if intervals == 0:
            warnings.append("provisioning_intervals_unavailable")
            return None, None, None
        if provisioned_minutes <= 0:
            warnings.append("provisioning_efficiency_zero_denominator")
            efficiency = None
        else:
            efficiency = min(100.0, required_minutes / provisioned_minutes * 100)
        return efficiency, over_minutes, under_seconds

    @staticmethod
    def _recovery_durations(
        snapshots: list[Any],
        *,
        actual_peak_at: datetime | None,
        floor: int,
        ended_at: datetime | None,
        warnings: list[str],
    ) -> tuple[float | None, float | None]:
        if actual_peak_at is None:
            return None, None
        after_peak = [item for item in snapshots if item.observed_at >= actual_peak_at]
        recovered = next(
            (
                item
                for item in after_peak
                if item.asg_desired_capacity is not None
                and item.asg_desired_capacity <= floor
                and item.load_shedding_level == 0
            ),
            None,
        )
        if recovered is None:
            warnings.append("recovery_not_observed")
            recovery = None
        else:
            recovery = (recovered.observed_at - actual_peak_at).total_seconds()
        above_floor = [
            item
            for item in after_peak
            if (item.asg_desired_capacity or 0) > floor or item.load_shedding_level > 0
        ]
        if above_floor:
            cost = (above_floor[-1].observed_at - actual_peak_at).total_seconds()
        elif ended_at is not None:
            cost = 0.0
        else:
            warnings.append("cost_duration_unavailable")
            cost = None
        return recovery, cost

    @staticmethod
    def _positive_number(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default


class FeedbackService:
    def __init__(
        self,
        *,
        runs: DemoRunStore,
        data_source: FeedbackDataSource,
        predictions: PredictionEvaluationStore,
        evaluator: FeedbackEvaluator | None = None,
        max_rows: int = 1_000,
    ) -> None:
        self._runs = runs
        self._data = data_source
        self._predictions = predictions
        self._evaluator = evaluator or FeedbackEvaluator()
        self._max_rows = min(max(max_rows, 1), 1_000)

    async def evaluate_run(self, run_id: UUID) -> Any:
        run = await self._runs.get(run_id)
        if run is None:
            raise LookupError(f"demo run {run_id} was not found")
        bundle = await self._data.load_for_run(run, limit=self._max_rows)
        outcome = self._evaluator.evaluate(run, bundle)
        persisted = await self._runs.complete(
            run_id=run.id,
            status=DemoRunStatus.COMPLETED,
            ended_at=run.ended_at,
            locust_summary=run.locust_summary,
            notes=run.notes,
            results=outcome.metrics,
            result_summary=outcome.summary,
        )
        if outcome.primary_prediction_id is not None:
            await self._predictions.update_evaluation(
                prediction_id=outcome.primary_prediction_id,
                actual_start_at=outcome.actual_start_at,
                actual_peak_at=outcome.actual_peak_at,
                actual_peak_rps=outcome.actual_peak_rps,
                detection_lead_seconds=outcome.metrics.detection_lead_seconds,
                prediction_error_pct=outcome.metrics.prediction_error_pct,
                overprovisioned_instance_minutes=(
                    outcome.metrics.overprovisioned_instance_minutes
                ),
                underprovisioned_seconds=outcome.metrics.underprovisioned_seconds,
            )
        return persisted


@dataclass(frozen=True, slots=True)
class FeedbackCycleResult:
    evaluated_run_ids: tuple[UUID, ...]
    deferred_run_ids: tuple[UUID, ...]


class FeedbackWorker:
    name = "feedback"

    def __init__(
        self,
        *,
        runs: DemoRunStore,
        service: FeedbackService,
        clock: Clock,
        horizon_seconds: int,
    ) -> None:
        if not 0 <= horizon_seconds <= 86_400:
            raise ValueError("horizon_seconds is out of bounds")
        self._runs = runs
        self._service = service
        self._clock = clock
        self._horizon_seconds = horizon_seconds
        self.status = ProviderStatus.DEGRADED
        self.detail = "not_started"

    async def run_once(self) -> FeedbackCycleResult:
        now = self._clock.now()
        evaluated: list[UUID] = []
        deferred: list[UUID] = []
        try:
            runs = await self._runs.list_pending_evaluation(limit=50)
            for run in runs:
                if run.ended_at is None or (
                    now - ensure_utc(run.ended_at)
                ).total_seconds() < self._horizon_seconds:
                    deferred.append(run.id)
                    continue
                await self._service.evaluate_run(run.id)
                evaluated.append(run.id)
        except Exception as exc:
            self.status = ProviderStatus.UNAVAILABLE
            self.detail = f"feedback_cycle_failed:{type(exc).__name__}"
            raise
        self.status = ProviderStatus.HEALTHY
        self.detail = f"evaluated:{len(evaluated)};deferred:{len(deferred)}"
        return FeedbackCycleResult(tuple(evaluated), tuple(deferred))

    async def run(self, stop_event: Any, *, poll_seconds: float) -> None:
        import asyncio

        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue


__all__ = [
    "EvaluationBundle",
    "EvaluationOutcome",
    "FORMULA_VERSION",
    "FeedbackCycleResult",
    "FeedbackEvaluator",
    "FeedbackService",
    "FeedbackWorker",
]
