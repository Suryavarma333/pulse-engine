from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class BaselineStrategy(Protocol):
    name: str

    def calculate(self, rates: Sequence[float]) -> float: ...


class MovingAverageBaseline:
    name = "moving_average"

    def calculate(self, rates: Sequence[float]) -> float:
        if not rates:
            raise ValueError("at least one rate is required")
        return sum(rates) / len(rates)


class ExponentialBaseline:
    name = "exponential"

    def __init__(self, alpha: float = 0.35) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha

    def calculate(self, rates: Sequence[float]) -> float:
        if not rates:
            raise ValueError("at least one rate is required")
        smoothed = float(rates[0])
        for rate in rates[1:]:
            smoothed = self.alpha * float(rate) + (1 - self.alpha) * smoothed
        return smoothed


__all__ = ["BaselineStrategy", "ExponentialBaseline", "MovingAverageBaseline"]
