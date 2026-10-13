# Path-optimising MPC (shadow mode) — design

Date: 2026-10-13. Status: approved; revised after the offline replay (see "Replay outcome").

## Replay outcome (2026-10-13, `tools/replay_path.py`)

Changes to the design below:
- **The comfort ↔ efficiency slider and the energy term are dropped** (user decision). At a flat kWh price the only saving is a cooler house. The strongest useful weight saved 1.4 % heat while time outside the band rose from 10 % to 15 %, so the target is the honest energy knob (≈ 3 % heat per 0.2 °C).
- **A weak pull towards the target inside the band was added** (`CENTER_WEIGHT` 0.1). Without it the plan drifts to the band edge and model error pushes the rooms out (20.5 % outside vs 14.1 % today).
- **Pump off is modelled as a lower bound:** supply ≥ the warmest weighted room, i.e. no heat. This replaces the pump-state rule, because the single-mass model would otherwise let a low supply cool the room.
- **The smoothing weight is 1e-4.** 1e-3 blocked the hard cut when a room is too warm.

Closed loop over 14 days. The plant is a coupled two-room model, the controllers use the per-room single-mass models, forecasts are perfect and control is hourly:

| | Today (3 h) | Path MPC (6 h) |
|---|---|---|
| Time outside ±0.2 °C | 14.1 % | 10.5 % |
| Worst undershoot | −0.68 °C | −0.37 °C |
| Worst overshoot | +0.52 °C | +0.52 °C (same; one shared supply can't prevent it) |
| Heat | 73 kWh | 73 kWh |
| Supply change per hour | 1.3 °C | 1.9 °C |

Horizon: 9 h gives the same result as 6 h, while 3–4 h is worse (12–13 % outside).

## Why

The live MPC picks **one constant supply** that makes the predicted indoor temperature hit the target exactly at the end of the horizon. It ignores what happens along the way, so the room can drift during the horizon, and it holds supply too long and overshoots (e.g. 2026-10-11/12). It also only weighs the final hour of the forecast, so one wrong forecast hour moves the whole decision: the 5 h horizon over-heated on SMHI's cold evening forecast.

The goal is **comfort along the whole path**. A secondary goal is a user-chosen trade against energy.

The design of 2026-10-12 said this depends on a two-mass model. The 2026-10-13 structure test (CLAUDE.md) showed the single mass predicts 3 h ahead about as well, so the design uses today's per-room single-mass models.

## Scope

- **Shadow only.** `_run_mpc` and `InnerCoordinator` read nothing from it. Switch-over is a later, separate decision.
- **Tariff:** flat kWh price (confirmed by the user), so only the total amount of heat matters, not its timing. Pre-heating never saves energy at a flat price; holding back before forecast sun does.
- **Out of scope:**
  - two-mass model and Kalman filter (tested, no gain);
  - time-varying prices;
  - return-temperature tariffs.

## Approach

A convex optimisation over an hourly supply plan, solved exactly. It was chosen over:
- a coarse grid search: it lands up to a step off, and its plan flickers from tick to tick;
- a fixed "cut, then hold" shape: it can't express e.g. easing off ahead of sun.

Receding horizon: the whole plan is optimised each outer tick, but only the first hour is used.

### Cost

For a supply plan S[0..N−1]:

```
comfort    = Σ_rooms importance_r · Σ_k max(0, |T_r[k+1] − target_r| − band)²
energy     = w_E · Σ_k Σ_rooms importance_r · UA_r · (S[k] − T_r[k])   (radiator heat, model units)
smoothness = w_S · Σ_k (S[k] − S[k−1])²,  S[−1] = current live supply setpoint
minimise comfort + energy + smoothness  subject to  supply_min ≤ S[k] ≤ supply_max
```

- The comfort error is symmetric with a dead band: inside ±band it costs nothing, and beyond the band it grows quadratically.
- `w_E` comes from the comfort ↔ efficiency slider, calibrated in the replay so that 50 % is a sensible middle.
- `w_S` is a fixed small constant.
- Each T_r[k] is linear in S (the single-mass recurrence), so the cost is convex and its gradient exact.
- With the pump off, the plan's first hour is simulated with supply = room temperature, as in the live MPC.

### Solver

- Projected gradient descent with step-size backtracking, in pure Python, with no new dependency.
- N = horizon in hours (default 6).
- Warm start: the previous plan shifted by one hour, with the last value repeated.
- Fixed iteration cap. If it doesn't converge, the last good plan is kept and the fault is reported.

### Two runs per outer tick

1. **Rooms:** every room subentry, weighted by its ramped importance. This replaces averaging the room setpoints, which is what `shadow_supply_setpoint` does today, with optimising one supply for all rooms jointly.
2. **House:** one "room" built from the house RC options, the steering sensor and the house target. This is like-for-like with the live controller.

## Components

- **`path_mpc.py`** (pure Python, like `rc_mpc.py` and `room_model.py`): `plan_supply(rooms, outdoor, solar, supply_min, supply_max, band, w_E, w_S, prev_supply, pump_off, warm_start)`. It returns:
  - the plan;
  - each room's predicted path;
  - the cost split: comfort, energy, smoothness;
  - the iteration count and a converged flag.
- **`OuterCoordinator`:** runs both plans after `_run_rooms`, inside try/except. Failures go to `room_faults`, never to `faults`. Forecasts are fetched for at least the plan horizon.
- **Settings** (`RestoreNumber`s on the house device, pushed into the coordinator, never reload):
  - `comfort_efficiency`: 0–100 %, default set after calibration;
  - `comfort_band`: ± °C, default 0.2;
  - `path_horizon`: h, default 6, range 2–12.
- **Entities:**
  - house `path_supply_setpoint` (first hour of the rooms plan). Attributes: `plan` (timestamped), `cost` split, `house_plan` (house-run plan), `house_first_hour`, `converged`.
  - per room `planned_temp` (next-hour value). Attribute: `path` (timestamped planned curve).
  - house `planned_indoor_temp`, from the house run.
- **Dashboard:**
  - each room tab: the planned curve beside the actual temperature and the target band;
  - Home tab: the path supply plan against the live setpoint.

## Errors

- Shadow problems never change the status level or the live path.
- An unavailable room drops out of the cost; with no rooms left, the rooms plan is unavailable.
- Non-convergence keeps the last good plan, flags `converged: false` and adds `path_mpc_not_converged` to the room faults.

## Testing

- **Unit checks** (in a tools script):
  - the plan always stays within supply min and max;
  - a room inside the band with flat weather gives an almost flat plan;
  - a room 1 °C too warm gives a hard cut followed by a recovery;
  - more weight on smoothness gives fewer swings;
  - with zero room weight, nothing is planned;
  - the gradient matches finite differences.
- **Closed-loop replay** over the last 14 days, with the per-room models as the plant and recorded outdoor and solar as perfect forecasts. Compare single-supply MPC (today's) with path MPC on:
  - time outside the band;
  - maximum overshoot;
  - heat used;
  - supply swings.

  Repeat at several slider values to calibrate `w_E` and pick the default.
- **Live in shadow:**
  - the plan is stable from tick to tick (no flicker);
  - compare the path setpoint with the live one around 09:00, 18:00 and on sunny afternoons.

  A plan's predicted curve can't be scored against reality until it's applied, because the real supply follows the old controller. The underlying room models are already scored by `prediction_error`.

## Rollout

1. Offline: implement and validate `path_mpc.py` and the replay, then choose the slider default.
2. Ship in shadow; HA restart with the user's OK.
3. Run alongside the room shadow for 1–2 weeks, then decide on switch-over separately.
