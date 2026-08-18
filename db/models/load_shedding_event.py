from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSON_TYPE


class LoadSheddingEvent(Base):
    __tablename__ = "load_shedding_events"
    __table_args__ = (
        CheckConstraint(
            "from_level IS NULL OR (from_level >= 0 AND from_level <= 3)", name="from_level_range"
        ),
        CheckConstraint("to_level >= 0 AND to_level <= 3", name="to_level_range"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="valid_interval"),
        Index("ix_load_shedding_events_environment_started_at", "environment", "started_at"),
        Index("ix_load_shedding_events_demo_run_started_at", "demo_run_id", "started_at"),
        Index(
            "uq_load_shedding_events_active_environment",
            "environment",
            unique=True,
            postgresql_where="ended_at IS NULL",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    demo_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("demo_runs.id", ondelete="SET NULL")
    )
    prediction_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("surge_predictions.id", ondelete="SET NULL")
    )
    trigger_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("traffic_snapshots.id", ondelete="SET NULL")
    )
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    from_level: Mapped[int | None] = mapped_column(SmallInteger)
    to_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    changed_by: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    signal_evidence: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    policy_snapshot: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    application_status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
