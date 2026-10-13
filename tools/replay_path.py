#!/usr/bin/env python3
"""Self-checks and closed-loop replay for path_mpc.

1. Unit checks: gradient vs finite differences, bounds, flat plan in band, hard cut
   when too warm, smoothing weight, zero weights, warm start.
2. Closed-loop replay over recorded weather. The "house" (plant) is the coupled
   two-room model fitted on the same data, so the controllers, which use the per-room
   single-mass seeds, face realistic model mismatch. Forecasts are perfect (recorded
   outdoor/solar) and control is hourly. Living-room importance follows the night-mode
   schedule (0 from 18:00 to 09:00). Today's shadow combine (per-room single-supply MPC
   at 3 h, weighted mean) is compared with path MPC (--sweep: centre pull, smoothing
   and horizon variants).

Needs numpy + scipy. Usage: replay_path.py [days=14]
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import numpy as np
from scipy.optimize import least_squares
from rc_mpc import compute_supply_setpoint
from room_model import RoomParams, combine
from path_mpc import PathRoom, _LinearRoom, _cost_grad, plan_supply

C = 500.0
SEEDS = {  # per-room single-mass (UA, R, g), 2026-10-13
    "bedroom": (8.0, 0.0783, 0.14),
    "living": (9.9, 0.0582, 0.19),
}
TARGET, BAND, SUPPLY_MIN, SUPPLY_MAX = 22.3, 0.2, 15.0, 70.0


def params(name):
    ua, r, g = SEEDS[name]
    return RoomParams(R=r, C=C, UA=ua, g=g)


def unit_checks():
    rng = np.random.default_rng(1)
    out = [8.0, 7.5, 7.0, 6.5, 6.0, 6.0]
    sol = [200.0, 100.0, 0.0, 0.0, 0.0, 0.0]
    rooms = [PathRoom(21.9, TARGET, 1.0, params("bedroom")), PathRoom(22.6, TARGET, 0.5, params("living"))]
    lin = [_LinearRoom(r, out, sol) for r in rooms]
    # Gradient matches finite differences (away from kinks)
    for _ in range(5):
        s = list(rng.uniform(25, 60, 6))
        _, _, g, _ = _cost_grad(s, lin, BAND, 1e-3, 40.0, True, 0.1)
        for j in range(6):
            h = 1e-5
            sp = list(s); sp[j] += h
            sm = list(s); sm[j] -= h
            fp = sum(_cost_grad(sp, lin, BAND, 1e-3, 40.0, False, 0.1)[:2])
            fm = sum(_cost_grad(sm, lin, BAND, 1e-3, 40.0, False, 0.1)[:2])
            assert abs((fp - fm) / (2 * h) - g[j]) < 1e-5 * max(1, abs(g[j])), (j, g[j], (fp - fm) / (2 * h))
    # Bounds: lower = warmest weighted room, upper = supply_max
    res = plan_supply(rooms, out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0)
    assert res.converged and all(22.6 - 1e-9 <= v <= SUPPLY_MAX + 1e-9 for v in res.plan), res.plan
    # Flat weather, room exactly at its steady state on target: plan stays flat at that supply
    p = params("bedroom")
    s_eq = (TARGET * (p.UA + 1 / p.R) - 8.0 / p.R) / p.UA
    flat = plan_supply([PathRoom(TARGET, TARGET, 1.0, p)], [8.0] * 6, [0.0] * 6,
                       SUPPLY_MIN, SUPPLY_MAX, BAND, s_eq)
    assert max(flat.plan) - min(flat.plan) < 0.05 and abs(flat.plan[0] - s_eq) < 0.05, flat.plan
    # 1 °C too warm: first hour at the lower bound (no heat), later hours recover
    warm = plan_supply([PathRoom(TARGET + 1.0, TARGET, 1.0, p)], [8.0] * 6, [0.0] * 6,
                       SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0)
    assert abs(warm.plan[0] - (TARGET + 1.0)) < 0.5 and warm.plan[-1] > warm.plan[0] + 5, warm.plan
    # More smoothing → smaller total swing
    def swing(r):
        return sum(abs(a - b) for a, b in zip([40.0] + r.plan[:-1], r.plan))
    lo = plan_supply(rooms, out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0, w_smooth=1e-5)
    hi = plan_supply(rooms, out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0, w_smooth=1e-1)
    assert swing(hi) < swing(lo), (swing(lo), swing(hi))
    # Zero weights → no plan; weight-0 rooms still get a path
    assert plan_supply([PathRoom(22.0, TARGET, 0.0, p)], out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0) is None
    mixed = plan_supply([PathRoom(22.0, TARGET, 1.0, p), PathRoom(21.0, TARGET, 0.0, params("living"))],
                        out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0)
    assert len(mixed.paths) == 2 and len(mixed.paths[1]) == 6
    # Warm start from the solution converges faster than a cold start
    cold = plan_supply(rooms, out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0)
    hot = plan_supply(rooms, out, sol, SUPPLY_MIN, SUPPLY_MAX, BAND, 40.0, warm_start=cold.plan)
    assert hot.iterations < cold.iterations, (hot.iterations, cold.iterations)
    print("unit checks OK (cold start %d iterations, warm %d)" % (cold.iterations, hot.iterations))


def load(days):
    from fit_rc_multistep import load_data
    db = load_data(days, "sensor.sonoff_sensor_03_temperature")
    dl = load_data(days, "sensor.sonoff_sensor_07_temperature")
    assert (db["h"] == dl["h"]).all()
    ok = db["ok"] & dl["ok"]

    def ffill(x):
        x = x.copy()
        for i in range(1, len(x)):
            if np.isnan(x[i]):
                x[i] = x[i - 1]
        return x
    return dict(h=db["h"], B=db["Tin_p"], L=dl["Tin_p"], Sup=ffill(db["Sup"]),
                Tout=ffill(db["Tout"]), I=ffill(np.nan_to_num(db["I"])), ok=ok)


def fit_plant(D, H=3):
    """Coupled two-room model fitted on 3 h-ahead error (as in the 2026-10-13 test)."""
    B, L, S, O, I, ok = D["B"], D["L"], D["Sup"], D["Tout"], D["I"], D["ok"]

    def errs(p):
        ub, lb, gb, ul, ll, gl, c, cl = p
        e = []
        for k in range(len(B) - H):
            if not ok[k:k + H + 1].all():
                continue
            b, l = B[k], L[k]
            for j in range(H):
                b, l = (b + ub * (S[k + j] - b) + lb * (O[k + j] - b) + gb * I[k + j] + c * (l - b),
                        l + ul * (S[k + j] - l) + ll * (O[k + j] - l) + gl * I[k + j] + cl * (b - l))
            e += [B[k + H] - b, L[k + H] - l]
        return np.array(e)
    r = least_squares(errs, [0.016, 0.025, 0.0003, 0.019, 0.034, 0.0004, 0.05, 0.05],
                      bounds=([1e-4] * 2 + [0] + [1e-4] * 2 + [0, 0, 0], [0.5, 0.5, 0.01, 0.5, 0.5, 0.01, 0.5, 0.5]))
    return r.x


def living_weight(ts):
    hour = (ts / 3600 + 2) % 24  # CEST
    return 1.0 if 9 <= hour < 18 else 0.0


def simulate(D, plant, controller, n_fc):
    ub, lb, gb, ul, ll, gl, c, cl = plant
    O, I, h = D["Tout"], D["I"], D["h"]
    n = len(h) - n_fc
    b, l = D["B"][0], D["L"][0]
    prev = 40.0
    state = {}
    outside = above = heat = swing = err_sum = 0.0
    over = under = 0.0
    for k in range(n):
        weights = (1.0, living_weight(h[k]))
        s = controller(b, l, weights, list(O[k:k + n_fc]), list(I[k:k + n_fc]), prev, state)
        s = max(SUPPLY_MIN, min(SUPPLY_MAX, s))
        swing += abs(s - prev)
        prev = s
        # The radiators can't cool: supply below a room means no heat to it
        sb, sl = max(s, b), max(s, l)
        heat += C * (ub * (sb - b) + ul * (sl - l))
        b, l = (b + ub * (sb - b) + lb * (O[k] - b) + gb * I[k] + c * (l - b),
                l + ul * (sl - l) + ll * (O[k] - l) + gl * I[k] + cl * (b - l))
        w_tot = sum(weights)
        for t, w in zip((b, l), weights):
            if w > 0:
                outside += w / w_tot * (abs(t - TARGET) > BAND)
                above += w / w_tot * (t - TARGET > BAND)
                err_sum += w / w_tot * (t - TARGET)
                over = max(over, t - TARGET)
                under = min(under, t - TARGET)
    return dict(outside=outside / n, above=above / n, mean=err_sum / n, over=over, under=under,
                heat=heat / 1000, swing=swing / n)


def baseline(b, l, weights, out, sol, prev, state):
    items = {}
    for name, t, w in (("bedroom", b, weights[0]), ("living", l, weights[1])):
        p = params(name)
        sp, _ = compute_supply_setpoint(t, TARGET, out[:3], sol[:3], p.R, p.C, p.UA, p.g, SUPPLY_MIN, SUPPLY_MAX)
        items[name] = (sp, w)
    mean, _ = combine(items)
    return mean if mean is not None else prev


def path_controller(horizon=6, **kw):
    def ctl(b, l, weights, out, sol, prev, state):
        rooms = [PathRoom(b, TARGET, weights[0], params("bedroom")),
                 PathRoom(l, TARGET, weights[1], params("living"))]
        warm = state.get("plan")
        warm = warm[1:] + warm[-1:] if warm else None
        res = plan_supply(rooms, out[:horizon], sol[:horizon], SUPPLY_MIN, SUPPLY_MAX, BAND, prev, warm_start=warm, **kw)
        if res is None:
            return prev
        state["plan"] = res.plan
        state["nc"] = state.get("nc", 0) + (not res.converged)
        return res.plan[0]
    return ctl


def main():
    unit_checks()
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 14
    D = load(days)
    plant = fit_plant(D)
    print("plant (coupled two-room) params:", np.round(plant, 4))
    print("\n%-34s %8s %7s %7s %7s %7s %8s %8s" % ("controller", "outside", "above", "mean", "max+", "max-", "heat kWh", "swing/h"))
    rows = [("today's shadow (3 h)", baseline), ("path 6 h, no centre pull", path_controller(w_center=0.0)),
            ("path 6 h (defaults)", path_controller())]
    if "--sweep" in sys.argv:
        rows += [("path 6 h, centre=%g" % c, path_controller(w_center=c)) for c in (0.3, 1.0)]
        rows += [("path 6 h, w_S=%g" % w, path_controller(w_smooth=w)) for w in (3e-4, 1e-3)]
        rows += [("path %d h" % n, path_controller(horizon=n)) for n in (3, 4, 9)]
    for label, ctl in rows:
        m = simulate(D, plant, ctl, 6)
        print("%-34s %7.1f%% %6.1f%% %+7.2f %+7.2f %+7.2f %8.0f %7.2f°" % (
            label, 100 * m["outside"], 100 * m["above"], m["mean"], m["over"], m["under"], m["heat"], m["swing"]))


if __name__ == "__main__":
    main()
