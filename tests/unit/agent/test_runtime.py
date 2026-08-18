from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agent.app.config import AgentSettings
from agent.app.main import build_signal_providers
from agent.app.runtime import AgentRuntime
from common.enums import ExecutionMode

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


class Clock:
    def now(self):
        return NOW


def test_runtime_assembles_bounded_signal_resources_with_injected_clock() -> None:
    settings = AgentSettings(
        signal_buffer_capacity=7,
        simulated_signal_max_ttl_seconds=45,
        simulated_signal_future_tolerance_seconds=2,
    )
    clock = Clock()
    runtime = AgentRuntime.create(settings, clock=clock)
    assert runtime.settings is settings
    assert runtime.clock is clock
    assert runtime.simulated_signals.capacity == 7
    assert runtime.simulated_signals.max_ttl_seconds == 45
    assert runtime.simulated_signals.future_tolerance_seconds == 2


def test_runtime_defaults_to_a_system_clock() -> None:
    runtime = AgentRuntime.create(AgentSettings())
    assert runtime.clock.now().tzinfo is not None


def test_dry_run_provider_assembly_is_credential_free_and_marks_omissions() -> None:
    calls = []
    runtime = AgentRuntime.create(AgentSettings())
    providers, omitted = build_signal_providers(
        runtime.settings,
        runtime,
        aws_client_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert [provider.name for provider in providers] == ["demo_app", "simulated"]
    assert omitted == ("cloudfront", "cloudwatch_cpu", "sessions", "sqs")
    assert calls == []


def test_live_provider_assembly_uses_bounded_sdk_config_and_explicit_sources() -> None:
    calls = []

    class Client:
        pass

    def factory(service, **kwargs):
        calls.append((service, kwargs))
        return Client()

    settings = AgentSettings(
        execution_mode=ExecutionMode.LIVE,
        aws_region="ap-south-1",
        asg_name="pulse-asg",
        cloudwatch_cpu_enabled=True,
        cloudfront_distribution_id="DIST123",
        sqs_queue_url="https://sqs.ap-south-1.amazonaws.com/example",
        session_signal_url="https://signals.example.test/session",
        aws_sdk_connect_timeout_seconds=2,
        aws_sdk_read_timeout_seconds=4,
        aws_sdk_max_attempts=2,
    )
    runtime = AgentRuntime.create(settings)
    providers, omitted = build_signal_providers(
        settings,
        runtime,
        aws_client_factory=factory,
    )

    assert [provider.name for provider in providers] == [
        "demo_app",
        "simulated",
        "cloudwatch_cpu",
        "cloudfront",
        "sqs",
        "sessions",
    ]
    assert omitted == ()
    assert [service for service, _ in calls] == ["cloudwatch", "sqs"]
    config = calls[0][1]["config"]
    assert config.connect_timeout == 2
    assert config.read_timeout == 4
    assert config.retries["total_max_attempts"] == 2
    asyncio.run(runtime.close())
