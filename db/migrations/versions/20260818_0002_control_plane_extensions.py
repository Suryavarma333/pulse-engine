"""Add control-plane run correlation and evaluation fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0002"
down_revision: str | None = "20260818_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demo_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("scenario_name", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("baseline_type", sa.String(length=40), nullable=False),
        sa.Column("execution_mode", sa.String(length=12), nullable=False),
        sa.Column("configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reactive_comparator_crossed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkout_p99_latency_ms", sa.Float(), nullable=True),
        sa.Column("checkout_success_rate", sa.Float(), nullable=True),
        sa.Column("detection_lead_seconds", sa.Float(), nullable=True),
        sa.Column("provisioning_efficiency_pct", sa.Float(), nullable=True),
        sa.Column("prediction_error_pct", sa.Float(), nullable=True),
        sa.Column("overprovisioned_instance_minutes", sa.Float(), nullable=True),
        sa.Column("underprovisioned_seconds", sa.Float(), nullable=True),
        sa.Column("error_rate", sa.Float(), nullable=True),
        sa.Column("recovery_duration_seconds", sa.Float(), nullable=True),
        sa.Column("cost_duration_seconds", sa.Float(), nullable=True),
        sa.Column("formula_version", sa.String(length=20), nullable=False),
        sa.Column("locust_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('scheduled', 'realtime')", name="ck_demo_runs_valid_mode"),
        sa.CheckConstraint(
            "execution_mode IN ('dry_run', 'live')",
            name="ck_demo_runs_valid_execution_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'pending_evaluation', 'completed', 'failed', 'cancelled')",
            name="ck_demo_runs_valid_status",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="ck_demo_runs_valid_interval"
        ),
        sa.CheckConstraint(
            "checkout_p99_latency_ms IS NULL OR checkout_p99_latency_ms >= 0",
            name="ck_demo_runs_nonnegative_checkout_p99",
        ),
        sa.CheckConstraint(
            "checkout_success_rate IS NULL OR "
            "(checkout_success_rate >= 0 AND checkout_success_rate <= 1)",
            name="ck_demo_runs_checkout_success_rate_range",
        ),
        sa.CheckConstraint(
            "provisioning_efficiency_pct IS NULL OR "
            "(provisioning_efficiency_pct >= 0 AND provisioning_efficiency_pct <= 100)",
            name="ck_demo_runs_provisioning_efficiency_range",
        ),
        sa.CheckConstraint(
            "prediction_error_pct IS NULL OR prediction_error_pct >= 0",
            name="ck_demo_runs_nonnegative_prediction_error",
        ),
        sa.CheckConstraint(
            "overprovisioned_instance_minutes IS NULL OR overprovisioned_instance_minutes >= 0",
            name="ck_demo_runs_nonnegative_overprovisioned_minutes",
        ),
        sa.CheckConstraint(
            "underprovisioned_seconds IS NULL OR underprovisioned_seconds >= 0",
            name="ck_demo_runs_nonnegative_underprovisioned_seconds",
        ),
        sa.CheckConstraint(
            "error_rate IS NULL OR (error_rate >= 0 AND error_rate <= 1)",
            name="ck_demo_runs_error_rate_range",
        ),
        sa.CheckConstraint(
            "recovery_duration_seconds IS NULL OR recovery_duration_seconds >= 0",
            name="ck_demo_runs_nonnegative_recovery_duration",
        ),
        sa.CheckConstraint(
            "cost_duration_seconds IS NULL OR cost_duration_seconds >= 0",
            name="ck_demo_runs_nonnegative_cost_duration",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_demo_runs"),
        sa.UniqueConstraint("idempotency_key", name="uq_demo_runs_idempotency_key"),
    )
    op.create_index(
        "ix_demo_runs_environment_started_at", "demo_runs", ["environment", "started_at"]
    )
    op.create_index(
        "ix_demo_runs_scenario_status", "demo_runs", ["scenario_name", "status"]
    )
    op.create_index(
        "uq_demo_runs_active_environment",
        "demo_runs",
        ["environment"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'pending_evaluation')"),
    )

    op.add_column("traffic_snapshots", sa.Column("demo_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "traffic_snapshots",
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "traffic_snapshots",
        sa.Column("concurrent_requests", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "traffic_snapshots",
        sa.Column("request_rate_change_rps", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "traffic_snapshots", sa.Column("checkout_p99_latency_ms", sa.Float(), nullable=True)
    )
    op.add_column(
        "traffic_snapshots", sa.Column("checkout_success_rate", sa.Float(), nullable=True)
    )
    op.add_column(
        "traffic_snapshots",
        sa.Column(
            "reactive_comparator_crossed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_traffic_snapshots_nonnegative_request_count",
        "traffic_snapshots",
        "request_count >= 0",
    )
    op.create_check_constraint(
        "ck_traffic_snapshots_nonnegative_concurrent_requests",
        "traffic_snapshots",
        "concurrent_requests >= 0",
    )
    op.create_check_constraint(
        "ck_traffic_snapshots_checkout_success_rate_range",
        "traffic_snapshots",
        "checkout_success_rate IS NULL OR "
        "(checkout_success_rate >= 0 AND checkout_success_rate <= 1)",
    )
    op.create_foreign_key(
        "fk_traffic_snapshots_demo_run_id_demo_runs",
        "traffic_snapshots",
        "demo_runs",
        ["demo_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_traffic_snapshots_demo_run_observed_at",
        "traffic_snapshots",
        ["demo_run_id", "observed_at"],
    )

    op.add_column("surge_predictions", sa.Column("demo_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "surge_predictions",
        sa.Column("environment", sa.String(length=32), server_default="local", nullable=False),
    )
    op.add_column("surge_predictions", sa.Column("correlation_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE surge_predictions SET correlation_id = id WHERE correlation_id IS NULL")
    op.alter_column("surge_predictions", "correlation_id", nullable=False)
    op.add_column(
        "surge_predictions",
        sa.Column("reactive_comparator_crossed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "surge_predictions",
        sa.Column("formula_version", sa.String(length=20), server_default="v1", nullable=False),
    )
    op.create_foreign_key(
        "fk_surge_predictions_demo_run_id_demo_runs",
        "surge_predictions",
        "demo_runs",
        ["demo_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_surge_predictions_demo_run_created_at",
        "surge_predictions",
        ["demo_run_id", "created_at"],
    )
    op.create_index(
        "ix_surge_predictions_environment_created_at",
        "surge_predictions",
        ["environment", "created_at"],
    )
    op.create_index(
        "ix_surge_predictions_correlation_id", "surge_predictions", ["correlation_id"]
    )

    op.add_column("scaling_actions", sa.Column("demo_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "scaling_actions",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_scaling_actions_demo_run_id_demo_runs",
        "scaling_actions",
        "demo_runs",
        ["demo_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_scaling_actions_demo_run_requested_at",
        "scaling_actions",
        ["demo_run_id", "requested_at"],
    )
    op.create_check_constraint(
        "ck_scaling_actions_nonnegative_applied_capacity",
        "scaling_actions",
        "applied_desired_capacity IS NULL OR applied_desired_capacity >= 0",
    )

    op.add_column("load_shedding_events", sa.Column("demo_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_load_shedding_events_demo_run_id_demo_runs",
        "load_shedding_events",
        "demo_runs",
        ["demo_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_load_shedding_events_demo_run_started_at",
        "load_shedding_events",
        ["demo_run_id", "started_at"],
    )

    op.alter_column("traffic_snapshots", "request_count", server_default=None)
    op.alter_column("traffic_snapshots", "concurrent_requests", server_default=None)
    op.alter_column("traffic_snapshots", "request_rate_change_rps", server_default=None)
    op.alter_column("traffic_snapshots", "reactive_comparator_crossed", server_default=None)
    op.alter_column("surge_predictions", "environment", server_default=None)
    op.alter_column("surge_predictions", "formula_version", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_load_shedding_events_demo_run_started_at", table_name="load_shedding_events"
    )
    op.drop_constraint(
        "fk_load_shedding_events_demo_run_id_demo_runs",
        "load_shedding_events",
        type_="foreignkey",
    )
    op.drop_column("load_shedding_events", "demo_run_id")

    op.drop_index("ix_scaling_actions_demo_run_requested_at", table_name="scaling_actions")
    op.drop_constraint(
        "ck_scaling_actions_nonnegative_applied_capacity", "scaling_actions", type_="check"
    )
    op.drop_constraint(
        "fk_scaling_actions_demo_run_id_demo_runs", "scaling_actions", type_="foreignkey"
    )
    op.drop_column("scaling_actions", "reconciled_at")
    op.drop_column("scaling_actions", "demo_run_id")

    op.drop_index("ix_surge_predictions_correlation_id", table_name="surge_predictions")
    op.drop_index("ix_surge_predictions_environment_created_at", table_name="surge_predictions")
    op.drop_index("ix_surge_predictions_demo_run_created_at", table_name="surge_predictions")
    op.drop_constraint(
        "fk_surge_predictions_demo_run_id_demo_runs", "surge_predictions", type_="foreignkey"
    )
    op.drop_column("surge_predictions", "formula_version")
    op.drop_column("surge_predictions", "reactive_comparator_crossed_at")
    op.drop_column("surge_predictions", "correlation_id")
    op.drop_column("surge_predictions", "environment")
    op.drop_column("surge_predictions", "demo_run_id")

    op.drop_index("ix_traffic_snapshots_demo_run_observed_at", table_name="traffic_snapshots")
    op.drop_constraint(
        "fk_traffic_snapshots_demo_run_id_demo_runs", "traffic_snapshots", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_traffic_snapshots_checkout_success_rate_range",
        "traffic_snapshots",
        type_="check",
    )
    op.drop_constraint(
        "ck_traffic_snapshots_nonnegative_concurrent_requests",
        "traffic_snapshots",
        type_="check",
    )
    op.drop_constraint(
        "ck_traffic_snapshots_nonnegative_request_count", "traffic_snapshots", type_="check"
    )
    op.drop_column("traffic_snapshots", "reactive_comparator_crossed")
    op.drop_column("traffic_snapshots", "checkout_success_rate")
    op.drop_column("traffic_snapshots", "checkout_p99_latency_ms")
    op.drop_column("traffic_snapshots", "request_rate_change_rps")
    op.drop_column("traffic_snapshots", "concurrent_requests")
    op.drop_column("traffic_snapshots", "request_count")
    op.drop_column("traffic_snapshots", "demo_run_id")

    op.drop_index("uq_demo_runs_active_environment", table_name="demo_runs")
    op.drop_index("ix_demo_runs_scenario_status", table_name="demo_runs")
    op.drop_index("ix_demo_runs_environment_started_at", table_name="demo_runs")
    op.drop_table("demo_runs")
