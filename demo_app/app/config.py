from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: str = "local"
    database_url: str | None = None
    control_token: str = "local-demo-token"
    metrics_window_seconds: int = 10
    metrics_max_samples: int = 20_000
    noncritical_rate_per_second: float = 2.0
    noncritical_rate_burst: int = 4

    def __post_init__(self) -> None:
        if not self.environment or len(self.environment) > 32:
            raise ValueError("environment must contain between 1 and 32 characters")
        if not self.control_token:
            raise ValueError("control_token must not be empty")
        if not 1 <= self.metrics_window_seconds <= 3_600:
            raise ValueError("metrics_window_seconds must be between 1 and 3600")
        if not 100 <= self.metrics_max_samples <= 1_000_000:
            raise ValueError("metrics_max_samples must be between 100 and 1000000")
        if self.noncritical_rate_per_second <= 0:
            raise ValueError("noncritical_rate_per_second must be positive")
        if not 1 <= self.noncritical_rate_burst <= 100_000:
            raise ValueError("noncritical_rate_burst must be between 1 and 100000")

    @classmethod
    def from_environment(cls) -> AppSettings:
        return cls(
            environment=os.getenv("PULSE_ENVIRONMENT", "local"),
            database_url=os.getenv("PULSE_DATABASE_URL") or None,
            control_token=os.getenv("PULSE_CONTROL_TOKEN", "local-demo-token"),
            metrics_window_seconds=int(os.getenv("PULSE_METRICS_WINDOW_SECONDS", "10")),
            metrics_max_samples=int(os.getenv("PULSE_METRICS_MAX_SAMPLES", "20000")),
            noncritical_rate_per_second=float(os.getenv("PULSE_NONCRITICAL_RATE_PER_SECOND", "2")),
            noncritical_rate_burst=int(os.getenv("PULSE_NONCRITICAL_RATE_BURST", "4")),
        )
