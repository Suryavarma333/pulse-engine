from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.app.detection.realtime import RealtimeDetector, RealtimeDetectorConfig
from agent.app.detection.strategies import ExponentialBaseline, MovingAverageBaseline
from agent.app.metrics.collector import CollectedSignals

NOW = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)


def signals(
    seconds: int,
    rate: float,
    *,
    optional: dict[str, float] | None = None,
    provider_health: dict | None = None,
) -> CollectedSignals:
    observed_at = NOW + timedelta(seconds=seconds)
    return CollectedSignals(
        observed_at=observed_at,
        origin_observed_at=observed_at,
        origin_request_rate_rps=rate,
        request_count=int(rate),
        concurrent_requests=1,
        error_rate=0,
        p50_latency_ms=2,
        p95_latency_ms=4,
        p99_latency_ms=5,
        checkout_p99_latency_ms=3,
        checkout_success_rate=1,
        load_shedding_level=0,
        optional_values=optional or {},
        provider_health=provider_health
        or {
            "demo_app": {
                "status": "healthy",
                "freshness_seconds": 0,
                "included": True,
                "details": {},
            }
        },
    )


def config(**overrides) -> RealtimeDetectorConfig:
    values = {
        "environment": "test",
        "baseline_window_seconds": 60,
        "minimum_samples": 3,
        "maximum_samples": 10,
        "acceleration_threshold_rps2": 2,
        "entry_ratio_threshold": 1.5,
        "exit_ratio_threshold": 1.1,
        "confidence_threshold": 0.65,
        "confirmation_count": 1,
        "reactive_load_threshold_rps": 50,
        "forecast_horizon_seconds": 10,
        "rps_per_instance": 20,
        "maximum_capacity": 3,
        **overrides,
    }
    return RealtimeDetectorConfig(**values)


def test_baseline_strategies_have_exact_deterministic_results() -> None:
    assert MovingAverageBaseline().calculate([10, 20, 30]) == 20
    assert ExponentialBaseline(alpha=0.5).calculate([10, 20, 30]) == 22.5
    with pytest.raises(ValueError, match="at least one"):
        MovingAverageBaseline().calculate([])


def test_flat_and_insufficient_vectors_do_not_trigger() -> None:
    detector = RealtimeDetector(config())

    decisions = [detector.evaluate(signals(second, 10)) for second in (0, 1, 2, 3)]

    assert decisions[0].reason_code == "insufficient_samples"
    assert all(not decision.trigger for decision in decisions)
    assert decisions[-1].reason_code == "entry_threshold_not_met"


def test_irregular_spacing_uses_actual_elapsed_seconds() -> None:
    detector = RealtimeDetector(
        config(
            acceleration_threshold_rps2=1,
            entry_ratio_threshold=1.1,
            exit_ratio_threshold=0.9,
        )
    )
    detector.evaluate(signals(0, 10))
    detector.evaluate(signals(2, 10))

    decision = detector.evaluate(signals(8, 22))

    assert decision.snapshot.request_rate_change_rps == 12
    assert decision.snapshot.request_acceleration_rps2 == 2
    assert decision.trigger is True


def test_noisy_crossing_must_rebuild_the_full_confirmation_window() -> None:
    detector = RealtimeDetector(config(confirmation_count=2))
    detector.evaluate(signals(0, 10))
    detector.evaluate(signals(1, 10))
    first_crossing = detector.evaluate(signals(2, 25))
    noise = detector.evaluate(signals(3, 11))
    second_crossing = detector.evaluate(signals(4, 26))

    assert first_crossing.reason_code == "awaiting_confirmation"
    assert noise.reason_code == "entry_threshold_not_met"
    assert second_crossing.trigger is False
    assert second_crossing.confirmation_count == 1


def test_disagreeing_leading_signals_lower_confidence_below_gate() -> None:
    detector = RealtimeDetector(config(confidence_threshold=0.85))
    health = {
        "demo_app": {"status": "healthy"},
        "simulated": {"status": "simulated"},
    }
    detector.evaluate(signals(0, 10, optional={"queue_depth": 10}, provider_health=health))
    detector.evaluate(signals(1, 10, optional={"queue_depth": 10}, provider_health=health))
    decision = detector.evaluate(
        signals(
            2,
            25,
            optional={
                "edge_request_rate_rps": 20,
                "queue_depth": 5,
                "concurrent_sessions": 3,
                "login_rate_rps": 1,
                "cpu_utilization_pct": 10,
            },
            provider_health=health,
        )
    )

    assert decision.trigger is False
    assert decision.reason_code == "confidence_below_threshold"
    assert decision.snapshot.signal_details["leading_signals_agreeing"] == 0


def test_accelerating_vector_triggers_before_reactive_comparator_and_recovers() -> None:
    detector = RealtimeDetector(config())
    detector.evaluate(signals(0, 10))
    detector.evaluate(signals(1, 10))
    trigger = detector.evaluate(
        signals(
            2,
            25,
            optional={
                "edge_request_rate_rps": 30,
                "queue_depth": 20,
                "cpu_utilization_pct": 60,
            },
            provider_health={
                "demo_app": {"status": "healthy"},
                "simulated": {"status": "simulated"},
            },
        )
    )
    recovery = detector.evaluate(signals(3, 5))

    assert trigger.trigger is True
    assert trigger.candidate is not None
    assert trigger.candidate.predicted_peak_rps == 175
    assert trigger.reactive_comparator_crossed_at is None
    assert trigger.snapshot.reactive_comparator_crossed is False
    assert recovery.reason_code == "realtime_recovered"
    assert recovery.trigger is False


def test_reactive_comparator_records_only_its_first_crossing_timestamp() -> None:
    detector = RealtimeDetector(config(reactive_load_threshold_rps=20))
    detector.evaluate(signals(0, 10))
    first = detector.evaluate(signals(1, 21))
    second = detector.evaluate(signals(2, 30))

    assert first.reactive_comparator_crossed_at == NOW + timedelta(seconds=1)
    assert second.reactive_comparator_crossed_at == first.reactive_comparator_crossed_at


def test_sample_history_is_capacity_and_time_bounded() -> None:
    detector = RealtimeDetector(
        config(maximum_samples=3, baseline_window_seconds=5, minimum_samples=2)
    )
    for second in (0, 1, 2, 3, 10):
        detector.evaluate(signals(second, 10))

    checkpoint = detector.checkpoint()
    assert len(checkpoint.samples) == 1


def test_duplicate_or_backwards_timestamps_are_rejected_without_window_growth() -> None:
    detector = RealtimeDetector(config())
    detector.evaluate(signals(0, 10))

    with pytest.raises(ValueError, match="strictly increasing"):
        detector.evaluate(signals(0, 20))

    assert len(detector.checkpoint().samples) == 1
