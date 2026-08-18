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
