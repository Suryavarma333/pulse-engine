from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from common.contracts import SignalReading
from common.enums import ProviderStatus
from common.time import ensure_utc


class CloudWatchMetricProvider:
    """Reads configured CloudWatch metrics through an injected boto3-compatible client."""

    required = False

    def __init__(
        self,
        *,
        name: str,
        client: Any,
        metric_queries: Mapping[str, dict[str, Any]],
        timeout_seconds: float,
        freshness_limit_seconds: float,
        period_seconds: int = 60,
        statistics: Mapping[str, str] | None = None,
        value_divisors: Mapping[str, float] | None = None,
    ) -> None:
        if not metric_queries:
            raise ValueError("metric_queries must not be empty")
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.freshness_limit_seconds = freshness_limit_seconds
        self._client = client
        self._queries = dict(metric_queries)
        self._period_seconds = period_seconds
        self._statistics = dict(statistics or {})
        self._divisors = dict(value_divisors or {})

    async def read(self, now: datetime) -> SignalReading:
        now = ensure_utc(now, field_name="now")
        queries = []
        id_to_key: dict[str, str] = {}
        for index, (key, query) in enumerate(self._queries.items()):
            query_id = f"m{index}"
            id_to_key[query_id] = key
            queries.append(
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": query,
                        "Period": self._period_seconds,
                        "Stat": self._statistics.get(key, "Average"),
                    },
                    "ReturnData": True,
                }
            )
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._client.get_metric_data,
                MetricDataQueries=queries,
                StartTime=now - timedelta(seconds=max(self._period_seconds * 2, 60)),
                EndTime=now,
                ScanBy="TimestampDescending",
                MaxDatapoints=max(1, len(queries)),
            ),
            timeout=self.timeout_seconds,
        )
        values: dict[str, float] = {}
        newest: datetime | None = None
        for result in response.get("MetricDataResults", []):
            samples = result.get("Values", [])
            timestamps = result.get("Timestamps", [])
            if not samples or not timestamps:
                continue
            key = id_to_key.get(result.get("Id"))
            if key is None:
                continue
            observed_at = ensure_utc(timestamps[0], field_name="cloudwatch_timestamp")
            divisor = self._divisors.get(key, 1.0)
            if divisor <= 0:
                raise ValueError("CloudWatch value divisor must be positive")
            values[key] = float(samples[0]) / divisor
            if newest is None or observed_at > newest:
                newest = observed_at
        if newest is None:
            return SignalReading(
                source=self.name,
                observed_at=now,
                status=ProviderStatus.UNAVAILABLE,
                values={},
                freshness_seconds=0,
                details={"reason": "no_datapoints"},
            )
        freshness = max(0.0, (now - newest).total_seconds())
        return SignalReading(
            source=self.name,
            observed_at=newest,
            status=(
                ProviderStatus.HEALTHY
                if freshness <= self.freshness_limit_seconds
                else ProviderStatus.STALE
            ),
            values=values,
            freshness_seconds=freshness,
            details={"metric_count": len(values)},
        )


class CloudFrontSignalProvider(CloudWatchMetricProvider):
    """Reads CloudFront request volume as a per-second leading request rate."""

    def __init__(
        self,
        client: Any,
        distribution_id: str,
        *,
        timeout_seconds: float,
        freshness_limit_seconds: float,
        period_seconds: int = 60,
    ) -> None:
        super().__init__(
            name="cloudfront",
            client=client,
            metric_queries={
                "edge_request_rate_rps": {
                    "Namespace": "AWS/CloudFront",
                    "MetricName": "Requests",
                    "Dimensions": [
                        {"Name": "DistributionId", "Value": distribution_id},
                        {"Name": "Region", "Value": "Global"},
                    ],
                }
            },
            timeout_seconds=timeout_seconds,
            freshness_limit_seconds=freshness_limit_seconds,
            period_seconds=period_seconds,
            statistics={"edge_request_rate_rps": "Sum"},
            value_divisors={"edge_request_rate_rps": float(period_seconds)},
        )


class CloudWatchCpuSignalProvider(CloudWatchMetricProvider):
    """Reads EC2 CPU pressure for the configured Auto Scaling Group."""

    def __init__(
        self,
        client: Any,
        asg_name: str,
        *,
        timeout_seconds: float,
        freshness_limit_seconds: float,
        period_seconds: int = 60,
    ) -> None:
        super().__init__(
            name="cloudwatch_cpu",
            client=client,
            metric_queries={
                "cpu_utilization_pct": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [
                        {"Name": "AutoScalingGroupName", "Value": asg_name}
                    ],
                }
            },
            timeout_seconds=timeout_seconds,
            freshness_limit_seconds=freshness_limit_seconds,
            period_seconds=period_seconds,
        )


__all__ = [
    "CloudFrontSignalProvider",
    "CloudWatchCpuSignalProvider",
    "CloudWatchMetricProvider",
]
