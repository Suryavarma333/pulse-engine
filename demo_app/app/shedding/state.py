from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from common.enums import SheddingLevel
from demo_app.app.shedding.policies import policy_snapshot
from demo_app.app.shedding.store import LoadSheddingStore, SheddingTransition

logger = logging.getLogger("pulse.demo_app.shedding")


@dataclass(frozen=True, slots=True)
class SheddingState:
    event_id: UUID | None
    level: SheddingLevel
    started_at: datetime
    reason_code: str
    reasoning: str
    policy: dict[str, str]


@dataclass(frozen=True, slots=True)
class TransitionResult:
    changed: bool
    state: SheddingState


class LoadSheddingController:
    def __init__(self, store: LoadSheddingStore, environment: str) -> None:
        self._store = store
        self._environment = environment
        self._lock = asyncio.Lock()
        self._state = self._normal_state()

    @staticmethod
    def _normal_state() -> SheddingState:
        return SheddingState(
            event_id=None,
            level=SheddingLevel.NORMAL,
            started_at=datetime.now(UTC),
            reason_code="application_start",
            reasoning="Application started in normal mode",
            policy=policy_snapshot(SheddingLevel.NORMAL),
        )

    async def load(self) -> None:
        stored = await self._store.load_active(self._environment)
        if stored is None:
            return
        self._state = SheddingState(
            event_id=stored.event_id,
            level=SheddingLevel(stored.level),
            started_at=stored.started_at,
            reason_code=stored.reason_code,
            reasoning=stored.reasoning,
            policy=stored.policy,
        )

    def current(self) -> SheddingState:
        return self._state

    async def transition(
        self,
        *,
        level: SheddingLevel,
        reason_code: str,
        reasoning: str,
        signal_evidence: dict,
        changed_by: str,
        correlation_id: UUID | None = None,
        prediction_id: UUID | None = None,
        trigger_snapshot_id: int | None = None,
    ) -> TransitionResult:
        async with self._lock:
            if level == self._state.level:
                return TransitionResult(changed=False, state=self._state)

            started_at = datetime.now(UTC)
            event_id = uuid4()
            transition = SheddingTransition(
                event_id=event_id,
                correlation_id=correlation_id or uuid4(),
                environment=self._environment,
                started_at=started_at,
                from_level=int(self._state.level),
                to_level=int(level),
                changed_by=changed_by,
                reason_code=reason_code,
                reasoning=reasoning,
                signal_evidence=signal_evidence,
                policy=policy_snapshot(level),
                prediction_id=prediction_id,
                trigger_snapshot_id=trigger_snapshot_id,
            )
            # Persist first so an unaudited policy cannot silently become active.
            await self._store.record_transition(transition)
            self._state = SheddingState(
                event_id=event_id,
                level=level,
                started_at=started_at,
                reason_code=reason_code,
                reasoning=reasoning,
                policy=transition.policy,
            )
            logger.warning(
                "load_shedding_transition %s",
                json.dumps(
                    {
                        **asdict(transition),
                        "event_id": str(event_id),
                        "correlation_id": str(transition.correlation_id),
                        "prediction_id": str(prediction_id) if prediction_id else None,
                        "started_at": started_at.isoformat(),
                    },
                    default=str,
                    sort_keys=True,
                ),
            )
            return TransitionResult(changed=True, state=self._state)
