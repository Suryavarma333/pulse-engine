from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from db import models  # noqa: F401
from db.base import Base

EXPECTED_TABLES = {
    "traffic_snapshots",
    "demo_runs",
    "scheduled_events",
    "surge_predictions",
    "surge_prediction_points",
    "scaling_actions",
    "load_shedding_events",
    "response_retries",
}


def test_all_audit_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_schema_compiles_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))


def test_control_plane_extensions_have_typed_columns_and_audit_fks() -> None:
    tables = Base.metadata.tables

    assert {
        "checkout_p99_latency_ms",
        "checkout_success_rate",
        "detection_lead_seconds",
        "provisioning_efficiency_pct",
        "prediction_error_pct",
    } <= set(tables["demo_runs"].columns.keys())
    assert {
        "demo_run_id",
        "request_count",
        "concurrent_requests",
        "request_rate_change_rps",
        "checkout_p99_latency_ms",
        "checkout_success_rate",
        "reactive_comparator_crossed",
    } <= set(tables["traffic_snapshots"].columns.keys())
    assert {
        "demo_run_id",
        "environment",
        "correlation_id",
        "reactive_comparator_crossed_at",
        "formula_version",
    } <= set(tables["surge_predictions"].columns.keys())

    for table_name in (
        "traffic_snapshots",
        "surge_predictions",
        "scaling_actions",
        "load_shedding_events",
    ):
        demo_run_id = tables[table_name].columns["demo_run_id"]
        foreign_key = next(iter(demo_run_id.foreign_keys))
        assert foreign_key.target_fullname == "demo_runs.id"
        assert foreign_key.ondelete == "SET NULL"


def test_query_indexes_cover_time_and_run_filters() -> None:
    indexes = {
        table_name: {index.name for index in table.indexes}
        for table_name, table in Base.metadata.tables.items()
    }

    assert "ix_traffic_snapshots_demo_run_observed_at" in indexes["traffic_snapshots"]
    assert "ix_surge_predictions_environment_created_at" in indexes["surge_predictions"]
    assert "ix_scaling_actions_demo_run_requested_at" in indexes["scaling_actions"]
    assert "ix_load_shedding_events_demo_run_started_at" in indexes["load_shedding_events"]
    assert "ix_demo_runs_environment_started_at" in indexes["demo_runs"]
    assert "ix_response_retries_due" in indexes["response_retries"]


def test_result_and_capacity_checks_are_registered() -> None:
    demo_run_checks = {
        constraint.name
        for constraint in Base.metadata.tables["demo_runs"].constraints
        if constraint.name
    }
    scaling_checks = {
        constraint.name
        for constraint in Base.metadata.tables["scaling_actions"].constraints
        if constraint.name
    }

    assert {
        "ck_demo_runs_checkout_success_rate_range",
        "ck_demo_runs_provisioning_efficiency_range",
        "ck_demo_runs_error_rate_range",
        "ck_demo_runs_nonnegative_recovery_duration",
        "ck_demo_runs_nonnegative_cost_duration",
    } <= demo_run_checks
    assert "ck_scaling_actions_nonnegative_applied_capacity" in scaling_checks
