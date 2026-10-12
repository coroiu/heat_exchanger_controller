# Multi-room support (shadow mode) — design

Date: 2026-10-12. Status: approved, not implemented.

## Why

The live controller steers on `sensor.indoor_steer_temperature`, an HA template that blends the bedroom and living-room sensors by importance. The living-room weight ramps to 0 around 18–20 and back to 1 around 09:00. So the controlled variable switches rooms twice a day, and the rooms differ by up to ~1 °C at night. The single house model and the disturbance observer then learn the blend ramps as if they were physics.

An offline fit on 14 days (3 h-ahead RMS) gave:

| Signal | 3 h RMS (°C) |
|---|---|
| Steered reading | 0.273 |
| Bedroom | 0.273 |
| Living room | 0.255 |
| 50/50 house average | 0.186 |

UA·R ≈ 0.6 in every case. The living room reacts faster and has about 2× the solar gain, and the bedroom shows about +0.05 °C/h of night-time gain (occupants). Each room is predictable on its own, so the room targets and the importance ramp belong inside the component rather than in a template.

## Scope

This design only covers **shadow mode**. Rooms are added with their own models, targets and importance, and their predictions and would-be setpoints are computed and scored. **The live control path is not changed.** The controller keeps steering on the existing indoor sensor, i.e. the steered reading. Switch-over is a later, separate decision based on the shadow results.

Out of scope (planned follow-ups):
- Online parameter estimation (RLS/Kalman) for the room models. The `RoomModel` interface keeps the parameters swappable so an estimator can be added without changing entities.
- A two-mass (air + structure) model, to capture stored heat.
- A path-optimising MPC that chooses a supply *sequence* minimising error along the whole path, with a penalty on supply changes, instead of one constant supply that hits the target at the horizon end. That gives the "cut hard, then land smoothly" curve. It depends on the two-mass model, because with one mass the landing would undershoot when cooling and overshoot when heating.
- Thermostat control per room (e.g. the baby room).

## Architecture

- **Room = config subentry** (type `room`), added via "Add room" on the integration (HA 2026.4 supports subentries). Config: name, temperature sensor, initial R / UA / g / C (seeded from the offline per-room fit). Each subentry gets its own device holding its entities.
- **`room_model.py`**: a new pure-Python module with no HA imports, like `rc_mpc.py`. `RoomModel` holds:
  - the parameters
  - the importance ramp state
  - the prediction buffer and rolling error

  It reuses `compute_supply_setpoint` for the room MPC and the forward prediction.
- **`OuterCoordinator`** owns one `RoomModel` per subentry. Each tick, after the live MPC, it feeds every room the same SMHI/Open-Meteo forecasts and the same pump state, then publishes the results in `OuterData.rooms` along with the combined shadow setpoint. No extra fetches.
- **Live path untouched**: `_run_mpc` and `InnerCoordinator` read no room data. Switch-over later will be a single option that chooses the house setpoint or the combined room setpoint.
- **Adding or removing a room reloads the entry**, like structural options. The observer and PID state survive through their stores.
- **The house sensor stays the existing `indoor_temp_entity`.** It can later be repointed (e.g. to a bedroom + living-room mean helper) via Reconfigure; no new field.

## Entities

Per room device:

| Entity | Kind | Notes |
|---|---|---|
| `target` | number (°C) | Room's own target. State option, so changing it doesn't reload. |
| `importance` | number (0–1) | Written by external automations or by hand. Restored on restart. |
| `importance_ramp_time` | number (min) | Time for a full 0→1 change, default 45. |
| `importance_ramped` | sensor | Slews linearly toward `importance` each outer tick. Restored on restart. |
| `rc_r`, `rc_ua`, `rc_g`, `rc_c` | number | Structural (reload). |
| `predicted_temp` | sensor | Room temperature at the control horizon under the live supply setpoint. |
| `supply_setpoint` | sensor | What this room's own MPC would request to reach its target. |
| `prediction_error` | sensor | Rolling 24 h RMS of the 3 h-ahead prediction. Attributes: mean bias, sample count, `anomaly`. |

House device (new):

| Entity | Kind | Notes |
|---|---|---|
| `shadow_supply_setpoint` | sensor | Importance-weighted mean (by ramped importance) of the room setpoints. Attributes: max-rule value, per-room weights. Unavailable when all weights are 0. |
| `scoring_paused` | switch | House-wide. Predictions still run, but errors aren't scored. Use it for open windows and similar events. |
| `window_suspected` | binary_sensor | On when any room cools more than 0.3 °C/h faster than the model's zero-heat prediction for at least 15 min. Diagnostic only in shadow mode: it doesn't pause scoring. |

## Data flow per outer tick (per room)

1. Read the room sensor; it's the model's starting state. If it's unavailable, skip the room (its entities go unavailable and it gets no weight).
2. Advance the importance ramp.
3. Run the room MPC against the room target, on the house forecasts. When the pump is off, predict with `prediction_supply = t_in`, as for the house.
4. Predict the room temperature 3 h ahead under the **live** supply setpoint and store it with a timestamp. The 3 h window is fixed, independent of the control horizon, so scores stay comparable with the offline fit.
5. Score every stored prediction that has come due against the actual reading (unless `scoring_paused`), and update the rolling RMS and bias.
6. Update the anomaly check (measured cooling rate vs the zero-heat prediction).

Then combine the room setpoints by ramped importance into `shadow_supply_setpoint`.

## Persistence and errors

- An HA `Store` (`.storage/heat_exchanger_controller.<entry_id>.rooms`) holds each room's prediction buffer, error accumulator and ramped importance. It's saved debounced and flushed on unload, like the observer store.
- Room faults show only in that room's entities and as `room_<name>_unavailable` in the house `status` attributes. **Shadow-mode problems never change the house status level or the live control path.**

## Testing

- `room_model.py` is pure Python. A replay script runs the 14-day history through `RoomModel` and must reproduce the offline fit scores (≈0.27 °C bedroom, ≈0.26 °C living room). A mismatch means a bug in the in-component model.
- The ramp, the scoring and the combination step get small standalone checks in the same script.

## Rollout

1. Ship, then add the bedroom and living-room subentries with the fitted parameters.
2. Point the existing morning/evening ramp automations at the new `importance` numbers as well. The template stays, because it still feeds the live controller.
3. Run for 1–2 weeks. Pause scoring (or record exclusions) for windows, fires and similar events.
4. Decide on switch-over based on:
   - per-room `prediction_error`
   - how `shadow_supply_setpoint` compares with the live setpoint, especially around 09:00 and 18:00
   - whether `window_suspected` is reliable enough to act on
