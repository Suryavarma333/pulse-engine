"""Add authoritative capacity evidence and durable tier intent."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0004"
down_revision: str | None = "20260818_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "traffic_snapshots",
        sa.Column("capacity_per_instance_rps", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "ck_traffic_snapshots_positive_capacity_per_instance",
        "traffic_snapshots",
        "capacity_per_instance_rps IS NULL OR capacity_per_instance_rps > 0",
    )

    op.add_column(
        "scaling_actions",
        sa.Column(
            "response_command",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "scaling_actions",
        sa.Column(
            "shedding_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "scaling_actions",
        sa.Column("requested_shedding_level", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "scaling_actions",
        sa.Column(
            "shedding_status",
            sa.String(length=16),
            server_default="not_requested",
            nullable=False,
        ),
    )
    op.add_column(
        "scaling_actions",
        sa.Column("shedding_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_scaling_actions_requested_shedding_level_range",
        "scaling_actions",
        "requested_shedding_level IS NULL OR "
        "(requested_shedding_level >= 0 AND requested_shedding_level <= 3)",
    )
    op.create_check_constraint(
        "ck_scaling_actions_valid_shedding_status",
        "scaling_actions",
        "shedding_status IN ('not_requested', 'pending', 'succeeded')",
    )
    op.create_check_constraint(
        "ck_scaling_actions_nonnegative_shedding_attempts",
        "scaling_actions",
        "shedding_attempts >= 0",
    )
    op.create_index(
        "ix_scaling_actions_pending_shedding",
        "scaling_actions",
        ["shedding_status", "requested_at"],
    )
    op.alter_column("scaling_actions", "response_command", server_default=None)
    op.alter_column("scaling_actions", "shedding_status", server_default=None)
    op.alter_column("scaling_actions", "shedding_attempts", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_scaling_actions_pending_shedding", table_name="scaling_actions")
    op.drop_constraint(
        "ck_scaling_actions_nonnegative_shedding_attempts",
        "scaling_actions",
        type_="check",
    )
    op.drop_constraint(
        "ck_scaling_actions_valid_shedding_status",
        "scaling_actions",
        type_="check",
    )
    op.drop_constraint(
        "ck_scaling_actions_requested_shedding_level_range",
        "scaling_actions",
        type_="check",
    )
    op.drop_column("scaling_actions", "shedding_error")
    op.drop_column("scaling_actions", "shedding_attempts")
    op.drop_column("scaling_actions", "shedding_status")
    op.drop_column("scaling_actions", "requested_shedding_level")
    op.drop_column("scaling_actions", "response_command")

    op.drop_constraint(
        "ck_traffic_snapshots_positive_capacity_per_instance",
        "traffic_snapshots",
        type_="check",
    )
    op.drop_column("traffic_snapshots", "capacity_per_instance_rps")


__all__ = ["downgrade", "upgrade"]
