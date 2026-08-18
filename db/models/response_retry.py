from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSON_TYPE


class ResponseRetry(Base):
    """Durable response/tier retry state kept separate from capacity claims."""

    __tablename__ = "response_retries"
    __table_args__ = (
        CheckConstraint("retry_kind IN ('dispatch', 'shedding')", name="valid_kind"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')", name="valid_status"
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint(
            "desired_shedding_level IS NULL OR "
            "(desired_shedding_level >= 0 AND desired_shedding_level <= 3)",
            name="shedding_level_range",
        ),
        Index("ix_response_retries_due", "status", "next_attempt_at"),
        Index("ix_response_retries_correlation_id", "correlation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    retry_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    retry_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    demo_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("demo_runs.id", ondelete="SET NULL")
    )
    prediction_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("surge_predictions.id", ondelete="SET NULL")
    )
    command_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False)
    desired_shedding_level: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["ResponseRetry"]
