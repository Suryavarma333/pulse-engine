from __future__ import annotations

import os
import random
from itertools import count, cycle

from locust import HttpUser, LoadTestShape, constant, task

from load_tests.lib.models import build_scenario_plan

SCENARIO = os.getenv("PULSE_SCENARIO", "sudden-spike")
if SCENARIO not in {"scheduled-diwali", "sudden-spike"}:
    raise RuntimeError("PULSE_SCENARIO must be scheduled-diwali or sudden-spike")
SEED = int(os.getenv("PULSE_SCENARIO_SEED", "20260818"))
SMOKE = os.getenv("PULSE_SMOKE", "false").lower() == "true"
PLAN = build_scenario_plan(SCENARIO, seed=SEED, smoke=SMOKE)  # type: ignore[arg-type]
USER_SEQUENCE = count()


class CommerceUser(HttpUser):
    wait_time = constant(0.12)

    def on_start(self) -> None:
        offset = random.Random(f"{SEED}:{next(USER_SEQUENCE)}").randrange(len(PLAN.endpoint_cycle))
        rotated = PLAN.endpoint_cycle[offset:] + PLAN.endpoint_cycle[:offset]
        self._requests = cycle(rotated)
        self._sequence = 0

    @task
    def representative_request(self) -> None:
        request = next(self._requests)
        self._sequence += 1
        if request.method == "POST":
            self.client.post(
                request.path,
                name=request.name,
                json={"cart_id": f"load-{SEED}-{self._sequence}", "item_count": 2},
            )
        else:
            self.client.get(request.path, name=request.name)


class PulseLoadShape(LoadTestShape):
    def tick(self) -> tuple[int, float] | None:
        elapsed = self.get_run_time()
        stage = PLAN.stage_at(elapsed)
        if stage is None or elapsed >= PLAN.duration_seconds or stage.users == 0:
            return None
        return stage.users, stage.spawn_rate


__all__ = ["CommerceUser", "PLAN", "PulseLoadShape"]
