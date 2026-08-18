from __future__ import annotations

from common.enums import EndpointMode, SheddingLevel

CRITICAL_ENDPOINTS = frozenset({"checkout"})

_POLICY_MATRIX: dict[SheddingLevel, dict[str, EndpointMode]] = {
    SheddingLevel.NORMAL: {
        "checkout": EndpointMode.NORMAL,
        "recommendations": EndpointMode.NORMAL,
        "catalog": EndpointMode.NORMAL,
    },
    SheddingLevel.DISABLE_RECOMMENDATIONS: {
        "checkout": EndpointMode.NORMAL,
        "recommendations": EndpointMode.DISABLED,
        "catalog": EndpointMode.NORMAL,
    },
    SheddingLevel.CACHED_NONCRITICAL: {
        "checkout": EndpointMode.NORMAL,
        "recommendations": EndpointMode.DISABLED,
        "catalog": EndpointMode.CACHED,
    },
    SheddingLevel.RATE_LIMIT_NONCRITICAL: {
        "checkout": EndpointMode.NORMAL,
        "recommendations": EndpointMode.RATE_LIMITED,
        "catalog": EndpointMode.RATE_LIMITED,
    },
}


def endpoint_mode(endpoint: str, level: SheddingLevel) -> EndpointMode:
    mode = _POLICY_MATRIX[level][endpoint]
    if endpoint in CRITICAL_ENDPOINTS and mode is not EndpointMode.NORMAL:
        raise RuntimeError(f"Critical endpoint {endpoint!r} cannot be shed")
    return mode


def policy_snapshot(level: SheddingLevel) -> dict[str, str]:
    return {endpoint: endpoint_mode(endpoint, level).value for endpoint in _POLICY_MATRIX[level]}


for _level in SheddingLevel:
    for _critical_endpoint in CRITICAL_ENDPOINTS:
        assert endpoint_mode(_critical_endpoint, _level) is EndpointMode.NORMAL
