"""Simple PID controller with integral anti-windup.

No Home Assistant imports — pure Python, independently testable.
"""

from __future__ import annotations


class SimplePID:
    """Discrete PID controller with back-calculation anti-windup."""

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        setpoint: float,
        output_limits: tuple[float, float],
    ) -> None:
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint
        self.output_limits = output_limits
        self._integral = 0.0
        self._prev_error = 0.0

    def reset(self) -> None:
        """Reset integrator and derivative state."""
        self._integral = 0.0
        self._prev_error = 0.0

    def __call__(self, measurement: float, dt: float) -> float:
        """Compute PID output for the current measurement.

        Args:
            measurement: Current process value.
            dt: Time elapsed since last call (seconds).

        Returns:
            Control output, clamped to output_limits.
        """
        lo, hi = self.output_limits
        dt_safe = max(dt, 1e-6)

        error = self.setpoint - measurement
        self._integral += error * dt_safe
        derivative = (error - self._prev_error) / dt_safe
        self._prev_error = error

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        clamped = max(lo, min(hi, output))

        # Back-calculation anti-windup: remove the excess that caused saturation
        if self.ki != 0:
            self._integral -= (output - clamped) / self.ki

        return clamped
