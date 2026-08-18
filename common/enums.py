from enum import IntEnum, StrEnum


class SheddingLevel(IntEnum):
    NORMAL = 0
    DISABLE_RECOMMENDATIONS = 1
    CACHED_NONCRITICAL = 2
    RATE_LIMIT_NONCRITICAL = 3


class EndpointMode(StrEnum):
    NORMAL = "normal"
    DISABLED = "disabled"
    CACHED = "cached"
    RATE_LIMITED = "rate_limited"


class PredictionMode(StrEnum):
    SCHEDULED = "scheduled"
    REALTIME = "realtime"


class ExecutionMode(StrEnum):
    DRY_RUN = "dry_run"
    LIVE = "live"


class ResponseIntent(StrEnum):
    DETECTOR = "detector"
    PROTECT = "protect"
    PREWARM = "prewarm"
    RECOVER = "recover"
    HOLD = "hold"


class ControlState(StrEnum):
    NORMAL = "normal"
    WATCH = "watch"
    PREWARM = "prewarm"
    PROTECT = "protect"
    RECOVERY = "recovery"
    COOLDOWN = "cooldown"
    FAILURE_SAFE = "failure_safe"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    DRY_RUN = "dry_run"
    NOOP = "noop"
    CAPPED = "capped"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


class ProviderStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    SIMULATED = "simulated"


class DemoRunStatus(StrEnum):
    RUNNING = "running"
    PENDING_EVALUATION = "pending_evaluation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PredictionStatus(StrEnum):
    ACTIVE = "active"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
