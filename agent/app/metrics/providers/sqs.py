from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from common.contracts import SignalReading
from common.enums import ProviderStatus
from common.time import ensure_utc


class SqsSignalProvider:
    name = "sqs"
    required = False

    def __init__(
        self,
        client: Any,
        queue_url: str,
        *,
        timeout_seconds: float,
        freshness_limit_seconds: float,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.freshness_limit_seconds = freshness_limit_seconds
        self._client = client
        self._queue_url = queue_url
        self._previous: tuple[datetime, int] | None = None

    async def read(self, now: datetime) -> SignalReading:
        now = ensure_utc(now, field_name="now")
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._client.get_queue_attributes,
                QueueUrl=self._queue_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            ),
            timeout=self.timeout_seconds,
        )
        attributes = response.get("Attributes", {})
        visible = int(attributes.get("ApproximateNumberOfMessages", 0))
        in_flight = int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0))
        depth = visible + in_flight
        growth = 0.0
        if self._previous is not None:
            previous_at, previous_depth = self._previous
            elapsed = (now - previous_at).total_seconds()
            if elapsed > 0:
                growth = (depth - previous_depth) / elapsed
        self._previous = (now, depth)
        return SignalReading(
            source=self.name,
            observed_at=now,
            status=ProviderStatus.HEALTHY,
            values={"queue_depth": float(depth), "queue_growth_per_second": growth},
            freshness_seconds=0,
            details={"visible": visible, "in_flight": in_flight},
        )


__all__ = ["SqsSignalProvider"]
