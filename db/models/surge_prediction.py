from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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


class SurgePrediction(Base):
    __tablename__ = "surge_predictions"
    __table_args__ = (
        CheckConstraint("mode IN ('scheduled', 'realtime')", name="valid_mode"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("baseline_rps >= 0 AND predicted_peak_rps >= 0", name="nonnegative_rates"),
        CheckConstraint("recommended_capacity >= 0", name="nonnegative_recommended_capacity"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    scheduled_event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("scheduled_events.id", ondelete="SET NULL"), index=True
    )
    trigger_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("traffic_snapshots.id", ondelete="SET NULL"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    basis_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    basis_window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predicted_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predicted_peak_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predicted_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline_rps: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_peak_rps: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_multiplier: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    recommended_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    signal_evidence: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_peak_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_peak_rps: Mapped[float | None] = mapped_column(Float)
    detection_lead_seconds: Mapped[int | None] = mapped_column(Integer)
    prediction_error_pct: Mapped[float | None] = mapped_column(Float)
    overprovisioned_instance_minutes: Mapped[float | None] = mapped_column(Float)
    underprovisioned_seconds: Mapped[int | None] = mapped_column(Integer)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SurgePredictionPoint(Base):
    __tablename__ = "surge_prediction_points"

    prediction_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("surge_predictions.id", ondelete="CASCADE"), primary_key=True
    )
    point_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    predicted_rps: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_capacity: Mapped[int | None] = mapped_column(Integer)
