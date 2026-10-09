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
        self.last_error: float | None = None
        self.last_p: float | None = None
        self.last_i: float | None = None
        self.last_d: float | None = None

    def reset(self) -> None:
        """Reset integrator and derivative state."""
        self._integral = 0.0
        self._prev_error = 0.0
        self.last_error = None
        self.last_p = None
        self.last_i = None
        self.last_d = None

    def preload(self, output: float, measurement: float) -> None:
        """Seed the integrator so the next call outputs ~`output` (bumpless start).

        Used after a restart to continue from the last written value instead of
        restarting from a zero integral.
        """
        if self.ki == 0:
            return
        lo, hi = self.output_limits
        output = max(lo, min(hi, output))
        error = self.setpoint - measurement
        self._integral = (output - self.kp * error) / self.ki
        self._prev_error = error

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

        p = self.kp * error
        i = self.ki * self._integral
        d = self.kd * derivative
        output = p + i + d
        clamped = max(lo, min(hi, output))

        self.last_error = error
        self.last_p = p
        self.last_i = i
        self.last_d = d

        # Back-calculation anti-windup: remove the excess that caused saturation
        if self.ki != 0:
            self._integral -= (output - clamped) / self.ki

        return clamped
