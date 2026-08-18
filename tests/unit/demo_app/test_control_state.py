from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from common.enums import SheddingLevel
from demo_app.app.shedding.state import LoadSheddingController
from demo_app.app.shedding.store import (
    InMemoryLoadSheddingStore,
    SheddingTransition,
    TransitionConflictError,
)


def test_duplicate_concurrent_transitions_are_serialized_to_one_audit() -> None:
    async def exercise():
        store = InMemoryLoadSheddingStore()
        controller = LoadSheddingController(store, "test")
        await controller.load()

        async def transition():
            return await controller.transition(
                level=SheddingLevel.DISABLE_RECOMMENDATIONS,
                reason_code="surge",
                reasoning="Concurrent duplicate",
                signal_evidence={"request_acceleration_rps2": 4.0},
                changed_by="agent",
            )

        return store, await asyncio.gather(transition(), transition())

    store, results = asyncio.run(exercise())

    assert sorted(result.changed for result in results) == [False, True]
    assert len(store.transitions) == 1
    assert store.active["test"].level == 1


class ConflictingStore(InMemoryLoadSheddingStore):
    async def record_transition(self, transition: SheddingTransition) -> None:
        external_transition = replace(
            transition,
            event_id=uuid4(),
            to_level=int(SheddingLevel.CACHED_NONCRITICAL),
            reason_code="external_winner",
            reasoning="Another controller committed first",
        )
        await super().record_transition(external_transition)
        raise TransitionConflictError("simulated competing controller")


def test_conflict_refreshes_the_authoritative_winning_tier() -> None:
    async def exercise():
        controller = LoadSheddingController(ConflictingStore(), "test")
        await controller.load()
        try:
            await controller.transition(
                level=SheddingLevel.DISABLE_RECOMMENDATIONS,
                reason_code="surge",
                reasoning="Losing transition",
                signal_evidence={},
                changed_by="agent",
            )
        except TransitionConflictError:
            return controller.current()
        raise AssertionError("transition conflict was not raised")

    state = asyncio.run(exercise())

    assert state.level is SheddingLevel.CACHED_NONCRITICAL
    assert state.reason_code == "external_winner"
    assert state.policy["checkout"] == "normal"
