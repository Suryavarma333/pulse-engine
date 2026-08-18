"""Add durable response retry state for runtime recovery."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0003"
down_revision: str | None = "20260818_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "response_retries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("retry_key", sa.String(length=180), nullable=False),
        sa.Column("retry_kind", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("demo_run_id", sa.Uuid(), nullable=True),
        sa.Column("prediction_id", sa.Uuid(), nullable=True),
        sa.Column(
            "command_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("desired_shedding_level", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
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
            "retry_kind IN ('dispatch', 'shedding')",
            name="ck_response_retries_valid_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_response_retries_valid_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_response_retries_nonnegative_attempts",
        ),
        sa.CheckConstraint(
            "desired_shedding_level IS NULL OR "
            "(desired_shedding_level >= 0 AND desired_shedding_level <= 3)",
            name="ck_response_retries_shedding_level_range",
        ),
        sa.ForeignKeyConstraint(
            ["demo_run_id"],
            ["demo_runs.id"],
            name="fk_response_retries_demo_run_id_demo_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["surge_predictions.id"],
            name="fk_response_retries_prediction_id_surge_predictions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_response_retries"),
        sa.UniqueConstraint("retry_key", name="uq_response_retries_retry_key"),
    )
    op.create_index(
        "ix_response_retries_due",
        "response_retries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_response_retries_correlation_id",
        "response_retries",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_response_retries_correlation_id", table_name="response_retries")
    op.drop_index("ix_response_retries_due", table_name="response_retries")
    op.drop_table("response_retries")


__all__ = ["downgrade", "upgrade"]
