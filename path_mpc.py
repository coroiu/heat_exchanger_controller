"""Path-optimising MPC: choose an hourly supply plan for the whole horizon.

No Home Assistant imports — pure Python, independently testable.

Minimises, over S[0..N-1] within [lower, supply_max]:
    comfort    Σ_r w_r Σ_k [max(0, |T_r[k+1] − target_r| − band)² + w_center·(T_r[k+1] − target_r)²]
    smoothness w_S · Σ_k (S[k] − S[k−1])²,  S[−1] = current live supply
Each room uses the single-mass recurrence of rc_mpc (dt = 1 h), so every T_r[k] is
linear in S and the cost is convex. There is no energy term: at a flat kWh price the
only saving is a cooler house (≈ 3 % heat per 0.2 °C), which the target already sets
(tools/replay_path.py, 2026-10-13). The lower bound is max(supply_min, warmest room):
a supply below room temperature would make the model cool the room, while physically
it only means the pump is off and no heat flows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

try:
    from .room_model import RoomParams
except ImportError:  # loaded as a top-level module by tools/
    from room_model import RoomParams

# Fixed penalty per (°C change of supply)² between consecutive hours
SMOOTH_WEIGHT = 1e-4
# Weak pull toward the target inside the band, relative to the cost beyond it, so the
# plan keeps a margin instead of drifting to the band edge
CENTER_WEIGHT = 0.1


@dataclass
class PathRoom:
    t_now: float
    target: float
    weight: float
    params: RoomParams


@dataclass
class PathResult:
    plan: list[float]  # N hourly supply values; plan[0] applies now
    paths: list[list[float]]  # per room: T at hours 1..N
    cost_comfort: float
    cost_smooth: float
    iterations: int
    converged: bool


class _LinearRoom:
    """T[k] = base[k] + Σ_{j<k} gain[k−1−j]·S[j] for k = 0..N."""

    def __init__(self, room: PathRoom, outdoor: list[float], solar: list[float]) -> None:
        p = room.params
        n = len(outdoor)
        a = (p.UA + 1.0 / p.R) / p.C
        b = p.UA / p.C
        c = 1.0 - a
        self.n = n
        self.target = room.target
        self.weight = room.weight
        # gain[m] = ∂T[k]/∂S[k−1−m]
        self.gain = [b * c ** m for m in range(n)]
        self.base = [room.t_now]
        for k in range(n):
            d = (p.g * solar[k] + outdoor[k] / p.R) / p.C
            self.base.append(c * self.base[-1] + d)

    def temps(self, s: list[float]) -> list[float]:
        return [
            self.base[k] + sum(self.gain[k - 1 - j] * s[j] for j in range(k))
            for k in range(self.n + 1)
        ]


def _cost_grad(
    s: list[float],
    rooms: list[_LinearRoom],
    band: float,
    w_s: float,
    prev: float,
    want_grad: bool = True,
    w_center: float = 0.0,
) -> tuple[float, float, list[float], list[list[float]]]:
    n = len(s)
    comfort = 0.0
    grad = [0.0] * n
    paths = []
    for r in rooms:
        t = r.temps(s)
        paths.append(t[1:])
        if r.weight <= 0:
            continue  # predicted only (e.g. a room ramped out at night)
        # dCost/dT[k] for k = 0..N; T[0] is fixed, so its entry stays 0
        d_t = [0.0] * (n + 1)
        for k in range(1, n + 1):
            e = t[k] - r.target
            comfort += r.weight * w_center * e * e
            d_t[k] += r.weight * 2.0 * w_center * e
            viol = abs(e) - band
            if viol > 0:
                comfort += r.weight * viol * viol
                d_t[k] += r.weight * 2.0 * viol * (1.0 if e > 0 else -1.0)
        if want_grad:
            for j in range(n):
                grad[j] += sum(d_t[k] * r.gain[k - 1 - j] for k in range(j + 1, n + 1))
    smooth = 0.0
    last = prev
    for k in range(n):
        diff = s[k] - last
        smooth += w_s * diff * diff
        if want_grad:
            grad[k] += 2.0 * w_s * diff
            if k > 0:
                grad[k - 1] -= 2.0 * w_s * diff
        last = s[k]
    return comfort, smooth, grad, paths


def plan_supply(
    rooms: list[PathRoom],
    outdoor: list[float],
    solar: list[float],
    supply_min: float,
    supply_max: float,
    band: float,
    prev_supply: float,
    w_smooth: float = SMOOTH_WEIGHT,
    w_center: float = CENTER_WEIGHT,
    warm_start: list[float] | None = None,
    max_iter: int = 500,
    tol: float = 1e-4,
) -> PathResult | None:
    """Optimise the hourly supply plan; None when no room has weight.

    Rooms with weight 0 don't affect the plan but still get a predicted path, in input order.
    """
    weighted = [r for r in rooms if r.weight > 0]
    n = len(outdoor)
    if not weighted or n == 0:
        return None
    lin = [_LinearRoom(r, outdoor, solar) for r in rooms]
    lower = min(supply_max, max(supply_min, max(r.t_now for r in weighted)))

    def project(x: list[float]) -> list[float]:
        return [max(lower, min(supply_max, v)) for v in x]

    def total(x: list[float]) -> float:
        c, sm, _, _ = _cost_grad(x, lin, band, w_smooth, prev_supply, False, w_center)
        return c + sm

    start = warm_start if warm_start is not None and len(warm_start) == n else [prev_supply] * n
    s = project(list(start))
    step = 10.0
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        c, sm, g, _ = _cost_grad(s, lin, band, w_smooth, prev_supply, True, w_center)
        f = c + sm
        # Projected gradient step with backtracking (sufficient-decrease test)
        while True:
            cand = project([v - step * gv for v, gv in zip(s, g)])
            dx = [cv - v for cv, v in zip(cand, s)]
            dx2 = sum(v * v for v in dx)
            if dx2 == 0.0:
                break
            if total(cand) <= f + sum(gv * d for gv, d in zip(g, dx)) + dx2 / (2.0 * step):
                break
            step *= 0.5
            if step < 1e-9:
                break
        s = cand
        if math.sqrt(dx2) < tol:
            converged = True
            break
        step *= 2.0  # let the step grow again after a successful iteration
    c, sm, _, paths = _cost_grad(s, lin, band, w_smooth, prev_supply, False, w_center)
    return PathResult(
        plan=s,
        paths=paths,
        cost_comfort=c,
        cost_smooth=sm,
        iterations=it,
        converged=converged,
    )
