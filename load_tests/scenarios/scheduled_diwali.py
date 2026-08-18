from load_tests.lib.models import ScenarioPlan, build_scenario_plan


def scheduled_diwali(*, seed: int = 20260818, smoke: bool = False) -> ScenarioPlan:
    return build_scenario_plan("scheduled-diwali", seed=seed, smoke=smoke)


__all__ = ["scheduled_diwali"]
