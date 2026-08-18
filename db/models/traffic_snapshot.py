from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSON_TYPE


class TrafficSnapshot(Base):
    __tablename__ = "traffic_snapshots"
    __table_args__ = (
        UniqueConstraint("environment", "observed_at", name="observation"),
        CheckConstraint("window_seconds > 0", name="positive_window"),
        CheckConstraint("request_count >= 0", name="nonnegative_request_count"),
        CheckConstraint("concurrent_requests >= 0", name="nonnegative_concurrent_requests"),
        CheckConstraint("origin_request_rate_rps >= 0", name="nonnegative_origin_rate"),
        CheckConstraint("baseline_request_rate_rps >= 0", name="nonnegative_baseline_rate"),
        CheckConstraint(
            "error_rate IS NULL OR (error_rate >= 0 AND error_rate <= 1)", name="error_rate_range"
        ),
        CheckConstraint(
            "load_shedding_level >= 0 AND load_shedding_level <= 3", name="shedding_level_range"
        ),
        CheckConstraint(
            "checkout_success_rate IS NULL OR "
            "(checkout_success_rate >= 0 AND checkout_success_rate <= 1)",
            name="checkout_success_rate_range",
        ),
        Index("ix_traffic_snapshots_environment_observed_at", "environment", "observed_at"),
        Index("ix_traffic_snapshots_demo_run_observed_at", "demo_run_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    demo_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("demo_runs.id", ondelete="SET NULL")
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_seconds: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    origin_request_rate_rps: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_request_rate_rps: Mapped[float] = mapped_column(Float, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    concurrent_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_rate_change_rps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    request_acceleration_rps2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    edge_request_rate_rps: Mapped[float | None] = mapped_column(Float)
    queue_depth: Mapped[int | None] = mapped_column(BigInteger)
    queue_growth_per_second: Mapped[float | None] = mapped_column(Float)
    concurrent_sessions: Mapped[int | None] = mapped_column(Integer)
    login_rate_rps: Mapped[float | None] = mapped_column(Float)
    cpu_utilization_pct: Mapped[float | None] = mapped_column(Float)
    p50_latency_ms: Mapped[float | None] = mapped_column(Float)
    p95_latency_ms: Mapped[float | None] = mapped_column(Float)
    p99_latency_ms: Mapped[float | None] = mapped_column(Float)
    checkout_p99_latency_ms: Mapped[float | None] = mapped_column(Float)
    checkout_success_rate: Mapped[float | None] = mapped_column(Float)
    error_rate: Mapped[float | None] = mapped_column(Float)
    asg_desired_capacity: Mapped[int | None] = mapped_column(Integer)
    asg_in_service_capacity: Mapped[int | None] = mapped_column(Integer)
    load_shedding_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    reactive_comparator_crossed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    signal_details: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
