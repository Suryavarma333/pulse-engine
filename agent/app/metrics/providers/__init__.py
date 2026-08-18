"""Signal-provider implementations."""

from agent.app.metrics.providers.base import SignalProvider
from agent.app.metrics.providers.cloudwatch import (
    CloudFrontSignalProvider,
    CloudWatchCpuSignalProvider,
    CloudWatchMetricProvider,
)
from agent.app.metrics.providers.demo_app import DemoAppSignalProvider
from agent.app.metrics.providers.sessions import SessionLoginSignalProvider
from agent.app.metrics.providers.simulated import (
    DuplicateSignalError,
    ExpiredSignalError,
    SignalBufferFullError,
    SimulatedSignalBuffer,
    SimulatedSignalFrame,
    SimulatedSignalProvider,
)
from agent.app.metrics.providers.sqs import SqsSignalProvider

__all__ = [
    "CloudWatchMetricProvider",
    "CloudFrontSignalProvider",
    "CloudWatchCpuSignalProvider",
    "DemoAppSignalProvider",
    "DuplicateSignalError",
    "ExpiredSignalError",
    "SessionLoginSignalProvider",
    "SignalBufferFullError",
    "SignalProvider",
    "SimulatedSignalBuffer",
    "SimulatedSignalFrame",
    "SimulatedSignalProvider",
    "SqsSignalProvider",
]
