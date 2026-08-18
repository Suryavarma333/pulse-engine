from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    cart_id: str = Field(default="demo-cart", min_length=1, max_length=100)
    item_count: int = Field(default=1, ge=1, le=100)


class SheddingTransitionRequest(BaseModel):
    level: int = Field(ge=0, le=3)
    reason_code: str = Field(min_length=1, max_length=60)
    reasoning: str = Field(min_length=1, max_length=1000)
    changed_by: str = Field(default="agent", pattern="^(agent|operator|system)$")
    signal_evidence: dict[str, Any] = Field(default_factory=dict)
    correlation_id: UUID | None = None
    prediction_id: UUID | None = None
    trigger_snapshot_id: int | None = None


class SheddingStateResponse(BaseModel):
    changed: bool | None = None
    event_id: UUID | None
    level: int
    level_name: str
    started_at: datetime
    reason_code: str
    reasoning: str
    endpoint_policies: dict[str, str]
