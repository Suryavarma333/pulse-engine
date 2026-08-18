from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LocustSummary:
    request_count: int
    failure_count: int
    p99_latency_ms: float | None
    checkout_attempts: int
    checkout_successes: int
    checkout_p99_latency_ms: float | None

    def as_api_payload(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "failure_count": self.failure_count,
            "p99_latency_ms": self.p99_latency_ms,
            "checkout": {
                "attempts": self.checkout_attempts,
                "successes": self.checkout_successes,
                "p99_latency_ms": self.checkout_p99_latency_ms,
            },
        }


def read_locust_summary(csv_prefix: Path) -> LocustSummary:
    stats_path = csv_prefix.with_name(f"{csv_prefix.name}_stats.csv")
    with stats_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    aggregate = next((row for row in rows if row.get("Name") == "Aggregated"), None)
    checkout = next((row for row in rows if row.get("Name") == "critical checkout"), None)
    if aggregate is None:
        raise ValueError(f"Locust aggregate row is missing from {stats_path}")
    return LocustSummary(
        request_count=_integer(aggregate, "Request Count"),
        failure_count=_integer(aggregate, "Failure Count"),
        p99_latency_ms=_optional_float(aggregate, "99%"),
        checkout_attempts=0 if checkout is None else _integer(checkout, "Request Count"),
        checkout_successes=(
            0
            if checkout is None
            else _integer(checkout, "Request Count") - _integer(checkout, "Failure Count")
        ),
        checkout_p99_latency_ms=(
            None if checkout is None else _optional_float(checkout, "99%")
        ),
    )


def export_evidence(
    detail: dict[str, Any],
    *,
    output_directory: Path,
    stem: str,
) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / f"{stem}.json"
    markdown_path = output_directory / f"{stem}.md"
    json_path.write_text(json.dumps(detail, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics = detail.get("result_summary", {}).get("metrics", {})
    warnings = detail.get("warnings", {}).get("items", [])
    if not warnings:
        warnings = detail.get("result_summary", {}).get("warnings", [])
    lines = [
        f"# Pulse run {detail.get('id', 'unknown')}",
        "",
        f"- Scenario: `{detail.get('scenario_name', 'unknown')}`",
        f"- Baseline: `{detail.get('baseline_type', 'unknown')}`",
        f"- Execution mode: `{detail.get('execution_mode', 'unknown')}`",
        f"- Status: `{detail.get('status', 'unknown')}`",
        f"- Formula version: `{detail.get('formula_version', 'unknown')}`",
        "",
        "## Measured metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for name in (
        "checkout_p99_latency_ms",
        "checkout_success_rate",
        "detection_lead_seconds",
        "provisioning_efficiency_pct",
        "prediction_error_pct",
    ):
        lines.append(f"| {name} | {_display(metrics.get(name))} |")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- `{warning}`" for warning in warnings)
    if not warnings:
        lines.append("- None")
    lines.extend(
        [
            "",
            "> Values are emitted only from this persisted run. "
            "Missing evidence remains not available.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def _integer(row: dict[str, str], field: str) -> int:
    return int(float(row.get(field) or 0))


def _optional_float(row: dict[str, str], field: str) -> float | None:
    raw = row.get(field)
    return None if raw in {None, "", "N/A"} else float(raw)


def _display(value: Any) -> str:
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


__all__ = ["LocustSummary", "export_evidence", "read_locust_summary"]
