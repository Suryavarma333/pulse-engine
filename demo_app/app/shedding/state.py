from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from common.enums import SheddingLevel
from demo_app.app.shedding.policies import policy_snapshot
from demo_app.app.shedding.store import (
    AuditPersistenceUnavailableError,
    LoadSheddingStore,
    SheddingTransition,
    StoredSheddingState,
    TransitionConflictError,
)

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


@dataclass(frozen=True, slots=True)
class PersistenceStatus:
    backend: str
    durable: bool
    available: bool
    state_loaded: bool


class LoadSheddingController:
    def __init__(self, store: LoadSheddingStore, environment: str) -> None:
        self._store = store
        self._environment = environment
        self._lock = asyncio.Lock()
        self._state = self._normal_state()
        self._persistence_available = False
        self._state_loaded = False

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
        try:
            stored = await self._store.load_active(self._environment)
        except Exception:
            self._persistence_available = False
            self._state_loaded = False
            logger.error(
                "load_shedding_authoritative_state_unavailable backend=%s",
                self._store.backend,
            )
            return
        self._persistence_available = True
        self._state_loaded = True
        if stored is not None:
            self._apply_stored_state(stored)

    def _apply_stored_state(self, stored: StoredSheddingState) -> None:
        level = SheddingLevel(stored.level)
        self._state = SheddingState(
            event_id=stored.event_id,
            level=level,
            started_at=stored.started_at,
            reason_code=stored.reason_code,
            reasoning=stored.reasoning,
            policy=policy_snapshot(level),
        )

    def current(self) -> SheddingState:
        return self._state

    def persistence_status(self) -> PersistenceStatus:
        return PersistenceStatus(
            backend=self._store.backend,
            durable=self._store.durable,
            available=self._persistence_available,
            state_loaded=self._state_loaded,
        )

    async def _refresh_authoritative(self) -> None:
        try:
            stored = await self._store.load_active(self._environment)
        except Exception as exc:
            self._persistence_available = False
            raise AuditPersistenceUnavailableError(
                "load-shedding audit persistence is unavailable"
            ) from exc

        self._persistence_available = True
        self._state_loaded = True
        if stored is not None:
            self._apply_stored_state(stored)
        elif self._state.level is not SheddingLevel.NORMAL:
            raise TransitionConflictError("authoritative tier state is missing")

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
        demo_run_id: UUID | None = None,
    ) -> TransitionResult:
        async with self._lock:
            await self._refresh_authoritative()
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
                demo_run_id=demo_run_id,
            )
            # Persist first so an unaudited policy cannot silently become active.
            try:
                await self._store.record_transition(transition)
            except TransitionConflictError:
                await self._refresh_authoritative()
                raise
            except Exception as exc:
                self._persistence_available = False
                raise AuditPersistenceUnavailableError(
                    "load-shedding audit persistence is unavailable"
                ) from exc

            self._persistence_available = True
            self._state_loaded = True
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
                        "demo_run_id": str(demo_run_id) if demo_run_id else None,
                        "started_at": started_at.isoformat(),
                    },
                    default=str,
                    sort_keys=True,
                ),
            )
            return TransitionResult(changed=True, state=self._state)
