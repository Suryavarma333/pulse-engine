"""AWS integrations isolated behind safety-bounded adapters."""

from agent.app.aws.autoscaling import (
    AutoScalingCapacityAdapter,
    CapacityObservation,
)

__all__ = ["AutoScalingCapacityAdapter", "CapacityObservation"]
