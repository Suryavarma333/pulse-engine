from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSON_TYPE


class ScalingAction(Base):
    __tablename__ = "scaling_actions"
    __table_args__ = (
        CheckConstraint("previous_desired_capacity >= 0", name="nonnegative_previous_capacity"),
        CheckConstraint("requested_desired_capacity >= 0", name="nonnegative_requested_capacity"),
        CheckConstraint("max_instance_ceiling >= 0", name="nonnegative_instance_ceiling"),
        CheckConstraint(
            "applied_desired_capacity IS NULL OR applied_desired_capacity <= max_instance_ceiling",
            name="applied_within_ceiling",
        ),
        CheckConstraint(
            "applied_desired_capacity IS NULL OR applied_desired_capacity >= 0",
            name="nonnegative_applied_capacity",
        ),
        Index("ix_scaling_actions_correlation_id", "correlation_id"),
        Index("ix_scaling_actions_demo_run_requested_at", "demo_run_id", "requested_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    demo_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("demo_runs.id", ondelete="SET NULL")
    )
    prediction_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("surge_predictions.id", ondelete="SET NULL"), index=True
    )
    trigger_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("traffic_snapshots.id", ondelete="SET NULL")
    )
    scheduled_event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("scheduled_events.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    action_type: Mapped[str] = mapped_column(String(24), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(12), nullable=False)
    target_resource: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_desired_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_desired_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_desired_capacity: Mapped[int | None] = mapped_column(Integer)
    max_instance_ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    signal_evidence: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    provider_request_id: Mapped[str | None] = mapped_column(String(160))
    error_message: Mapped[str | None] = mapped_column(Text)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
