from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from agent.app.detection.strategies import BaselineStrategy, MovingAverageBaseline
from agent.app.metrics.collector import CollectedSignals
from common.contracts import SurgePredictionCandidate
from common.enums import PredictionMode
from common.time import ensure_utc
from db.models import TrafficSnapshot


@dataclass(frozen=True, slots=True)
class RealtimeDetectorConfig:
    environment: str = "local"
    baseline_window_seconds: int = 60
    minimum_samples: int = 5
    maximum_samples: int = 1_000
    acceleration_threshold_rps2: float = 2.0
    entry_ratio_threshold: float = 1.5
    exit_ratio_threshold: float = 1.15
    confidence_threshold: float = 0.65
    confirmation_count: int = 3
    reactive_cpu_threshold_pct: float = 70.0
    reactive_load_threshold_rps: float = 50.0
    forecast_horizon_seconds: int = 30
    rps_per_instance: float = 25.0
    maximum_capacity: int = 3

    def __post_init__(self) -> None:
        if self.baseline_window_seconds <= 0:
            raise ValueError("baseline_window_seconds must be positive")
        if self.minimum_samples < 2 or self.maximum_samples < self.minimum_samples:
            raise ValueError("sample bounds are invalid")
        if self.acceleration_threshold_rps2 <= 0:
            raise ValueError("acceleration threshold must be positive")
        if self.entry_ratio_threshold <= 1:
            raise ValueError("entry ratio must exceed one")
        if not 0 <= self.exit_ratio_threshold < self.entry_ratio_threshold:
            raise ValueError("exit ratio must be lower than entry ratio")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("confidence threshold must be in [0, 1]")
        if self.confirmation_count <= 0:
            raise ValueError("confirmation_count must be positive")
        if self.rps_per_instance <= 0 or self.maximum_capacity <= 0:
            raise ValueError("capacity configuration must be positive")


@dataclass(frozen=True, slots=True)
class DetectorSample:
    observed_at: datetime
    origin_rate_rps: float
    queue_depth: float | None
    concurrent_sessions: float | None
    login_rate_rps: float | None
    cpu_utilization_pct: float | None


@dataclass(frozen=True, slots=True)
class DetectorCheckpoint:
    samples: tuple[DetectorSample, ...]
    confirmations: int
    active: bool
    prediction_emitted: bool
    comparator_crossed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RealtimeDecision:
    snapshot: TrafficSnapshot
    trigger: bool
    reason_code: str
    reasoning: str
    confidence: float
    confirmation_count: int
    candidate: SurgePredictionCandidate | None
    reactive_comparator_crossed_at: datetime | None


