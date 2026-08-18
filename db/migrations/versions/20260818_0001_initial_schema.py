"""Create the Pulse audit and forecasting schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "traffic_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_seconds", sa.SmallInteger(), nullable=False),
        sa.Column("origin_request_rate_rps", sa.Float(), nullable=False),
        sa.Column("baseline_request_rate_rps", sa.Float(), nullable=False),
        sa.Column("request_acceleration_rps2", sa.Float(), nullable=False),
        sa.Column("edge_request_rate_rps", sa.Float(), nullable=True),
        sa.Column("queue_depth", sa.BigInteger(), nullable=True),
        sa.Column("queue_growth_per_second", sa.Float(), nullable=True),
        sa.Column("concurrent_sessions", sa.Integer(), nullable=True),
        sa.Column("login_rate_rps", sa.Float(), nullable=True),
        sa.Column("cpu_utilization_pct", sa.Float(), nullable=True),
        sa.Column("p50_latency_ms", sa.Float(), nullable=True),
        sa.Column("p95_latency_ms", sa.Float(), nullable=True),
        sa.Column("p99_latency_ms", sa.Float(), nullable=True),
        sa.Column("error_rate", sa.Float(), nullable=True),
        sa.Column("asg_desired_capacity", sa.Integer(), nullable=True),
        sa.Column("asg_in_service_capacity", sa.Integer(), nullable=True),
        sa.Column("load_shedding_level", sa.SmallInteger(), nullable=False),
        sa.Column("signal_details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("window_seconds > 0", name=op.f("ck_traffic_snapshots_positive_window")),
        sa.CheckConstraint(
            "origin_request_rate_rps >= 0",
            name=op.f("ck_traffic_snapshots_nonnegative_origin_rate"),
        ),
        sa.CheckConstraint(
            "baseline_request_rate_rps >= 0",
            name=op.f("ck_traffic_snapshots_nonnegative_baseline_rate"),
        ),
        sa.CheckConstraint(
            "error_rate IS NULL OR (error_rate >= 0 AND error_rate <= 1)",
            name=op.f("ck_traffic_snapshots_error_rate_range"),
        ),
        sa.CheckConstraint(
            "load_shedding_level >= 0 AND load_shedding_level <= 3",
            name=op.f("ck_traffic_snapshots_shedding_level_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_traffic_snapshots")),
        sa.UniqueConstraint("environment", "observed_at", name="uq_traffic_snapshots_observation"),
    )
    op.create_index(
        "ix_traffic_snapshots_environment_observed_at",
        "traffic_snapshots",
        ["environment", "observed_at"],
    )

    op.create_table(
        "scheduled_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("recurrence_rule", sa.Text(), nullable=True),
        sa.Column("expected_multiplier", sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column("baseline_rps", sa.Float(), nullable=True),
        sa.Column("expected_peak_rps", sa.Float(), nullable=True),
        sa.Column("prewarm_lead_seconds", sa.Integer(), nullable=False),
        sa.Column("peak_lead_seconds", sa.Integer(), nullable=False),
        sa.Column("scale_down_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("minimum_desired_capacity", sa.Integer(), nullable=False),
        sa.Column("peak_desired_capacity", sa.Integer(), nullable=False),
        sa.Column("max_instances_override", sa.Integer(), nullable=True),
        sa.Column("ramp_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
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
        sa.CheckConstraint(
            "ends_at > starts_at", name=op.f("ck_scheduled_events_valid_time_range")
        ),
        sa.CheckConstraint(
            "expected_multiplier >= 1", name=op.f("ck_scheduled_events_valid_multiplier")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_scheduled_events_confidence_range")
        ),
        sa.CheckConstraint(
            "peak_lead_seconds <= prewarm_lead_seconds",
            name=op.f("ck_scheduled_events_lead_time_order"),
        ),
        sa.CheckConstraint(
            "minimum_desired_capacity >= 0",
            name=op.f("ck_scheduled_events_nonnegative_minimum_capacity"),
        ),
        sa.CheckConstraint(
            "peak_desired_capacity >= minimum_desired_capacity",
            name=op.f("ck_scheduled_events_valid_peak_capacity"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_events")),
    )
    op.create_index(
        "ix_scheduled_events_status_starts_at", "scheduled_events", ["status", "starts_at"]
    )

    op.create_table(
        "surge_predictions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("scheduled_event_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("model_name", sa.String(length=80), nullable=False),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("basis_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("basis_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_peak_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline_rps", sa.Float(), nullable=False),
        sa.Column("predicted_peak_rps", sa.Float(), nullable=False),
        sa.Column("predicted_multiplier", sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column("recommended_capacity", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("signal_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("actual_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_peak_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_peak_rps", sa.Float(), nullable=True),
        sa.Column("detection_lead_seconds", sa.Integer(), nullable=True),
        sa.Column("prediction_error_pct", sa.Float(), nullable=True),
        sa.Column("overprovisioned_instance_minutes", sa.Float(), nullable=True),
        sa.Column("underprovisioned_seconds", sa.Integer(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('scheduled', 'realtime')", name=op.f("ck_surge_predictions_valid_mode")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_surge_predictions_confidence_range"),
        ),
        sa.CheckConstraint(
            "baseline_rps >= 0 AND predicted_peak_rps >= 0",
            name=op.f("ck_surge_predictions_nonnegative_rates"),
        ),
        sa.CheckConstraint(
            "recommended_capacity >= 0",
            name=op.f("ck_surge_predictions_nonnegative_recommended_capacity"),
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_event_id"],
            ["scheduled_events.id"],
            name=op.f("fk_surge_predictions_scheduled_event_id_scheduled_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_snapshot_id"],
            ["traffic_snapshots.id"],
            name=op.f("fk_surge_predictions_trigger_snapshot_id_traffic_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surge_predictions")),
    )
    op.create_index(op.f("ix_surge_predictions_created_at"), "surge_predictions", ["created_at"])
    op.create_index(op.f("ix_surge_predictions_mode"), "surge_predictions", ["mode"])
    op.create_index(
        op.f("ix_surge_predictions_scheduled_event_id"), "surge_predictions", ["scheduled_event_id"]
    )
    op.create_index(
        op.f("ix_surge_predictions_trigger_snapshot_id"),
        "surge_predictions",
        ["trigger_snapshot_id"],
    )

    op.create_table(
        "surge_prediction_points",
        sa.Column("prediction_id", sa.Uuid(), nullable=False),
        sa.Column("point_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_rps", sa.Float(), nullable=False),
        sa.Column("predicted_capacity", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["surge_predictions.id"],
            name=op.f("fk_surge_prediction_points_prediction_id_surge_predictions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "prediction_id", "point_at", name=op.f("pk_surge_prediction_points")
        ),
    )

    op.create_table(
        "scaling_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("prediction_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("scheduled_event_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_type", sa.String(length=24), nullable=False),
        sa.Column("execution_mode", sa.String(length=12), nullable=False),
        sa.Column("target_resource", sa.String(length=255), nullable=False),
        sa.Column("previous_desired_capacity", sa.Integer(), nullable=False),
        sa.Column("requested_desired_capacity", sa.Integer(), nullable=False),
        sa.Column("applied_desired_capacity", sa.Integer(), nullable=True),
        sa.Column("max_instance_ceiling", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=60), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("signal_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_request_id", sa.String(length=160), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "previous_desired_capacity >= 0",
            name=op.f("ck_scaling_actions_nonnegative_previous_capacity"),
        ),
        sa.CheckConstraint(
            "requested_desired_capacity >= 0",
            name=op.f("ck_scaling_actions_nonnegative_requested_capacity"),
        ),
        sa.CheckConstraint(
            "max_instance_ceiling >= 0",
            name=op.f("ck_scaling_actions_nonnegative_instance_ceiling"),
        ),
        sa.CheckConstraint(
            "applied_desired_capacity IS NULL OR applied_desired_capacity <= max_instance_ceiling",
            name=op.f("ck_scaling_actions_applied_within_ceiling"),
        ),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["surge_predictions.id"],
            name=op.f("fk_scaling_actions_prediction_id_surge_predictions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_event_id"],
            ["scheduled_events.id"],
            name=op.f("fk_scaling_actions_scheduled_event_id_scheduled_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_snapshot_id"],
            ["traffic_snapshots.id"],
            name=op.f("fk_scaling_actions_trigger_snapshot_id_traffic_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scaling_actions")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_scaling_actions_idempotency_key")),
    )
    op.create_index("ix_scaling_actions_correlation_id", "scaling_actions", ["correlation_id"])
    op.create_index(op.f("ix_scaling_actions_prediction_id"), "scaling_actions", ["prediction_id"])
    op.create_index(op.f("ix_scaling_actions_requested_at"), "scaling_actions", ["requested_at"])

    op.create_table(
        "load_shedding_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("prediction_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_level", sa.SmallInteger(), nullable=True),
        sa.Column("to_level", sa.SmallInteger(), nullable=False),
        sa.Column("changed_by", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=60), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("signal_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("application_status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "from_level IS NULL OR (from_level >= 0 AND from_level <= 3)",
            name=op.f("ck_load_shedding_events_from_level_range"),
        ),
        sa.CheckConstraint(
            "to_level >= 0 AND to_level <= 3", name=op.f("ck_load_shedding_events_to_level_range")
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name=op.f("ck_load_shedding_events_valid_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["surge_predictions.id"],
            name=op.f("fk_load_shedding_events_prediction_id_surge_predictions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_snapshot_id"],
            ["traffic_snapshots.id"],
            name=op.f("fk_load_shedding_events_trigger_snapshot_id_traffic_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_load_shedding_events")),
    )
    op.create_index(
        op.f("ix_load_shedding_events_correlation_id"), "load_shedding_events", ["correlation_id"]
    )
    op.create_index(
        "ix_load_shedding_events_environment_started_at",
        "load_shedding_events",
        ["environment", "started_at"],
    )
    op.create_index(
        "uq_load_shedding_events_active_environment",
        "load_shedding_events",
        ["environment"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_load_shedding_events_active_environment", table_name="load_shedding_events")
    op.drop_index(
        "ix_load_shedding_events_environment_started_at", table_name="load_shedding_events"
    )
    op.drop_index(op.f("ix_load_shedding_events_correlation_id"), table_name="load_shedding_events")
    op.drop_table("load_shedding_events")
    op.drop_index(op.f("ix_scaling_actions_requested_at"), table_name="scaling_actions")
    op.drop_index(op.f("ix_scaling_actions_prediction_id"), table_name="scaling_actions")
    op.drop_index("ix_scaling_actions_correlation_id", table_name="scaling_actions")
    op.drop_table("scaling_actions")
    op.drop_table("surge_prediction_points")
    op.drop_index(op.f("ix_surge_predictions_trigger_snapshot_id"), table_name="surge_predictions")
    op.drop_index(op.f("ix_surge_predictions_scheduled_event_id"), table_name="surge_predictions")
    op.drop_index(op.f("ix_surge_predictions_mode"), table_name="surge_predictions")
    op.drop_index(op.f("ix_surge_predictions_created_at"), table_name="surge_predictions")
    op.drop_table("surge_predictions")
    op.drop_index("ix_scheduled_events_status_starts_at", table_name="scheduled_events")
    op.drop_table("scheduled_events")
    op.drop_index("ix_traffic_snapshots_environment_observed_at", table_name="traffic_snapshots")
    op.drop_table("traffic_snapshots")
