from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.config import Config
from botocore.exceptions import (
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from common.contracts import CapacityDecision, CapacityState
from common.enums import ActionStatus, ExecutionMode, ProviderStatus
from common.time import ensure_utc

_SECRET_PATTERN = re.compile(
    r"(?i)(aws_access_key_id|aws_secret_access_key|session_token|authorization|credential|"
    r"password|secret|token)\s*[=:]\s*\S+"
)


@dataclass(frozen=True, slots=True)
class CapacityObservation:
    state: CapacityState
    provider_request_id: str | None = None


class AutoScalingCapacityAdapter:
    """The single EC2 Auto Scaling mutation boundary used by Pulse.

    Dry-run construction deliberately does not import or initialize boto3, so local
    operation neither discovers credentials nor makes a network request.
    """

    def __init__(
        self,
        *,
        execution_mode: ExecutionMode,
        target_resource: str,
        region: str | None,
        global_ceiling: int,
        simulated_capacity: int,
        client: Any | None = None,
        connect_timeout_seconds: float = 1.0,
        read_timeout_seconds: float = 3.0,
        max_attempts: int = 3,
    ) -> None:
        if not target_resource:
            raise ValueError("target_resource is required")
        if not 1 <= global_ceiling <= 100:
            raise ValueError("global_ceiling must be between 1 and 100")
        if not 0 <= simulated_capacity <= global_ceiling:
            raise ValueError("simulated_capacity must be within the global ceiling")
        if execution_mode is ExecutionMode.LIVE and not region:
            raise ValueError("live execution requires an AWS region")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("AWS SDK timeouts must be positive")
        if not 1 <= max_attempts <= 10:
            raise ValueError("AWS SDK attempts must be between 1 and 10")

        self.execution_mode = execution_mode
        self.target_resource = target_resource
        self.global_ceiling = global_ceiling
        self._simulated_capacity = simulated_capacity
        self._client = client
        if execution_mode is ExecutionMode.LIVE and self._client is None:
            import boto3

            self._client = boto3.client(
                "autoscaling",
                region_name=region,
                config=Config(
                    connect_timeout=connect_timeout_seconds,
                    read_timeout=read_timeout_seconds,
                    retries={
                        "mode": "standard",
                        "total_max_attempts": max_attempts,
                    },
                ),
            )

    async def read_capacity(self, *, observed_at: datetime) -> CapacityObservation:
        observed_at = ensure_utc(observed_at, field_name="observed_at")
        if self.execution_mode is ExecutionMode.DRY_RUN:
            return CapacityObservation(
                state=CapacityState(
                    desired=self._simulated_capacity,
                    in_service=self._simulated_capacity,
                    pending=0,
                    observed_at=observed_at,
                    provider_status=ProviderStatus.SIMULATED,
                )
            )

        assert self._client is not None
        response = await asyncio.to_thread(
            self._client.describe_auto_scaling_groups,
            AutoScalingGroupNames=[self.target_resource],
        )
        groups = response.get("AutoScalingGroups", [])
        if len(groups) != 1:
            raise RuntimeError("configured Auto Scaling group was not found")
        group = groups[0]
        instances = group.get("Instances", [])
        in_service = sum(item.get("LifecycleState") == "InService" for item in instances)
        pending = sum(
            str(item.get("LifecycleState", "")).startswith("Pending")
            for item in instances
        )
        return CapacityObservation(
            state=CapacityState(
                desired=int(group["DesiredCapacity"]),
                in_service=in_service,
                pending=pending,
                observed_at=observed_at,
                provider_status=ProviderStatus.HEALTHY,
            ),
            provider_request_id=_request_id(response),
        )

    async def execute(
        self,
        *,
        requested_capacity: int,
        effective_ceiling: int,
        observed_at: datetime,
    ) -> CapacityDecision:
        ensure_utc(observed_at, field_name="observed_at")
        if requested_capacity < 0:
            raise ValueError("requested_capacity must be nonnegative")
        ceiling = min(self.global_ceiling, effective_ceiling)
        if ceiling < 1:
            raise ValueError("effective_ceiling must be positive")
        target = min(requested_capacity, ceiling)
        capped = target != requested_capacity

        if self.execution_mode is ExecutionMode.DRY_RUN:
            if capped:
                status = ActionStatus.CAPPED
            elif target == self._simulated_capacity:
                status = ActionStatus.NOOP
            else:
                status = ActionStatus.DRY_RUN
            self._simulated_capacity = target
            return CapacityDecision(
                requested=requested_capacity,
                applied=target,
                ceiling=ceiling,
                execution_mode=self.execution_mode,
                status=status,
            )

        try:
            current = await self.read_capacity(observed_at=observed_at)
            if current.state.desired == target:
                return CapacityDecision(
                    requested=requested_capacity,
                    applied=target,
                    ceiling=ceiling,
                    execution_mode=self.execution_mode,
                    status=ActionStatus.CAPPED if capped else ActionStatus.NOOP,
                    provider_request_id=current.provider_request_id,
                )
            assert self._client is not None
            try:
                response = await asyncio.to_thread(
                    self._client.set_desired_capacity,
                    AutoScalingGroupName=self.target_resource,
                    DesiredCapacity=target,
                    HonorCooldown=True,
                )
            except Exception as exc:
                return CapacityDecision(
                    requested=requested_capacity,
                    applied=None,
                    ceiling=ceiling,
                    execution_mode=self.execution_mode,
                    status=(
                        ActionStatus.UNKNOWN
                        if _is_ambiguous_mutation_error(exc)
                        else ActionStatus.FAILED
                    ),
                    sanitized_error=sanitize_provider_error(exc),
                )
            return CapacityDecision(
                requested=requested_capacity,
                applied=target,
                ceiling=ceiling,
                execution_mode=self.execution_mode,
                status=ActionStatus.CAPPED if capped else ActionStatus.SUCCEEDED,
                provider_request_id=_request_id(response),
            )
        except Exception as exc:
            return CapacityDecision(
                requested=requested_capacity,
                applied=None,
                ceiling=ceiling,
                execution_mode=self.execution_mode,
                status=ActionStatus.FAILED,
                sanitized_error=sanitize_provider_error(exc),
            )


def sanitize_provider_error(exc: Exception) -> str:
    code = type(exc).__name__
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        code = str(error.get("Code") or code)
        message = str(error.get("Message") or "provider request failed")
    else:
        message = str(exc) or "provider request failed"
    sanitized = _SECRET_PATTERN.sub(r"\1=[REDACTED]", f"{code}: {message}")
    return sanitized[:2_000]


def _is_ambiguous_mutation_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionClosedError,
            EndpointConnectionError,
            ReadTimeoutError,
        ),
    )


def _request_id(response: dict[str, Any]) -> str | None:
    value = response.get("ResponseMetadata", {}).get("RequestId")
    return None if value is None else str(value)[:160]


__all__ = [
    "AutoScalingCapacityAdapter",
    "CapacityObservation",
    "sanitize_provider_error",
]
