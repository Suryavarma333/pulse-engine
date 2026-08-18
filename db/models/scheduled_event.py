from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSON_TYPE


class ScheduledEvent(Base):
    __tablename__ = "scheduled_events"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="valid_time_range"),
        CheckConstraint("expected_multiplier >= 1", name="valid_multiplier"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("peak_lead_seconds <= prewarm_lead_seconds", name="lead_time_order"),
        CheckConstraint("minimum_desired_capacity >= 0", name="nonnegative_minimum_capacity"),
        CheckConstraint(
            "peak_desired_capacity >= minimum_desired_capacity", name="valid_peak_capacity"
        ),
        Index("ix_scheduled_events_status_starts_at", "status", "starts_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    expected_multiplier: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    baseline_rps: Mapped[float | None] = mapped_column(Float)
    expected_peak_rps: Mapped[float | None] = mapped_column(Float)
    prewarm_lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    peak_lead_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    scale_down_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    minimum_desired_capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    peak_desired_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    max_instances_override: Mapped[int | None] = mapped_column(Integer)
    ramp_profile: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0.5)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
