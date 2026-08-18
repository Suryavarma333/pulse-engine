from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Float, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSON_TYPE


class DemoRun(Base):
    __tablename__ = "demo_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('scheduled', 'realtime')", name="valid_mode"),
        CheckConstraint("execution_mode IN ('dry_run', 'live')", name="valid_execution_mode"),
        CheckConstraint(
            "status IN ('running', 'pending_evaluation', 'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="valid_interval"),
        CheckConstraint(
            "checkout_p99_latency_ms IS NULL OR checkout_p99_latency_ms >= 0",
            name="nonnegative_checkout_p99",
        ),
        CheckConstraint(
            "checkout_success_rate IS NULL OR "
            "(checkout_success_rate >= 0 AND checkout_success_rate <= 1)",
            name="checkout_success_rate_range",
        ),
        CheckConstraint(
            "provisioning_efficiency_pct IS NULL OR "
            "(provisioning_efficiency_pct >= 0 AND provisioning_efficiency_pct <= 100)",
            name="provisioning_efficiency_range",
        ),
        CheckConstraint(
            "prediction_error_pct IS NULL OR prediction_error_pct >= 0",
            name="nonnegative_prediction_error",
        ),
        CheckConstraint(
            "overprovisioned_instance_minutes IS NULL OR overprovisioned_instance_minutes >= 0",
            name="nonnegative_overprovisioned_minutes",
        ),
        CheckConstraint(
            "underprovisioned_seconds IS NULL OR underprovisioned_seconds >= 0",
            name="nonnegative_underprovisioned_seconds",
        ),
        CheckConstraint(
            "error_rate IS NULL OR (error_rate >= 0 AND error_rate <= 1)",
            name="error_rate_range",
        ),
        CheckConstraint(
            "recovery_duration_seconds IS NULL OR recovery_duration_seconds >= 0",
            name="nonnegative_recovery_duration",
        ),
        CheckConstraint(
            "cost_duration_seconds IS NULL OR cost_duration_seconds >= 0",
            name="nonnegative_cost_duration",
        ),
        Index("ix_demo_runs_environment_started_at", "environment", "started_at"),
        Index("ix_demo_runs_scenario_status", "scenario_name", "status"),
        Index(
            "uq_demo_runs_active_environment",
            "environment",
            unique=True,
            postgresql_where="status IN ('running', 'pending_evaluation')",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    scenario_name: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    baseline_type: Mapped[str] = mapped_column(String(40), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(12), nullable=False)
    configuration: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    thresholds: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    reactive_comparator_crossed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    checkout_p99_latency_ms: Mapped[float | None] = mapped_column(Float)
    checkout_success_rate: Mapped[float | None] = mapped_column(Float)
    detection_lead_seconds: Mapped[float | None] = mapped_column(Float)
    provisioning_efficiency_pct: Mapped[float | None] = mapped_column(Float)
    prediction_error_pct: Mapped[float | None] = mapped_column(Float)
    overprovisioned_instance_minutes: Mapped[float | None] = mapped_column(Float)
    underprovisioned_seconds: Mapped[float | None] = mapped_column(Float)
    error_rate: Mapped[float | None] = mapped_column(Float)
    recovery_duration_seconds: Mapped[float | None] = mapped_column(Float)
    cost_duration_seconds: Mapped[float | None] = mapped_column(Float)
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False, default="v1")
    locust_summary: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    result_summary: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    warnings: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
