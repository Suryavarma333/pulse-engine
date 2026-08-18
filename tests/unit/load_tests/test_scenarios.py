from load_tests.lib.models import ENDPOINT_MIX, build_scenario_plan


def test_endpoint_mix_is_representative_and_deterministic() -> None:
    plan = build_scenario_plan("sudden-spike", seed=17)
    repeated = build_scenario_plan("sudden-spike", seed=17)

    assert plan == repeated
    assert [(item.path, item.weight) for item in ENDPOINT_MIX] == [
        ("/checkout", 5),
        ("/catalog", 3),
        ("/recommendations", 2),
    ]
    assert [request.path for request in plan.endpoint_cycle].count("/checkout") == 5
    assert len(plan.endpoint_cycle) == 10


def test_scheduled_plan_ramps_before_peak_and_correlates_signals() -> None:
    plan = build_scenario_plan("scheduled-diwali", smoke=True)

    assert plan.stages[0].label == "warmup"
    assert plan.stages[2].label == "event-peak"
    assert plan.stage_at(8).users == 9
    assert plan.stage_at(plan.duration_seconds).users == 0
    assert all(
        earlier.at_seconds < later.at_seconds
        for earlier, later in zip(plan.pulse_signals, plan.pulse_signals[1:], strict=False)
    )


def test_sudden_spike_leading_signal_precedes_reactive_comparator() -> None:
    plan = build_scenario_plan("sudden-spike", smoke=True)
    first_leading = next(
        frame.at_seconds
        for frame in plan.pulse_signals
        if frame.edge_request_rate_rps is not None and frame.edge_request_rate_rps >= 24
    )
    comparator = next(
        frame.at_seconds
        for frame in plan.reactive_signals
        if frame.cpu_utilization_pct is not None and frame.cpu_utilization_pct >= 70
    )

    assert first_leading < comparator
    assert plan.stages[1].label == "unannounced-spike"
    assert plan.stage_at(plan.stages[1].starts_at_seconds).users == 14


def test_reactive_baseline_omits_edge_queue_and_session_signals() -> None:
    plan = build_scenario_plan("sudden-spike")

    assert plan.reactive_signals
    assert all(frame.edge_request_rate_rps is None for frame in plan.reactive_signals)
    assert all(frame.queue_depth is None for frame in plan.reactive_signals)
    assert all(frame.concurrent_sessions is None for frame in plan.reactive_signals)
