from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ScenarioName = Literal["scheduled-diwali", "sudden-spike"]
BaselineType = Literal["pulse", "reactive_only"]


@dataclass(frozen=True, slots=True)
class EndpointRequest:
    method: Literal["GET", "POST"]
    path: str
    weight: int
    name: str


@dataclass(frozen=True, slots=True)
class LoadStage:
    starts_at_seconds: int
    users: int
    spawn_rate: float
    label: str


@dataclass(frozen=True, slots=True)
class SignalFrame:
    at_seconds: int
    edge_request_rate_rps: float | None = None
    queue_depth: int | None = None
    concurrent_sessions: int | None = None
    login_rate_rps: float | None = None
    cpu_utilization_pct: float | None = None


@dataclass(frozen=True, slots=True)
class ScenarioPlan:
    name: ScenarioName
    seed: int
    duration_seconds: int
    endpoint_cycle: tuple[EndpointRequest, ...]
    stages: tuple[LoadStage, ...]
    pulse_signals: tuple[SignalFrame, ...]
    reactive_signals: tuple[SignalFrame, ...]

    def stage_at(self, elapsed_seconds: float) -> LoadStage | None:
        return next(
            (
                stage
                for stage in reversed(self.stages)
                if elapsed_seconds >= stage.starts_at_seconds
            ),
            None,
        )

    def signals_for(self, baseline_type: BaselineType) -> tuple[SignalFrame, ...]:
        return (
            self.pulse_signals
            if baseline_type == "pulse"
            else self.reactive_signals
        )


ENDPOINT_MIX = (
    EndpointRequest("POST", "/checkout", 5, "critical checkout"),
    EndpointRequest("GET", "/catalog", 3, "catalog browse"),
    EndpointRequest("GET", "/recommendations", 2, "recommendations"),
)


def _endpoint_cycle() -> tuple[EndpointRequest, ...]:
    return tuple(request for request in ENDPOINT_MIX for _ in range(request.weight))


def build_scenario_plan(
    name: ScenarioName,
    *,
    seed: int = 20260818,
    smoke: bool = False,
) -> ScenarioPlan:
    if seed < 0:
        raise ValueError("seed must be nonnegative")
    cycle = _endpoint_cycle()
    if name == "scheduled-diwali":
        if smoke:
            stages = (
                LoadStage(0, 2, 2, "warmup"),
                LoadStage(3, 5, 3, "announced-ramp"),
                LoadStage(7, 9, 5, "event-peak"),
                LoadStage(12, 3, 3, "recovery"),
                LoadStage(16, 0, 3, "complete"),
            )
            duration = 16
            signals = (
                SignalFrame(2, edge_request_rate_rps=8, concurrent_sessions=20),
                SignalFrame(6, edge_request_rate_rps=22, concurrent_sessions=55),
                SignalFrame(10, edge_request_rate_rps=38, cpu_utilization_pct=58),
                SignalFrame(14, edge_request_rate_rps=4, cpu_utilization_pct=24),
            )
        else:
            stages = (
                LoadStage(0, 5, 2, "warmup"),
                LoadStage(30, 20, 4, "announced-ramp-1"),
                LoadStage(75, 45, 8, "announced-ramp-2"),
                LoadStage(120, 80, 12, "event-peak"),
                LoadStage(210, 20, 8, "recovery"),
                LoadStage(270, 0, 5, "complete"),
            )
            duration = 270
            signals = (
                SignalFrame(20, edge_request_rate_rps=12, concurrent_sessions=30),
                SignalFrame(60, edge_request_rate_rps=45, concurrent_sessions=120),
                SignalFrame(105, edge_request_rate_rps=90, concurrent_sessions=260),
                SignalFrame(160, edge_request_rate_rps=120, cpu_utilization_pct=62),
                SignalFrame(230, edge_request_rate_rps=12, cpu_utilization_pct=28),
            )
        return ScenarioPlan(
            name=name,
            seed=seed,
            duration_seconds=duration,
            endpoint_cycle=cycle,
            stages=stages,
            pulse_signals=signals,
            reactive_signals=tuple(
                SignalFrame(frame.at_seconds, cpu_utilization_pct=frame.cpu_utilization_pct)
                for frame in signals
                if frame.cpu_utilization_pct is not None
            ),
        )
    if name != "sudden-spike":
        raise ValueError(f"unknown scenario: {name}")
    if smoke:
        stages = (
            LoadStage(0, 2, 2, "quiet-baseline"),
            LoadStage(5, 14, 12, "unannounced-spike"),
            LoadStage(12, 5, 8, "recovery"),
            LoadStage(16, 0, 4, "complete"),
        )
        duration = 16
        pulse = (
            SignalFrame(2, edge_request_rate_rps=5, queue_depth=2, cpu_utilization_pct=20),
            SignalFrame(4, edge_request_rate_rps=24, queue_depth=18, login_rate_rps=12),
            SignalFrame(7, edge_request_rate_rps=46, queue_depth=55, cpu_utilization_pct=48),
            SignalFrame(10, edge_request_rate_rps=60, queue_depth=80, cpu_utilization_pct=78),
            SignalFrame(14, edge_request_rate_rps=7, queue_depth=3, cpu_utilization_pct=26),
        )
    else:
        stages = (
            LoadStage(0, 5, 2, "quiet-baseline"),
            LoadStage(40, 80, 35, "unannounced-spike"),
            LoadStage(150, 25, 15, "recovery"),
            LoadStage(210, 0, 8, "complete"),
        )
        duration = 210
        pulse = (
            SignalFrame(15, edge_request_rate_rps=8, queue_depth=3, cpu_utilization_pct=18),
            SignalFrame(30, edge_request_rate_rps=38, queue_depth=25, login_rate_rps=18),
            SignalFrame(38, edge_request_rate_rps=72, queue_depth=90, login_rate_rps=42),
            SignalFrame(55, edge_request_rate_rps=120, queue_depth=180, cpu_utilization_pct=48),
            SignalFrame(90, edge_request_rate_rps=150, queue_depth=240, cpu_utilization_pct=76),
            SignalFrame(175, edge_request_rate_rps=16, queue_depth=8, cpu_utilization_pct=30),
        )
    reactive = tuple(
        SignalFrame(frame.at_seconds, cpu_utilization_pct=frame.cpu_utilization_pct)
        for frame in pulse
        if frame.cpu_utilization_pct is not None
    )
    return ScenarioPlan(name, seed, duration, cycle, stages, pulse, reactive)


__all__ = [
    "BaselineType",
    "ENDPOINT_MIX",
    "EndpointRequest",
    "LoadStage",
    "ScenarioName",
    "ScenarioPlan",
    "SignalFrame",
    "build_scenario_plan",
]
