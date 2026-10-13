#!/usr/bin/env python3
"""Offline test: does a two-mass (air + structure) model beat the single mass, and is it
better per room or at house level?

Model per hourly step (ratios to C, as in fit_rc_multistep.py):
    T  += ua*(S - T) + l*(Tout - T) + h*(Tm - T) + g*I
    Tm += hm*(T - Tm)
Only the air T is measured, so the structure temperature Tm is estimated with a
fixed-gain Kalman filter (steady-state gain km on the one-step air innovation),
using past data only. Each 3 h-ahead prediction starts from the measured air and the
filtered Tm. h = hm = km = 0 gives back the single-mass model.

Compared on the per-room 3 h error:
  per-room   — each room fitted and filtered on its own sensor
  house      — one model on the mean of both rooms; a room is predicted as the house
               prediction plus its current offset from the house (held for 3 h)

Usage: fit_two_mass.py [days=14]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy.optimize import least_squares
from fit_rc_multistep import load_data

ROOMS = {"bedroom": "sensor.sonoff_sensor_03_temperature", "living": "sensor.sonoff_sensor_07_temperature"}
H = 3


def run(params, Tp, Sup, Tout, I, ok, two_mass):
    """Return {k: 3 h-ahead prediction started at hour k} for every fully usable window."""
    if two_mass:
        ua, l, g, h, hm, km = params
    else:
        (ua, l, g), h, hm, km = params, 0.0, 0.0, 0.0
    preds = {}
    tm = None
    t_pred = None  # one-step air prediction for hour k, from hour k-1
    for k in range(len(Tp)):
        if not ok[k]:
            tm = t_pred = None  # gap: restart the filter
            continue
        t = Tp[k]
        if tm is None:
            tm = t  # no information yet: assume structure at air temperature
        elif t_pred is not None:
            tm += km * (t - t_pred)  # Kalman correction of the hidden state
        if k + H < len(Tp) and ok[k:k + H + 1].all():
            a, m = t, tm
            for j in range(H):
                a, m = (a + ua * (Sup[k + j] - a) + l * (Tout[k + j] - a) + h * (m - a) + g * I[k + j],
                        m + hm * (a - m))
            preds[k] = a
        # Advance the filter one hour
        t_pred = t + ua * (Sup[k] - t) + l * (Tout[k] - t) + h * (tm - t) + g * I[k]
        tm = tm + hm * (t - tm)
    return preds


def errors(preds, Tp):
    return np.array([Tp[k + H] - p for k, p in preds.items()])


def fit(D, Tp, two_mass):
    if two_mass:
        x0 = [0.016, 0.025, 0.0003, 0.1, 0.05, 0.1]
        lo, hi = [1e-4, 1e-4, 0, 0, 1e-4, 0], [0.5, 0.5, 0.01, 0.9, 0.5, 2.0]
    else:
        x0, lo, hi = [0.016, 0.025, 0.0003], [1e-4, 1e-4, 0], [0.5, 0.5, 0.01]
    args = (Tp, D["Sup"], D["Tout"], D["I"], D["ok"], two_mass)
    r = least_squares(lambda p: errors(run(p, *args), Tp), x0, bounds=(lo, hi))
    return r.x, run(r.x, *args)


def stats(e, mask=None):
    if mask is not None:
        e = e[mask]
    return "rms=%.3f mean=%+.3f n=%d" % (np.sqrt((e ** 2).mean()), e.mean(), len(e))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    data = {name: load_data(days, eid) for name, eid in ROOMS.items()}
    hb, hl = data["bedroom"]["h"], data["living"]["h"]
    assert len(hb) == len(hl) and (hb == hl).all(), "hour grids differ; rerun"
    D = dict(data["bedroom"])  # shared inputs: Sup, Tout, I, h
    D["ok"] = data["bedroom"]["ok"] & data["living"]["ok"]
    rooms_tp = {n: d["Tin_p"] for n, d in data.items()}
    house_tp = (rooms_tp["bedroom"] + rooms_tp["living"]) / 2
    # Night = prediction ending 21:00–07:00 local (CEST, UTC+2): stored-heat territory
    end_hour_local = ((D["h"] + H * 3600) / 3600 + 2) % 24

    print("days", days, "hours", len(D["h"]), "usable", int(D["ok"].sum()))
    for two_mass in (False, True):
        label = "two-mass" if two_mass else "single  "
        hx, hpred = fit(D, house_tp, two_mass)
        print(f"\n== {label} house params", np.round(hx, 5))
        print(f"   house mean itself: {stats(errors(hpred, house_tp))}")
        for name, tp in rooms_tp.items():
            x, pred = fit(D, tp, two_mass)
            ks = sorted(pred)
            night = np.array([(end_hour_local[k] >= 21) | (end_hour_local[k] < 7) for k in ks])
            e_room = errors(pred, tp)
            # House-level: house prediction + the room's offset at the start
            e_house = np.array([tp[k + H] - (hpred[k] + tp[k] - house_tp[k]) for k in ks])
            print(f"   {name:8s} per-room: {stats(e_room)}  night {stats(e_room, night)}   params {np.round(x, 5)}")
            print(f"   {name:8s} house   : {stats(e_house)}  night {stats(e_house, night)}")


if __name__ == "__main__":
    main()
