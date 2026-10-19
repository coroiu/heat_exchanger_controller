#!/usr/bin/env python3
"""Replay history through room_model.RoomModel and self-check the shadow-mode logic.

1. Synthetic checks: importance ramp, combine(), scoring, anomaly, persistence and
   the rc_mpc.model_inputs refactor.
2. Replay: each room's 3 h-ahead RMS with hourly-mean inputs (perfect forecasts, as in
   fit_rc_multistep.py). Must match the fit's rms for the same params; a mismatch is a
   bug in the in-component model. Also prints the in-component variant, which
   predicts with the supply held constant at its start-of-window value.

Needs numpy + scipy (for the loader). Usage: replay_rooms.py [days=14]
"""
import os, sys, math
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import numpy as np
from rc_mpc import compute_supply_setpoint, model_inputs
from room_model import RoomModel, RoomParams, combine

import ha_local

# name: (sensor, UA, R, g) at C = 500, from the integration's room subentries
ROOMS = {
    title: (d["room_sensor"], d["rc_ua"], d["rc_r"], d["rc_g"]) for title, d in ha_local.rooms()
}
C = 500.0


def old_inputs(horizon_h, smhi, solar, outdoor):
    """The pre-refactor inline logic from OuterCoordinator._run_mpc."""
    if horizon_h < 1.0:
        return smhi[:1] or [outdoor], solar[:1] or [0.0], horizon_h
    n = max(1, round(horizon_h))
    ft = smhi[-1] if smhi else outdoor
    fs = solar[-1] if solar else 0.0
    return (smhi + [ft] * n)[:n], (solar + [fs] * n)[:n], 1.0


def synthetic_checks():
    m = RoomModel(RoomParams(R=0.08, C=C, UA=8.0, g=0.14))
    # Ramp: first call jumps, then 45 min for a full change regardless of tick length
    assert m.advance_ramp(0, 1.0, 45) == 1.0
    assert abs(m.advance_ramp(15 * 60, 0.0, 45) - (1 - 15 / 45)) < 1e-9
    assert abs(m.advance_ramp(30 * 60, 0.0, 45) - (1 - 30 / 45)) < 1e-9
    assert m.advance_ramp(60 * 60, 0.0, 45) == 0.0
    assert m.advance_ramp(65 * 60, None, 45) == 0.0  # unknown commanded value: hold
    # combine
    assert combine({}) == (None, None)
    assert combine({"a": (40.0, 0.0), "b": (30.0, 0.0)}) == (None, None)
    mean, mx = combine({"a": (40.0, 1.0), "b": (30.0, 0.0)})
    assert (mean, mx) == (40.0, 40.0)
    mean, mx = combine({"a": (40.0, 1.0), "b": (30.0, 3.0)})
    assert abs(mean - 32.5) < 1e-9 and mx == 40.0
    # Scoring: due predictions scored once, late ones dropped, paused ones dropped
    m.push_prediction(100.0, 21.0)
    m.push_prediction(200.0, 21.0)
    m.push_prediction(300.0, 21.0)
    m.score_due(150.0, 21.5, paused=False)
    assert m.error_stats()[2] == 1 and abs(m.error_stats()[1] - 0.5) < 1e-9
    m.score_due(250.0, 22.0, paused=True)
    assert m.error_stats()[2] == 1
    m.score_due(300.0 + 601, 22.0, paused=False)  # beyond tolerance
    assert m.error_stats()[2] == 1
    m.score_due(150.0 + 24 * 3600 + 1, 22.0, paused=False)  # window prune
    assert m.error_stats() == (None, None, 0)
    # Persistence round trip
    m.push_prediction(10.0, 20.0)
    m2 = RoomModel(m.params)
    m2.load_dict(m.to_dict())
    assert m2.to_dict() == m.to_dict()
    # Anomaly: steady → no; cooling 1.5 °C/h for 45 min → yes after the hold
    a = RoomModel(RoomParams(R=0.08, C=C, UA=8.0, g=0.14))
    t = 0.0
    for _ in range(12):
        assert not a.check_anomaly(t, 22.0, 8.0, 0.0); t += 300
    flags = []
    temp = 22.0
    for _ in range(12):
        temp -= 1.5 / 12; flags.append(a.check_anomaly(t, temp, 8.0, 0.0)); t += 300
    assert any(flags) and not flags[0], flags
    # model_inputs is a pure refactor of the old inline code
    smhi = [8.0, 7.5, 7.0, 6.8, 6.5]; sol = [100.0, 50.0, 0.0, 0.0, 0.0]
    for hz in (0.5, 1, 3, 5, 12):
        for sm, so in ((smhi[: math.ceil(hz)], sol[: math.ceil(hz)]), (smhi, sol), ([], [])):
            assert model_inputs(hz, sm, so, 9.0) == old_inputs(hz, sm, so, 9.0)
            n, s_, dt = model_inputs(hz, sm, so, 9.0)
            assert compute_supply_setpoint(21.8, 22.3, n, s_, 0.085, C, 7.6, 0.15, 20, 70, dt_hours=dt) == \
                compute_supply_setpoint(21.8, 22.3, *old_inputs(hz, sm, so, 9.0)[:2], 0.085, C, 7.6, 0.15, 20, 70,
                                        dt_hours=old_inputs(hz, sm, so, 9.0)[2])
    print("synthetic checks OK")


def replay(days):
    from fit_rc_multistep import load_data
    import room_model
    room_model.ERROR_WINDOW_S = float("inf")  # score the whole replay, not the last 24 h
    H = 3
    for name, (eid, UA, R, g) in ROOMS.items():
        D = load_data(days, eid)
        h, Tin_p, Tout, Sup, I, ok = D["h"], D["Tin_p"], D["Tout"], D["Sup"], D["I"], D["ok"]
        res = {}
        for variant in ("hourly_supply", "constant_supply"):
            m = RoomModel(RoomParams(R=R, C=C, UA=UA, g=g))
            for k in range(len(h)):
                if ok[k]:
                    m.score_due(h[k], Tin_p[k], paused=False)
                # The fit only scores windows whose every hour is usable
                if k + H < len(h) and ok[k:k + H + 1].all():
                    sup = list(Sup[k:k + H]) if variant == "hourly_supply" else [Sup[k]] * H
                    m.push_prediction(h[k] + H * 3600, m.predict(Tin_p[k], list(Tout[k:k + H]), list(I[k:k + H]), sup))
            rms, bias, n = m.error_stats()
            res[variant] = (rms, bias, n)
        print("%-8s %s  hourly supply: rms=%.3f bias=%+.3f n=%d   constant supply: rms=%.3f bias=%+.3f n=%d"
              % (name, eid, *res["hourly_supply"], *res["constant_supply"]))


if __name__ == "__main__":
    synthetic_checks()
    replay(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
