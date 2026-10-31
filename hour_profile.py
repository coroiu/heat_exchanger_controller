"""Hour-of-day disturbance profile for the RC model.

No Home Assistant imports — pure Python, independently testable.

The single-mass model misses heat that follows the household's daily schedule
(occupied bedroom at night, afternoon activity, ...) and, on the house path,
the time-of-day blending of the steering sensor. This learns one correction per
clock hour, q[h] in °C/h, from the model's own one-hour residual:

    r = (T_in(end) − T_model(end)) / elapsed_h      model started from T_in(start)
    q[h] += β · (r − q[h])                           once per completed hour

and the MPC adds q[hour] · dt to each step's disturbance. β 0.2 ≈ 5 days of
memory: the 14-day backtest (2026-10-16) gave house 3 h-ahead RMS 0.285 → 0.230
and night bias +0.10 → +0.01; β 0.1 learned too slowly, 0.35 overreacted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PROFILE_BETA = 0.2
# One-off events (window, party) shouldn't drag a slot far; normal residuals stay within ±0.25
RESIDUAL_CLIP = 0.4  # °C/h
# An hour only counts if it was observed nearly whole (restarts, sensor gaps)
MIN_SAMPLES = 8
MIN_ELAPSED_H = 50 / 60
MAX_ELAPSED_H = 75 / 60
# Default no-pre-heat window (local hours, start inclusive, end exclusive), configurable via
# profile_no_preheat_start / _end. Here the profile may only take heat away (q ≥ 0 applied,
# q < 0 ignored), so occupied rooms aren't pre-heated while people sleep; extend it over any
# hours where the steering sensor blends rooms, since those slots learn the blend, not
# physics. Without it, negative early-morning slots pushed supply to ~40 and overheated
# the bedroom.
DEFAULT_NO_PREHEAT_START = 22
DEFAULT_NO_PREHEAT_END = 7


def reduce_only_hours(start: int, end: int) -> frozenset[int]:
    """Local hours from start (inclusive) to end (exclusive), wrapping midnight; empty if equal."""
    start, end = int(start) % 24, int(end) % 24
    return frozenset((start + i) % 24 for i in range((end - start) % 24))


REDUCE_ONLY_HOURS = reduce_only_hours(DEFAULT_NO_PREHEAT_START, DEFAULT_NO_PREHEAT_END)


@dataclass
class _Bucket:
    hour: int
    start: datetime
    start_temp: float
    supply: float = 0.0
    outdoor: float = 0.0
    solar: float = 0.0
    n: int = 0
    valid: bool = True


@dataclass
class HourProfile:
    q: list[float] = field(default_factory=lambda: [0.0] * 24)
    beta: float = PROFILE_BETA
    updates: int = 0
    last_residual: float | None = None
    last_hour: int | None = None
    _bucket: _Bucket | None = None

    def observe(
        self,
        now_local: datetime,
        t_in: float | None,
        supply: float | None,
        outdoor: float | None,
        solar: float | None,
        rc: tuple[float, float, float, float],
        learn: bool,
    ) -> bool:
        """Feed one outer tick. Returns True when an hour completed and q was updated.

        now_local must be local wall-clock time (the schedule follows it).
        supply is the effective supply: measured when the pump runs, t_in when it's off.
        rc is (UA, R, g, C). learn=False (window, scoring paused) discards the current hour.
        """
        b = self._bucket
        updated = False
        if b is not None and now_local.hour != b.hour:
            if t_in is not None:
                updated = self._finish(b, now_local, t_in, rc)
            b = self._bucket = None
        if t_in is None:
            self._bucket = None
            return updated
        if b is None:
            b = self._bucket = _Bucket(hour=now_local.hour, start=now_local, start_temp=t_in)
        if not learn:
            b.valid = False
        if supply is None or outdoor is None:
            return updated
        b.supply += supply
        b.outdoor += outdoor
        b.solar += solar or 0.0
        b.n += 1
        return updated

    def _finish(self, b: _Bucket, now_local: datetime, t_end: float, rc: tuple[float, float, float, float]) -> bool:
        # Real elapsed time: aware datetimes in the same zone subtract as wall clock, so the
        # repeated hour when DST ends would count as one hour (it's two, and gets dropped)
        elapsed_h = (now_local.timestamp() - b.start.timestamp()) / 3600.0
        if not b.valid or b.n < MIN_SAMPLES or not MIN_ELAPSED_H <= elapsed_h <= MAX_ELAPSED_H:
            return False
        ua, r_env, g, c = rc
        supply, outdoor, solar = b.supply / b.n, b.outdoor / b.n, b.solar / b.n
        t0 = b.start_temp
        rate = (ua * (supply - t0) + (outdoor - t0) / r_env + g * solar) / c
        residual = (t_end - (t0 + rate * elapsed_h)) / elapsed_h
        residual = max(-RESIDUAL_CLIP, min(RESIDUAL_CLIP, residual))
        self.q[b.hour] += self.beta * (residual - self.q[b.hour])
        self.updates += 1
        self.last_residual = residual
        self.last_hour = b.hour
        return True

    def corrections(
        self, start_local: datetime, n_steps: int, dt_hours: float,
        reduce_only: frozenset[int] = REDUCE_ONLY_HOURS,
    ) -> list[float]:
        """Per-step extra temperature change (°C) for the model, by the hour of each step's midpoint.

        In the reduce_only hours a negative slot (house loses heat → more supply) is ignored.
        """
        # Step in UTC and convert back, so steps across a DST change land on the right local hour
        start_utc, tz = start_local.astimezone(timezone.utc), start_local.tzinfo
        return [
            self.applied(
                (start_utc + timedelta(hours=(k + 0.5) * dt_hours)).astimezone(tz).hour, reduce_only
            )
            * dt_hours
            for k in range(n_steps)
        ]

    def applied(self, hour: int, reduce_only: frozenset[int] = REDUCE_ONLY_HOURS) -> float:
        """The slot's q as the MPC applies it (°C/h): negative slots are ignored in reduce_only hours."""
        q = self.q[hour]
        return max(q, 0.0) if hour in reduce_only else q

    def to_dict(self) -> dict:
        return {"q": list(self.q), "updates": self.updates}

    def load_dict(self, data: dict) -> None:
        q = [float(v) for v in data["q"]]
        if len(q) != 24:
            raise ValueError(f"expected 24 values, got {len(q)}")
        self.q = q
        self.updates = int(data.get("updates", 0))
