from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from common.contracts import ResponseCommand
from common.enums import SheddingLevel


class SheddingControlError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SheddingControlResult:
    changed: bool
    level: SheddingLevel
    event_id: str | None
    endpoint_policies: dict[str, str]


@dataclass(frozen=True, slots=True)
class DemoControlStatus:
    ready: bool
    level: SheddingLevel


class DemoAppSheddingClient:
    """Authenticated, server-side client for the demo app's authoritative tier control."""

    def __init__(
        self,
        *,
        base_url: str,
        control_token: SecretStr | str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._token = (
            control_token.get_secret_value()
            if isinstance(control_token, SecretStr)
            else control_token
        )
        if not self._token:
            raise ValueError("control_token is required")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    async def apply(
        self,
        *,
        command: ResponseCommand,
        level: SheddingLevel,
    ) -> SheddingControlResult:
        payload: dict[str, Any] = {
            "level": int(level),
            "reason_code": command.reason_code,
            "reasoning": command.reasoning,
            "changed_by": "agent",
            "signal_evidence": command.signal_evidence,
            "correlation_id": str(command.correlation_id),
            "prediction_id": str(command.prediction_id) if command.prediction_id else None,
            "trigger_snapshot_id": command.trigger_snapshot_id,
            "demo_run_id": str(command.demo_run_id) if command.demo_run_id else None,
        }
        try:
            response = await self._client.post(
                "/internal/load-shedding",
                headers={"X-Pulse-Control-Token": self._token},
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SheddingControlError(
                f"demo app control request failed: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            retryable = response.status_code in {409, 429, 503} or response.status_code >= 500
            code = _error_code(response)
            raise SheddingControlError(
                f"demo app control rejected the transition: {code}",
                retryable=retryable,
                status_code=response.status_code,
            )
        body = response.json()
        return SheddingControlResult(
            changed=bool(body.get("changed")),
            level=SheddingLevel(int(body["level"])),
            event_id=None if body.get("event_id") is None else str(body["event_id"]),
            endpoint_policies={
                str(key): str(value)
                for key, value in body["endpoint_policies"].items()
            },
        )

    async def read_status(self) -> DemoControlStatus:
        """Verify the authoritative tier control dependency without mutating it."""

        try:
            response = await self._client.get("/health")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise SheddingControlError(
                f"demo app health request failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise SheddingControlError(
                f"demo app health is unavailable: {_error_code(response)}",
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            )
        body = response.json()
        return DemoControlStatus(
            ready=bool(body.get("ready")),
            level=SheddingLevel(int(body.get("load_shedding", {}).get("level", 0))),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _error_code(response: httpx.Response) -> str:
    try:
        value = response.json().get("code", "HTTP_ERROR")
    except (ValueError, AttributeError):
        value = "HTTP_ERROR"
    return str(value)[:80]


__all__ = [
    "DemoControlStatus",
    "DemoAppSheddingClient",
    "SheddingControlError",
    "SheddingControlResult",
]
