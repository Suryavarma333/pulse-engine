from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: str = "local"
    database_url: str | None = None
    control_token: str = "local-demo-token"
    metrics_window_seconds: int = 10
    noncritical_rate_per_second: float = 2.0
    noncritical_rate_burst: int = 4

    @classmethod
    def from_environment(cls) -> AppSettings:
        return cls(
            environment=os.getenv("PULSE_ENVIRONMENT", "local"),
            database_url=os.getenv("PULSE_DATABASE_URL") or None,
            control_token=os.getenv("PULSE_CONTROL_TOKEN", "local-demo-token"),
            metrics_window_seconds=int(os.getenv("PULSE_METRICS_WINDOW_SECONDS", "10")),
            noncritical_rate_per_second=float(os.getenv("PULSE_NONCRITICAL_RATE_PER_SECOND", "2")),
            noncritical_rate_burst=int(os.getenv("PULSE_NONCRITICAL_RATE_BURST", "4")),
        )
