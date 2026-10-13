"""Per-room RC model for shadow mode: MPC, prediction scoring and anomaly check.

No Home Assistant imports — pure Python, independently testable. Timestamps are
epoch seconds (float).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

try:
    from .rc_mpc import compute_supply_setpoint, model_inputs
except ImportError:  # loaded as a top-level module by tools/
    from rc_mpc import compute_supply_setpoint, model_inputs

# Fixed scoring horizon (h), independent of the control horizon, so scores stay
# comparable with tools/fit_rc_multistep.py
SCORE_HORIZON_H = 3
# A due prediction is scored against the first reading at most this late (s)
SCORE_TOLERANCE_S = 600
# Rolling window for the prediction error (s)
ERROR_WINDOW_S = 24 * 3600
# Anomaly: measured slope this much below the zero-heat model slope (°C/h) ...
ANOMALY_RATE = 0.3
# ... held for at least this long (s)
ANOMALY_HOLD_S = 15 * 60
# Measured slope window (s); needs at least 2/3 of it covered
SLOPE_WINDOW_S = 30 * 60


@dataclass
class RoomParams:
    R: float
    C: float
    UA: float
    g: float


@dataclass
class RoomResult:
    temperature: float
    target: float
    importance_ramped: float
    supply_setpoint: float
    predicted_temp: float
    error_rms: float | None
    error_bias: float | None
    error_samples: int
    anomaly: bool


class RoomModel:
    """One room's RC model plus its shadow-mode state."""

    def __init__(self, params: RoomParams) -> None:
        self.params = params
        self.importance_ramped: float | None = None
        self._ramp_ts: float | None = None
        self._pending: deque[tuple[float, float]] = deque()  # (due_ts, predicted)
        self._errors: deque[tuple[float, float]] = deque()  # (ts, actual − predicted)
        self._history: deque[tuple[float, float]] = deque()  # (ts, temp) for the slope
        self._anomaly_since: float | None = None

    # --- Importance ramp ---

    def advance_ramp(self, now: float, commanded: float | None, ramp_minutes: float) -> float:
        """Slew importance_ramped toward commanded; ramp_minutes is a full 0→1 change."""
        if commanded is None:
            # Commanded value not known yet (entity not restored): hold
            self._ramp_ts = now
            return self.importance_ramped if self.importance_ramped is not None else 0.0
        if self.importance_ramped is None or self._ramp_ts is None or ramp_minutes <= 0:
            self.importance_ramped = commanded
        else:
            max_step = max(0.0, now - self._ramp_ts) / 60.0 / ramp_minutes
            delta = commanded - self.importance_ramped
            self.importance_ramped += max(-max_step, min(max_step, delta))
        self._ramp_ts = now
        return self.importance_ramped

    # --- Model ---

    def predict(
        self,
        t_room: float,
        outdoor: list[float],
        solar: list[float],
        supply: list[float],
        dt_hours: float = 1.0,
    ) -> float:
        """Simulate the room forward with a per-step supply; return the final temperature."""
        p = self.params
        a = (p.UA + 1.0 / p.R) / p.C * dt_hours
        b = p.UA / p.C * dt_hours
        t = t_room
        for k in range(len(outdoor)):
            d = (p.g * solar[k] + outdoor[k] / p.R) / p.C * dt_hours
            t = (1.0 - a) * t + b * supply[k] + d
        return t

    def zero_heat_slope(self, t_room: float, t_out: float, solar: float) -> float:
        """Model slope (°C/h) with no radiator heat."""
        p = self.params
        return (-(t_room - t_out) / p.R + p.g * solar) / p.C

    # --- Scoring ---

    def push_prediction(self, due: float, predicted: float) -> None:
        self._pending.append((due, predicted))

    def score_due(self, now: float, actual: float, paused: bool) -> None:
        """Score predictions that have come due; drop stale ones and old errors."""
        while self._pending and self._pending[0][0] <= now:
            due, predicted = self._pending.popleft()
            if not paused and now - due <= SCORE_TOLERANCE_S:
                self._errors.append((now, actual - predicted))
        while self._errors and now - self._errors[0][0] > ERROR_WINDOW_S:
            self._errors.popleft()

    def error_stats(self) -> tuple[float | None, float | None, int]:
        """(rms, bias, samples) of the rolling 3 h-ahead error (actual − predicted)."""
        n = len(self._errors)
        if n == 0:
            return None, None, 0
        errs = [e for _, e in self._errors]
        return math.sqrt(sum(e * e for e in errs) / n), sum(errs) / n, n

    # --- Anomaly ---

    def check_anomaly(self, now: float, t_room: float, t_out: float, solar: float) -> bool:
        """True when the room cools clearly faster than with no heat at all (e.g. open window)."""
        self._history.append((now, t_room))
        # Keep one sample at or beyond the window edge so the slope spans the full window
        while len(self._history) > 1 and now - self._history[1][0] >= SLOPE_WINDOW_S:
            self._history.popleft()
        oldest_ts, oldest_temp = self._history[0]
        span_h = (now - oldest_ts) / 3600.0
        if span_h < SLOPE_WINDOW_S / 3600.0 * 0.66:
            self._anomaly_since = None
            return False
        slope = (t_room - oldest_temp) / span_h
        if slope < self.zero_heat_slope(t_room, t_out, solar) - ANOMALY_RATE:
            if self._anomaly_since is None:
                self._anomaly_since = now
            return now - self._anomaly_since >= ANOMALY_HOLD_S
        self._anomaly_since = None
        return False

    # --- One outer tick ---

    def step(
        self,
        now: float,
        t_room: float,
        target: float,
        commanded_importance: float | None,
        ramp_minutes: float,
        outdoor_forecast: list[float],
        solar_forecast: list[float],
        t_out: float,
        live_supply: float,
        pump_off: bool,
        horizon_h: float,
        supply_min: float,
        supply_max: float,
        scoring_paused: bool,
    ) -> RoomResult:
        importance = self.advance_ramp(now, commanded_importance, ramp_minutes)
        p = self.params
        # With the pump off the radiators sit at room temperature and deliver no heat
        prediction_supply = t_room if pump_off else live_supply

        # Room MPC on the control horizon, plus the prediction under the live supply
        outdoor, solar, dt_hours = model_inputs(horizon_h, outdoor_forecast, solar_forecast, t_out)
        supply_setpoint, predicted = compute_supply_setpoint(
            t_in=t_room,
            t_setpoint=target,
            outdoor_temps=outdoor,
            solar_irradiance=solar,
            R=p.R,
            C=p.C,
            UA=p.UA,
            g=p.g,
            supply_min=supply_min,
            supply_max=supply_max,
            prediction_supply=prediction_supply,
            dt_hours=dt_hours,
        )

        # Score what came due before adding this tick's prediction
        self.score_due(now, t_room, scoring_paused)
        s_out, s_sol, _ = model_inputs(SCORE_HORIZON_H, outdoor_forecast, solar_forecast, t_out)
        self.push_prediction(
            now + SCORE_HORIZON_H * 3600,
            self.predict(t_room, s_out, s_sol, [prediction_supply] * SCORE_HORIZON_H),
        )
        rms, bias, n = self.error_stats()

        solar_now = solar_forecast[0] if solar_forecast else 0.0
        anomaly = self.check_anomaly(now, t_room, t_out, solar_now)

        return RoomResult(
            temperature=t_room,
            target=target,
            importance_ramped=importance,
            supply_setpoint=supply_setpoint,
            predicted_temp=predicted,
            error_rms=rms,
            error_bias=bias,
            error_samples=n,
            anomaly=anomaly,
        )

    # --- Persistence ---

    def to_dict(self) -> dict:
        return {
            "importance_ramped": self.importance_ramped,
            "ramp_ts": self._ramp_ts,
            "pending": [list(x) for x in self._pending],
            "errors": [list(x) for x in self._errors],
        }

    def load_dict(self, data: dict) -> None:
        self.importance_ramped = data.get("importance_ramped")
        self._ramp_ts = data.get("ramp_ts")
        self._pending = deque((float(a), float(b)) for a, b in data.get("pending", []))
        self._errors = deque((float(a), float(b)) for a, b in data.get("errors", []))


def combine(
    items: dict[str, tuple[float, float]],
) -> tuple[float | None, float | None]:
    """Combine room setpoints {name: (supply_setpoint, weight)}.

    Returns (weighted mean, max over rooms with weight > 0); both None when the
    weights sum to 0.
    """
    total = sum(max(0.0, w) for _, w in items.values())
    if total <= 0:
        return None, None
    mean = sum(s * max(0.0, w) for s, w in items.values()) / total
    return mean, max(s for s, w in items.values() if w > 0)