class RealtimeDetector:
    """Stateful bounded detector with asymmetric entry/exit hysteresis."""

    def __init__(
        self,
        config: RealtimeDetectorConfig,
        *,
        baseline_strategy: BaselineStrategy | None = None,
    ) -> None:
        self.config = config
        self._baseline = baseline_strategy or MovingAverageBaseline()
        self._samples: deque[DetectorSample] = deque(maxlen=config.maximum_samples)
        self._confirmations = 0
        self._active = False
        self._prediction_emitted = False
        self._comparator_crossed_at: datetime | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> RealtimeDetector:
        from agent.app.detection.strategies import ExponentialBaseline

        config = RealtimeDetectorConfig(
            environment=settings.environment,
            baseline_window_seconds=settings.baseline_window_seconds,
            minimum_samples=settings.minimum_samples,
            maximum_samples=settings.realtime_window_max_samples,
            acceleration_threshold_rps2=settings.acceleration_threshold_rps2,
            entry_ratio_threshold=settings.entry_ratio_threshold,
            exit_ratio_threshold=settings.exit_ratio_threshold,
            confidence_threshold=settings.confidence_threshold,
            confirmation_count=settings.confirmation_count,
            reactive_cpu_threshold_pct=settings.reactive_cpu_threshold_pct,
            reactive_load_threshold_rps=settings.reactive_load_threshold_rps,
            forecast_horizon_seconds=settings.forecast_horizon_seconds,
            rps_per_instance=settings.rps_per_instance,
            maximum_capacity=settings.global_instance_ceiling,
        )
        strategy: BaselineStrategy
        if settings.baseline_strategy == "exponential":
            strategy = ExponentialBaseline(settings.exponential_smoothing_alpha)
        else:
            strategy = MovingAverageBaseline()
        return cls(config, baseline_strategy=strategy)

    def checkpoint(self) -> DetectorCheckpoint:
        return DetectorCheckpoint(
            samples=tuple(self._samples),
            confirmations=self._confirmations,
            active=self._active,
            prediction_emitted=self._prediction_emitted,
            comparator_crossed_at=self._comparator_crossed_at,
        )

    def restore(self, checkpoint: DetectorCheckpoint) -> None:
        self._samples = deque(checkpoint.samples, maxlen=self.config.maximum_samples)
        self._confirmations = checkpoint.confirmations
        self._active = checkpoint.active
        self._prediction_emitted = checkpoint.prediction_emitted
        self._comparator_crossed_at = checkpoint.comparator_crossed_at

    def evaluate(self, signals: CollectedSignals) -> RealtimeDecision:
        observed_at = ensure_utc(signals.observed_at, field_name="observed_at")
        self._prune(observed_at)
        if self._samples and observed_at <= self._samples[-1].observed_at:
            raise ValueError("real-time samples must have strictly increasing timestamps")

        prior = tuple(self._samples)
        previous = prior[-1] if prior else None
        baseline_rates = [sample.origin_rate_rps for sample in prior]
        baseline = (
            self._baseline.calculate(baseline_rates)
            if baseline_rates
            else signals.origin_request_rate_rps
        )
        change = (
            signals.origin_request_rate_rps - previous.origin_rate_rps if previous else 0.0
        )
        elapsed = (observed_at - previous.observed_at).total_seconds() if previous else 0.0
        acceleration = change / elapsed if elapsed > 0 else 0.0
        ratio = signals.origin_request_rate_rps / baseline if baseline > 0 else 1.0
        edge_rate = signals.optional_values.get("edge_request_rate_rps")
        edge_ratio = (
            edge_rate / signals.origin_request_rate_rps
            if edge_rate is not None and signals.origin_request_rate_rps > 0
            else None
        )
        queue_growth = self._growth(
            signals.optional_values.get("queue_depth"),
            previous.queue_depth if previous else None,
            elapsed,
        )
        session_growth = self._growth(
            signals.optional_values.get("concurrent_sessions"),
            previous.concurrent_sessions if previous else None,
            elapsed,
        )
        login_growth = self._growth(
            signals.optional_values.get("login_rate_rps"),
            previous.login_rate_rps if previous else None,
            elapsed,
        )

        sample_count = len(prior) + 1
        sufficient = sample_count >= self.config.minimum_samples
        core_qualifies = (
            sufficient
            and acceleration >= self.config.acceleration_threshold_rps2
            and ratio >= self.config.entry_ratio_threshold
        )
        if core_qualifies:
            self._confirmations = min(self.config.confirmation_count, self._confirmations + 1)
        elif not self._active:
            self._confirmations = 0

        leading_agreement, leading_available = self._leading_agreement(
            edge_ratio=edge_ratio,
            queue_growth=queue_growth,
            session_growth=session_growth,
            login_growth=login_growth,
            cpu=signals.optional_values.get("cpu_utilization_pct"),
        )
        confidence = self._confidence(
            acceleration=acceleration,
            ratio=ratio,
            sample_count=sample_count,
            leading_agreement=leading_agreement,
            leading_available=leading_available,
            provider_health=signals.provider_health,
        )
        confirmed = self._confirmations >= self.config.confirmation_count
        trigger = (
            core_qualifies
            and confirmed
            and confidence >= self.config.confidence_threshold
            and not self._prediction_emitted
        )
        if trigger:
            self._active = True
            self._prediction_emitted = True

        recovered = self._active and ratio < self.config.exit_ratio_threshold and acceleration <= 0
        if recovered:
            self._active = False
            self._prediction_emitted = False
            self._confirmations = 0

        comparator_now = (
            signals.optional_values.get("cpu_utilization_pct", 0)
            >= self.config.reactive_cpu_threshold_pct
            or signals.origin_request_rate_rps >= self.config.reactive_load_threshold_rps
        )
        if comparator_now and self._comparator_crossed_at is None:
            self._comparator_crossed_at = observed_at

        sample = DetectorSample(
            observed_at=observed_at,
            origin_rate_rps=signals.origin_request_rate_rps,
            queue_depth=signals.optional_values.get("queue_depth"),
            concurrent_sessions=signals.optional_values.get("concurrent_sessions"),
            login_rate_rps=signals.optional_values.get("login_rate_rps"),
            cpu_utilization_pct=signals.optional_values.get("cpu_utilization_pct"),
        )
        self._samples.append(sample)

        reason_code, reasoning = self._reason(
            trigger=trigger,
            recovered=recovered,
            sufficient=sufficient,
            core_qualifies=core_qualifies,
            confirmed=confirmed,
            confidence=confidence,
            leading_agreement=leading_agreement,
            leading_available=leading_available,
        )
        evidence: dict[str, Any] = {
            "environment": self.config.environment,
            "baseline_strategy": self._baseline.name,
            "sample_count": sample_count,
            "minimum_samples": self.config.minimum_samples,
            "sample_sufficient": sufficient,
            "baseline_rps": round(baseline, 6),
            "current_rps": round(signals.origin_request_rate_rps, 6),
            "request_rate_change_rps": round(change, 6),
            "elapsed_seconds": round(elapsed, 6),
            "request_acceleration_rps2": round(acceleration, 6),
            "current_to_baseline_ratio": round(ratio, 6),
            "edge_to_origin_ratio": None if edge_ratio is None else round(edge_ratio, 6),
            "queue_growth_per_second": None if queue_growth is None else round(queue_growth, 6),
            "session_growth_per_second": (
                None if session_growth is None else round(session_growth, 6)
            ),
            "login_growth_per_second": None if login_growth is None else round(login_growth, 6),
            "confirmation_count": self._confirmations,
            "confirmation_required": self.config.confirmation_count,
            "leading_signals_available": leading_available,
            "leading_signals_agreeing": leading_agreement,
            "confidence": round(confidence, 6),
            "reactive_comparator_crossed_at": (
                self._comparator_crossed_at.isoformat()
                if self._comparator_crossed_at is not None
                else None
            ),
            "provider_health": signals.provider_health,
            "reason_code": reason_code,
        }
        snapshot = signals.to_snapshot(
            window_seconds=self.config.baseline_window_seconds,
            baseline_request_rate_rps=max(0.0, baseline),
            request_rate_change_rps=change,
            request_acceleration_rps2=acceleration,
            queue_growth_per_second=queue_growth,
            reactive_comparator_crossed=comparator_now,
            evidence=evidence,
        )
        candidate = self._candidate(
            signals=signals,
            basis_start=prior[0].observed_at if prior else observed_at - timedelta(microseconds=1),
            baseline=baseline,
            acceleration=acceleration,
            confidence=confidence,
            evidence=evidence,
            reasoning=reasoning,
        ) if trigger else None
        return RealtimeDecision(
            snapshot=snapshot,
            trigger=trigger,
            reason_code=reason_code,
            reasoning=reasoning,
            confidence=confidence,
            confirmation_count=self._confirmations,
            candidate=candidate,
            reactive_comparator_crossed_at=self._comparator_crossed_at,
        )

    def _candidate(
        self,
        *,
        signals: CollectedSignals,
        basis_start: datetime,
        baseline: float,
        acceleration: float,
        confidence: float,
        evidence: dict[str, Any],
        reasoning: str,
    ) -> SurgePredictionCandidate:
        horizon = self.config.forecast_horizon_seconds
        peak = max(
            signals.origin_request_rate_rps,
            signals.origin_request_rate_rps + max(0.0, acceleration) * horizon,
        )
        capacity = min(
            self.config.maximum_capacity,
            max(1, math.ceil(peak / self.config.rps_per_instance)),
        )
        return SurgePredictionCandidate(
            demo_run_id=signals.demo_run_id,
            mode=PredictionMode.REALTIME,
            environment=self.config.environment,
            basis_window_start=basis_start,
            basis_window_end=signals.observed_at,
            predicted_start_at=signals.observed_at,
            predicted_peak_at=signals.observed_at + timedelta(seconds=horizon),
            baseline_rps=max(0.0, baseline),
            predicted_peak_rps=peak,
            confidence=confidence,
            recommended_capacity=capacity,
            reason_code="realtime_acceleration_confirmed",
            reasoning=reasoning,
            signal_evidence=evidence,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.config.baseline_window_seconds)
        while self._samples and self._samples[0].observed_at < cutoff:
            self._samples.popleft()

    @staticmethod
    def _growth(current: float | None, previous: float | None, elapsed: float) -> float | None:
        if current is None or previous is None or elapsed <= 0:
            return None
        return (current - previous) / elapsed

    @staticmethod
    def _leading_agreement(
        *,
        edge_ratio: float | None,
        queue_growth: float | None,
        session_growth: float | None,
        login_growth: float | None,
        cpu: float | None,
    ) -> tuple[int, int]:
        checks = [
            edge_ratio is not None and edge_ratio >= 1,
            queue_growth is not None and queue_growth > 0,
            session_growth is not None and session_growth > 0,
            login_growth is not None and login_growth > 0,
            cpu is not None and cpu >= 50,
        ]
        availability = [
            edge_ratio is not None,
            queue_growth is not None,
            session_growth is not None,
            login_growth is not None,
            cpu is not None,
        ]
        return sum(checks), sum(availability)

    def _confidence(
        self,
        *,
        acceleration: float,
        ratio: float,
        sample_count: int,
        leading_agreement: int,
        leading_available: int,
        provider_health: dict[str, dict[str, Any]],
    ) -> float:
        acceleration_score = min(
            1.0, max(0.0, acceleration / self.config.acceleration_threshold_rps2)
        )
        confirmation_score = min(1.0, self._confirmations / self.config.confirmation_count)
        acceleration_confirmation = acceleration_score * confirmation_score
        ratio_score = min(
            1.0,
            max(0.0, (ratio - 1.0) / (self.config.entry_ratio_threshold - 1.0)),
        )
        leading_score = (
            leading_agreement / leading_available if leading_available else 0.0
        )
        sufficiency_score = min(1.0, sample_count / self.config.minimum_samples)
        fresh = sum(
            item.get("status") in {"healthy", "simulated"}
            for item in provider_health.values()
            if item.get("status")
        )
        total = sum(bool(item.get("status")) for item in provider_health.values())
        freshness_score = fresh / total if total else 0.0
        score = (
            0.30 * acceleration_confirmation
            + 0.25 * ratio_score
            + 0.20 * leading_score
            + 0.15 * sufficiency_score
            + 0.10 * freshness_score
        )
        return min(1.0, max(0.0, score))

    @staticmethod
    def _reason(
        *,
        trigger: bool,
        recovered: bool,
        sufficient: bool,
        core_qualifies: bool,
        confirmed: bool,
        confidence: float,
        leading_agreement: int,
        leading_available: int,
    ) -> tuple[str, str]:
        if trigger:
            agreement = f"{leading_agreement}/{leading_available}" if leading_available else "0/0"
            return (
                "realtime_acceleration_confirmed",
                "Origin acceleration and baseline ratio remained above entry thresholds; "
                f"confirmation and confidence gates passed with leading agreement {agreement}.",
            )
        if recovered:
            return "realtime_recovered", "Load fell below the exit hysteresis threshold."
        if not sufficient:
            return "insufficient_samples", "The bounded window does not yet contain enough samples."
        if not core_qualifies:
            return "entry_threshold_not_met", "Acceleration or baseline ratio is below entry."
        if not confirmed:
            return "awaiting_confirmation", "Entry evidence has not reached the confirmation count."
        return (
            "confidence_below_threshold",
            f"Evidence confidence {confidence:.3f} is below the configured threshold.",
        )


__all__ = [
    "DetectorCheckpoint",
    "DetectorSample",
    "RealtimeDecision",
    "RealtimeDetector",
    "RealtimeDetectorConfig",
]
