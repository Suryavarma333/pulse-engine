from load_tests.lib.models import ScenarioPlan, build_scenario_plan


def sudden_spike(*, seed: int = 20260818, smoke: bool = False) -> ScenarioPlan:
    return build_scenario_plan("sudden-spike", seed=seed, smoke=smoke)


__all__ = ["sudden_spike"]
